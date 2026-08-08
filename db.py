"""Доступ к базе: один класс, два движка — SQLite (текущий прод) и PostgreSQL.

Почему второй движок вообще нужен. В SQLite писатель ровно один на файл:
пока идёт запись, остальные ждут. На одном процессе это честно и быстро
(WAL + synchronous=NORMAL дают тысячи транзакций в секунду), но воркеры на
одном файле начинают ждать друг друга на блокировке, а сеть между машинами
файл вообще не отдаёт. PostgreSQL снимает и то, и другое.

Весь SQL в коде писался легальным для обоих движков заранее: `ON CONFLICT
DO NOTHING`, `RETURNING`, `UPDATE ... WHERE <guard>` + rowcount, шимы
`GREATEST`/`LEAST`. Диалектных мест осталось ровно пять, и все они здесь:
  * драйвер и прагмы соединения;
  * генерация DDL (`INTEGER PRIMARY KEY` → `BIGSERIAL`, `REAL` → `DOUBLE
    PRECISION`, `DEFAULT "en"` → `DEFAULT 'en'`);
  * чтение списка колонок (`PRAGMA table_info` → `information_schema`);
  * триггеры-инварианты (`RAISE(ABORT)` → PL/pgSQL `RAISE EXCEPTION`);
  * снимок базы (sqlite backup API → `pg_dump`).

Соединения — ПО ПОТОКАМ, а не через пул драйвера. Транзакция здесь живёт в
`tx()` и охватывает несколько вызовов подряд, поэтому все они обязаны идти по
одному соединению; пул с выдачей соединения на операцию это ломает. Верхнюю
границу задаёт пул потоков FastAPI (синхронные хендлеры выполняются в нём), и
на PostgreSQL за ним надо следить: воркеры × потоки не должны превышать
max_connections сервера.
"""
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from contextlib import contextmanager

# Только счётчики. server.obs не тянет ни db, ни игровую логику — специально,
# чтобы измеряемое можно было импортировать из измеряющего без кольца.
from server import obs

log = logging.getLogger(__name__)

# сколько раз повторить BEGIN IMMEDIATE, если база занята другим писателем
TX_RETRIES = 5

# Префикс ключей app_state, по которому миграция отделяет снимки конфига ивентов
# от аналитики в старой таблице `events`. Подчёркивание оставлено одиночным
# джокером LIKE, а не экранировано: ESCAPE ведёт себя по-разному на двух
# движках, а событий вида «eventXcfg:...» не существует. Сам шаблон уходит
# ПАРАМЕТРОМ — литеральный процент в тексте запроса сломал бы подстановку под
# PostgreSQL, где плейсхолдеры переписываются в %s (см. _sql).
EVENT_CFG_PREFIX = "event_cfg:%"

# Ошибки, на которые смотрит КОД (а не только человек в логе). Классы у
# драйверов разные, решения по ним одинаковые, поэтому ловить надо кортеж, а не
# sqlite3.IntegrityError напрямую — иначе на PostgreSQL нарушение инварианта
# пролетело бы мимо обработчика и превратилось в 500.
INTEGRITY_ERRORS: tuple = (sqlite3.IntegrityError,)
# «база занята» — то, что имеет смысл повторить
BUSY_ERRORS: tuple = (sqlite3.OperationalError,)


def _register_postgres_errors():
    """Дописать классы psycopg в кортежи ошибок (один раз на процесс)."""
    global INTEGRITY_ERRORS, BUSY_ERRORS
    try:
        from psycopg import errors as pg
    except ImportError as e:
        # ImportError на первой строке старта — это полчаса чтения трейсбека
        # ради одной забытой команды. Пишем сразу, что делать
        raise SystemExit(
            "DATABASE_URL задан, значит работаем на PostgreSQL, но драйвер не "
            'установлен. Поставь: pip install "psycopg[binary]>=3.2,<4"') from e

    # RaiseException — это наш собственный RAISE EXCEPTION из триггера
    # (append-only книги, вменяемость баланса). Формально он не IntegrityError,
    # но для вызывающего это ровно то же «база не дала записать».
    INTEGRITY_ERRORS = tuple(dict.fromkeys(
        INTEGRITY_ERRORS + (pg.IntegrityError, pg.RaiseException, pg.DataError)))
    BUSY_ERRORS = tuple(dict.fromkeys(
        BUSY_ERRORS + (pg.SerializationFailure, pg.DeadlockDetected,
                       pg.LockNotAvailable)))


class DataBase:
    # Диалект и два имени, которые расходятся между движками. Всё остальное в
    # новом SQL — общий синтаксис (ON CONFLICT DO NOTHING, RETURNING,
    # UPDATE ... WHERE <guard>). Значения ниже — дефолт класса для SQLite;
    # на PostgreSQL __init__ выставляет их экземпляру.
    DIALECT = "sqlite"
    GREATEST = "MAX"     # на postgres -> "GREATEST"
    LEAST = "MIN"        # на postgres -> "LEAST"

    # Типы SQLite → типы PostgreSQL. INTEGER всегда BIGINT: user_id — это
    # telegram id, он не влезает в 32 бита, и разнотипица между колонками
    # (INT против BIGINT) ломала бы соединения по ним.
    PG_TYPES = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION"}

    # Предохранитель от зависшего стейтмента, мс. Минута — это заведомо больше
    # любого нашего запроса (все они по индексу) и заведомо меньше, чем время,
    # за которое залипший запрос успеет утопить базу блокировками.
    PG_STATEMENT_TIMEOUT_MS = 60_000

    # Ключ межпроцессного замка миграций на PostgreSQL. Advisory-замки лежат в
    # одном пространстве имён на всю базу, поэтому число обязано быть
    # ПОСТОЯННЫМ (вычисляемое, скажем, из имени схемы развело бы воркеров по
    # разным замкам, и замок перестал бы быть замком) и достаточно своим, чтобы
    # не столкнуться с чужим кодом на той же базе. Влезает в bigint, которого
    # ждёт pg_advisory_lock.
    MIGRATION_LOCK_KEY = 0x600DC0071E

    # Сколько ждать чужие миграции, прежде чем сдаться, с. Пять минут — заведомо
    # больше самой долгой из них (перенос аналитики пачками, CREATE UNIQUE INDEX
    # по таблице игроков) и заведомо меньше терпения оркестратора. Не дождавшись,
    # процесс ПАДАЕТ, а не идёт работать: схема, которую прямо сейчас меняют, —
    # это не та схема, под которую написан код.
    MIGRATION_LOCK_TIMEOUT = 300.0

    # Размер пачки при переносе старой таблицы events в аналитику. Порядок
    # тысяч: меньше — лишние обороты и лишний шум в логе, больше — снова длинная
    # блокирующая запись, ради ухода от которой пачки и заводились.
    ANALYTICS_MIGRATION_BATCH = 2000

    # Журнал миграций: единственная таблица, которую создаём вручную, — на неё
    # опирается всё остальное. Держим спекой, а не строкой, чтобы DDL для неё
    # переводился тем же кодом, что и для остальных таблиц.
    MIGRATIONS_SCHEMA = {"name": "TEXT PRIMARY KEY",
                         "applied_at": "REAL NOT NULL DEFAULT 0"}

    def __init__(self, db_file=None, url=None):
        # Путь/строку подключения можно переопределить (тесты используют
        # временную БД). Это единственные ключи, которые читаются ЖИВЫМ
        # os.environ, а не через server.settings: тесты и симуляторы подменяют
        # их уже после импорта модуля, и снимок, сделанный при загрузке
        # настроек, был бы для них путём к боевой базе.
        self.url = url if url is not None else os.environ.get("DATABASE_URL", "")
        if self.url:
            # Заполнен DATABASE_URL — работаем на PostgreSQL, DATABASE_PATH
            # игнорируется (об этом же говорит .env.example)
            self.DIALECT = "postgres"
            self.GREATEST = "GREATEST"
            self.LEAST = "LEAST"
            _register_postgres_errors()
        db_file = db_file or os.environ.get("DATABASE_PATH", "data.db")
        self.db_file = db_file
        # соединение, курсор и глубина транзакции живут ПО ПОТОКАМ: один общий
        # курсор на процесс — это гонка за rowcount (любой SELECT из другого
        # потока сбрасывал его в -1 между записью и проверкой)
        self._local = threading.local()
        self._memory_conn = None
        self.last_insert_id = None

        self.tables_schema = {
            'users': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER UNIQUE',
                'username': 'TEXT',
                'first_name': 'TEXT',
                'lang': 'TEXT DEFAULT "en"',          # язык Mini App (en/uk/ru) для бота/пушей
                'cookies': 'REAL DEFAULT 0',          # основная валюта
                'total_earned': 'REAL DEFAULT 0',     # всего заработано (для ачивок/уровней)
                'total_clicks': 'INTEGER DEFAULT 0',
                'total_merges': 'INTEGER DEFAULT 0',
                # рекорд тира: с него платится first_item_xp — основной XP игры.
                # 0 у старых аккаунтов, поднимается до факта при первом мердже
                # (см. game_logic.claim_item_record)
                'best_item_level': 'INTEGER DEFAULT 0',
                'click_level': 'INTEGER DEFAULT 1',   # прокачка силы клика
                'energy': 'REAL DEFAULT 500',
                'energy_updated_at': 'REAL DEFAULT 0',
                'level': 'INTEGER DEFAULT 1',         # уровень на тропинке
                'xp': 'REAL DEFAULT 0',
                'passive_collected_at': 'REAL DEFAULT 0',  # когда забирали пассивный доход
                'referrer_id': 'INTEGER',
                'source_code': 'TEXT',
                'bp_xp': 'REAL DEFAULT 0',
                'bp_premium': 'INTEGER DEFAULT 0',
                # премиум, купленный на стыке сезонов, переносится в новый сезон
                'bp_premium_next': 'INTEGER DEFAULT 0',
                # УСТАРЕЛИ: забранные награды пасса переехали в bp_claims
                # (см. _backfill_bp_claims). Колонки не читаются и не пишутся, но
                # оставлены — их данные единственный след истории до миграции.
                'bp_claimed_free': 'TEXT DEFAULT "[]"',
                'bp_claimed_premium': 'TEXT DEFAULT "[]"',
                # «отдых»: множитель XP дневного контента за пропущенные дни.
                # Считается один раз в сутки и хранится, иначе первое задание дня
                # съедало бы весь бонус (см. game_logic.rest_mult)
                'rest_day': 'TEXT',
                'rest_mult': 'REAL DEFAULT 1',
                'farm_collected_at': 'REAL DEFAULT 0',      # когда забирали доход фермы
                'active_skin': 'TEXT DEFAULT "classic"',    # скин большой печеньки
                'created_at': 'REAL DEFAULT 0',
                # --- сезоны ---
                'season_id': 'INTEGER DEFAULT 0',           # сезон, в котором живут bp_* и season_earned
                'season_earned': 'REAL DEFAULT 0',          # заработано за текущий сезон (лидерборд)
                # --- ежедневная награда ---
                'daily_streak': 'INTEGER DEFAULT 0',        # текущий стрик (дней подряд)
                'daily_claimed_at': 'REAL DEFAULT 0',       # когда забирали дневную награду
                # --- пуши от бота ---
                'last_notified_at': 'REAL DEFAULT 0',
                'last_seen_at': 'REAL DEFAULT 0',           # последний запрос к API
                'notify_blocked': 'INTEGER DEFAULT 0',      # юзер заблокировал бота
                # смещение часового пояса игрока в минутах от UTC. Телеграм его
                # не отдаёт — присылает клиент. NULL (а не 0) значит «не знаем»:
                # тихие часы для такого игрока считаются по языку, и отличить
                # «не знаем» от честного UTC+0 обязательно, иначе половина
                # аудитории получала бы пуши в свои четыре утра
                'tz_offset_min': 'INTEGER',
                # --- подписка на канал ---
                'channel_claimed': 'INTEGER DEFAULT 0',
                # --- золотая печенька ---
                'golden_next_at': 'REAL DEFAULT 0',     # когда появится следующая
                'golden_expires_at': 'REAL DEFAULT 0',  # пока > now — активна, можно тапнуть
                'golden_effect': 'TEXT',                # эффект активной ("frenzy"/"chain")
                # --- комбо ---
                'combo_mult': 'REAL DEFAULT 1',
                'combo_last_at': 'REAL DEFAULT 0',
                # --- престиж ---
                'prestige_points': 'REAL DEFAULT 0',
                'prestige_count': 'INTEGER DEFAULT 0',
                # --- дневной кап XP за клики ---
                'clicks_day': 'TEXT',                   # 'YYYY-MM-DD' (UTC)
                'clicks_day_count': 'INTEGER DEFAULT 0',
                # --- CPS-лимит (переживает рестарт и мульти-worker) ---
                'cps_ts': 'REAL DEFAULT 0',             # окно анти-чита: время
                'cps_allowance': 'REAL DEFAULT 0',      # окно анти-чита: остаток кликов
                # --- QoL: реролл квеста и заморозка стрика ---
                'quest_reroll_day': 'TEXT',             # день, когда потрачен реролл
                'streak_freeze_week': 'TEXT',           # ISO-неделя, когда потрачена заморозка
                # --- коллекция блестящих печенек ---
                'shiny_pity': 'INTEGER DEFAULT 0',      # мерджей без блестяшки (гарант при пороге)
                # --- Stars: постоянное расширение оффлайн-капа (часы сверх базовых) ---
                'offline_bonus_hours': 'REAL DEFAULT 0',
                # --- оффлайн-рецепт (закваска перед выходом из игры) ---
                'recipe_key': 'TEXT',
                'recipe_started_at': 'REAL DEFAULT 0',
                # --- стартовый чеклист / заказы ---
                'tutorial_done': 'INTEGER DEFAULT 0',
                'orders_completed': 'INTEGER DEFAULT 0',
                'orders_day': 'TEXT',                   # день счётчика заказов
                'orders_day_count': 'INTEGER DEFAULT 0',
                # номер выписки офферов: из него сеется выбор шаблонов. Раньше
                # сеялось из int(time.time()) — секундная гранулярность, и два
                # параллельных обновления брали ОДИН И ТОТ ЖЕ набор шаблонов
                'orders_offer_gen': 'INTEGER NOT NULL DEFAULT 0',
                # --- версии состояния: клиент присылает свою, сервер отвергает
                # ход, посчитанный от устаревшей картинки ---
                'user_revision': 'INTEGER NOT NULL DEFAULT 0',
                'board_revision': 'INTEGER NOT NULL DEFAULT 0',
                # долг: возврат Stars забирает больше, чем есть на балансе, —
                # остаток висит здесь, а не уводит баланс в минус
                'cookie_debt': 'REAL NOT NULL DEFAULT 0',
            },
            # Небольшое ДОЛГОВЕЧНОЕ состояние приложения: снимок конфига
            # прогона ивента и его kill switch (game_logic.event_snapshot).
            #
            # Раньше это жило в таблице `events` — то есть в аналитике. Пока
            # аналитику никто не трогал, разница была незаметной; как только у
            # аналитики появились TTL и выгрузка наружу, она стала
            # принципиальной: kill switch, погашенный чистилкой по сроку, — это
            # ивент, воскресший сам собой, а снимок конфига, уехавший в
            # аналитическое хранилище строкой «событие event_cfg:...», — мусор
            # в отчётах. Разные сроки жизни и разные читатели: разные таблицы.
            'app_state': {
                'id': 'INTEGER PRIMARY KEY',
                'name': 'TEXT NOT NULL',       # event_cfg:<прогон>:<поле>
                'value': 'REAL NOT NULL DEFAULT 0',
                'created_at': 'REAL NOT NULL DEFAULT 0',
            },
            # Аналитика: одно событие = одна строка. Пришла на смену таблице
            # `events` (имя, число, время) — её полей не хватало ни на одно
            # продуктовое решение: по ним нельзя сказать ни откуда игрок
            # пришёл, ни какой конфиг у него стоял, ни в какой ветке
            # эксперимента он был, ни какому движению денег событие
            # соответствует. Перенос старых строк и снос старой таблицы —
            # разовая миграция analytics:events_v2 (двух систем не остаётся).
            'analytics_events': {
                'id': 'INTEGER PRIMARY KEY',
                # Ключ события, выданный на стороне записи. Нужен приёмнику
                # выгрузки: экспорт можно повторить (упала сеть, перезапустили
                # скрипт), и без внешнего ключа повтор дублирует события в
                # хранилище, а дубли в аналитике неотличимы от роста.
                'event_id': 'TEXT NOT NULL',
                'user_id': 'INTEGER NOT NULL DEFAULT 0',
                'event': 'TEXT NOT NULL',
                'value': 'REAL NOT NULL DEFAULT 0',
                # ВРЕМЯ СЕРВЕРА. Клиентскому времени тут места нет: часы на
                # телефоне врут произвольно, а событие из будущего ломает любую
                # когорту, в которую попадёт
                'created_at': 'REAL NOT NULL DEFAULT 0',
                'config_version': 'TEXT',      # cfg.CONFIG_VERSION на момент записи
                'source': 'TEXT',              # organic | link | referral
                'campaign': 'TEXT',            # код ссылки или ref:<id>
                'lang': 'TEXT',
                'country': 'TEXT',             # из заголовка обратного прокси
                'platform': 'TEXT',            # android | ios | tdesktop | web
                'variant': 'TEXT',             # ветка A/B: <эксперимент>/<ветка>
                # Стык с книгой операций. Ledger уже несёт operation_id, и
                # соединение идёт по нему, а не по параллельному ключу: иначе
                # «сколько печенек принесло событие» пришлось бы считать
                # дважды и получать два разных ответа
                'economy_operation_id': 'TEXT',
                'props': 'TEXT',               # остальные свойства, json
                # Когда строка уехала наружу. 0 = ещё не выгружена, и TTL её не
                # трогает: срок хранения не имеет права уносить данные, которых
                # никто ни разу не видел
                'exported_at': 'REAL NOT NULL DEFAULT 0',
            },
            'orders': {  # заказы пекарни: offer (выбор из 3) -> active -> done
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'slot': 'INTEGER',            # 1..3 для офферов
                'template': 'TEXT',
                'metric': 'TEXT',
                'goal': 'REAL DEFAULT 0',
                'progress': 'REAL DEFAULT 0',
                'reward_cookies': 'REAL DEFAULT 0',
                'reward_bp_xp': 'REAL DEFAULT 0',
                'status': 'TEXT DEFAULT "offer"',
                'created_at': 'REAL DEFAULT 0',
                # версия строки: клиент присылает (id, version) взятого заказа,
                # и «сдать» со старого экрана не сдаёт ДРУГОЙ заказ, который
                # успел встать на его место
                'version': 'INTEGER NOT NULL DEFAULT 1',
                # набор шаблонов, по которому заказ выписан: конфиг меняется
                # между релизами, и по этой метке видно, чей заказ разбираем
                'config_rev': 'TEXT',
                # уровень и доход на момент взятия. NULL — заказ взят до этой
                # миграции, достижимость для него пересчитывается по текущим
                'taken_level': 'INTEGER',
                'taken_income': 'REAL',
                # печеньки, вложенные в этот заказ (спавны, здания). Если заказ
                # снимает СЕРВЕР (цель стала недостижимой), они возвращаются
                'invested': 'REAL NOT NULL DEFAULT 0',
            },
            'collection': {  # альбом блестящих печенек: строка = открытый слот
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'item_level': 'INTEGER',
                'obtained_at': 'REAL DEFAULT 0',
            },
            'click_batches': {  # обработанные батчи кликов (дедуп ретраев, TTL ~1ч)
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'batch_id': 'TEXT',
                'created_at': 'REAL DEFAULT 0',
            },
            'farm': {  # здания автофарма: одна строка = тип здания у юзера
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'building_key': 'TEXT',
                'count': 'INTEGER DEFAULT 0',
            },
            'upgrades': {  # купленные одноразовые апгрейды
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'upgrade_key': 'TEXT',
            },
            'skins': {  # купленные скины
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'skin_key': 'TEXT',
            },
            'board': {  # merge-доска: одна строка = занятая клетка
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'cell': 'INTEGER',        # 0..24
                'item_level': 'INTEGER',  # уровень печеньки в клетке
                # сколько печенек фактически вложено (спавн + сумма родителей
                # при мердже) — от этого считается возврат при переплавке
                'paid': 'REAL DEFAULT 0',
            },
            'referrals': {
                'id': 'INTEGER PRIMARY KEY',
                'referrer_id': 'INTEGER',
                'referred_id': 'INTEGER UNIQUE',
                'created_at': 'REAL DEFAULT 0',
            },
            'promo_codes': {
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT UNIQUE',
                'reward_cookies': 'REAL DEFAULT 0',
                'reward_energy': 'REAL DEFAULT 0',
                'max_uses': 'INTEGER DEFAULT 0',   # 0 = безлимит
                'uses': 'INTEGER DEFAULT 0',
                'active': 'INTEGER DEFAULT 1',
                'created_at': 'REAL DEFAULT 0',
            },
            'promo_redemptions': {
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT',
                'user_id': 'INTEGER',
                'redeemed_at': 'REAL DEFAULT 0',
            },
            'sources': {  # отслеживаемые ссылки t.me/bot?startapp=src_CODE
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT UNIQUE',
                'title': 'TEXT',
                'registrations': 'INTEGER DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
            },
            'achievements': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'key': 'TEXT',
                'claimed': 'INTEGER DEFAULT 0',
            },
            'purchases': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'item_key': 'TEXT',
                'stars_amount': 'INTEGER',
                'tg_payment_id': 'TEXT',
                'status': 'TEXT DEFAULT "pending"',
                'created_at': 'REAL DEFAULT 0',
                # ЧТО ИМЕННО выдали за этот платёж, json (см. _apply_purchase_effect).
                # Возврат снимает по этой записи, а не пересчитывает: «2 часа
                # дохода» на момент покупки и сегодня — разные числа, и пересчёт
                # забирал в разы больше выданного.
                # БЕЗ DEFAULT намеренно: NULL = покупка до миграции, для неё
                # остаётся старый путь с пересчётом. DEFAULT '{}' превратил бы
                # каждый старый возврат в тихий no-op, то есть в бесплатный товар
                'granted_payload': 'TEXT',
                'granted_at': 'REAL DEFAULT 0',
                # ярлык конкретной выдачи: буст этой покупки, а не любой буст с
                # тем же ключом (boost_x2_1h и boost_x2_24h делят ключ click_x2,
                # и его же выдаёт награда за рефералов)
                'effect_instance_id': 'TEXT',
                # из какого состояния платёж уехал в 'refunded' и когда. Слепая
                # перезапись статуса стирала единственное, по чему видно, был ли
                # товар выдан вообще, — а это именно те строки, которые человек
                # разбирает руками в /api/admin/payments
                'prior_status': 'TEXT',
                'refunded_at': 'REAL DEFAULT 0',
                'refund_stars': 'INTEGER',
            },
            'boosts': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'boost_key': 'TEXT',
                'expires_at': 'REAL DEFAULT 0',
                'effect_instance_id': 'TEXT',   # какая выдача создала строку
                'source': 'TEXT',               # purchase | ref_milestone | golden
            },
            'entitlements': {  # право, выданное источником: чтобы возврат снимал СВОЁ
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'kind': 'TEXT',                # пока только 'bp_premium'
                'source': 'TEXT',              # purchase | ref_milestone | legacy
                'source_ref': 'TEXT',          # charge_id платежа (или '')
                'season_id': 'INTEGER',        # сезон, к которому относится право
                'created_at': 'REAL DEFAULT 0',
            },
            'daily_quests': {  # прогресс ежедневных заданий: строка = юзер+день+задание
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'day': 'TEXT',            # 'YYYY-MM-DD' (UTC)
                'quest_key': 'TEXT',
                'progress': 'REAL DEFAULT 0',
                'claimed': 'INTEGER DEFAULT 0',
            },
            'ref_claims': {  # забранные milestone-награды рефералки
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'milestone_key': 'TEXT',
                'claimed_at': 'REAL DEFAULT 0',
            },
            # Забранные награды батл-пасса — СТРОКАМИ, а не json-списком в users.
            # Список читался, дополнялся в питоне и записывался целиком: два
            # клейма подряд затирали друг друга, один уровень терялся и его можно
            # было забрать второй раз. Строка + UNIQUE делают клейм атомарным.
            # season_id в ключе: сезонный сброс теперь не UPDATE на всех, а просто
            # другой набор строк.
            'bp_claims': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'season_id': 'INTEGER',
                'track': 'TEXT',               # 'free' | 'premium'
                'level': 'INTEGER',
                'claimed_at': 'REAL DEFAULT 0',
            },
            'duels': {  # асинхронная дуэль 1x1: кто больше напечёт за сутки
                'id': 'INTEGER PRIMARY KEY',
                'user_a': 'INTEGER',           # создатель, ждёт соперника
                'user_b': 'INTEGER',           # присоединившийся
                'league': 'TEXT',              # подбор внутри своей лиги
                'a_start': 'REAL DEFAULT 0',   # total_earned на старте
                'b_start': 'REAL DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
                'started_at': 'REAL DEFAULT 0',
                'ends_at': 'REAL DEFAULT 0',
                'status': 'TEXT DEFAULT "waiting"',   # waiting | active | done
                'winner_id': 'INTEGER',
                'claimed_a': 'INTEGER DEFAULT 0',
                'claimed_b': 'INTEGER DEFAULT 0',
                'reward': 'REAL DEFAULT 0',
            },
            # --- экономика: книга движений и токены операций ---
            'economy_ledger': {  # НЕИЗМЕНЯЕМАЯ книга: строка = одно движение валюты
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER NOT NULL',
                'operation_id': 'TEXT NOT NULL',   # токен операции (см. economy_ops)
                'seq': 'INTEGER NOT NULL DEFAULT 0',  # номер движения внутри операции
                'currency': 'TEXT NOT NULL',       # cookies | xp | bp_xp | energy | ...
                'amount': 'REAL NOT NULL',         # со знаком: минт > 0, трата < 0
                'reason': 'TEXT NOT NULL',         # за что (daily, farm_building, ...)
                'ref_type': 'TEXT',                # на что ссылается (order, building)
                'ref_id': 'TEXT',
                'balance_after': 'REAL NOT NULL',  # баланс сразу после движения
                'counts_earned': 'INTEGER NOT NULL DEFAULT 0',  # пошло в total_earned
                'season_id': 'INTEGER',
                'external': 'INTEGER NOT NULL DEFAULT 0',  # валюта вне users (Stars)
                'created_at': 'REAL NOT NULL DEFAULT 0',
                # NaN. На SQLite он ложится в базу как NULL, и его ловит NOT
                # NULL; сравнение x = x — второй рубеж на случай, если движок
                # однажды начнёт хранить его честно. На PostgreSQL так нельзя:
                # там NaN = NaN истинно (NaN считается равным себе и большим
                # любого числа), поэтому проверка «x = x» пропустила бы его, и
                # ловить надо сравнением с самим значением 'NaN'.
                '__constraints__': {
                    "sqlite": [
                        "CHECK (amount = amount)",
                        "CHECK (balance_after = balance_after)",
                    ],
                    "postgres": [
                        "CHECK (amount <> 'NaN'::double precision)",
                        "CHECK (balance_after <> 'NaN'::double precision)",
                    ],
                    # диапазон одинаков; на PostgreSQL он же отсекает ±Infinity
                    "*": [
                        "CHECK (amount > -1e15 AND amount < 1e15)",
                        "CHECK (balance_after > -1e15 AND balance_after < 1e15)",
                    ],
                },
                # книга только дописывается: правка задним числом уничтожает
                # весь смысл сверки. Текст ошибки на обоих движках ОДИН И ТОТ ЖЕ
                # — по нему отличают запрет книги от любого другого отказа базы.
                '__after_create__': {
                    "sqlite": [
                        "CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update "
                        "BEFORE UPDATE ON economy_ledger "
                        "BEGIN SELECT RAISE(ABORT, 'economy_ledger is append-only'); END",
                        "CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete "
                        "BEFORE DELETE ON economy_ledger "
                        "BEGIN SELECT RAISE(ABORT, 'economy_ledger is append-only'); END",
                    ],
                    # CREATE TRIGGER IF NOT EXISTS в PostgreSQL нет, поэтому
                    # DROP + CREATE: так же идемпотентно, но без привязки к
                    # версии сервера (OR REPLACE появился только в 14)
                    "postgres": [
                        "CREATE OR REPLACE FUNCTION cookie_ledger_append_only() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                        "RAISE EXCEPTION 'economy_ledger is append-only'; END $$",
                        "DROP TRIGGER IF EXISTS trg_ledger_no_update ON economy_ledger",
                        "CREATE TRIGGER trg_ledger_no_update BEFORE UPDATE "
                        "ON economy_ledger FOR EACH ROW "
                        "EXECUTE FUNCTION cookie_ledger_append_only()",
                        "DROP TRIGGER IF EXISTS trg_ledger_no_delete ON economy_ledger",
                        "CREATE TRIGGER trg_ledger_no_delete BEFORE DELETE "
                        "ON economy_ledger FOR EACH ROW "
                        "EXECUTE FUNCTION cookie_ledger_append_only()",
                    ],
                },
            },
            'economy_ops': {  # токен операции: ретрай не выдаёт награду второй раз
                'id': 'INTEGER PRIMARY KEY',
                'operation_id': 'TEXT NOT NULL',
                'user_id': 'INTEGER NOT NULL',
                'kind': 'TEXT NOT NULL',
                'status': 'TEXT NOT NULL DEFAULT "open"',
                'response': 'TEXT',        # ответ первого запроса, отдаётся ретраю
                'created_at': 'REAL NOT NULL DEFAULT 0',
            },
            'economy_opening': {  # входящие остатки: с чем игрок пришёл в книгу
                'user_id': 'INTEGER PRIMARY KEY',
                'total_earned': 'REAL NOT NULL DEFAULT 0',
                'season_earned': 'REAL NOT NULL DEFAULT 0',
                'captured_at': 'REAL NOT NULL DEFAULT 0',
            },
            'job_runs': {  # расписание фоновых задач, общее для всех процессов
                'job_key': 'TEXT PRIMARY KEY',
                'last_run_at': 'REAL NOT NULL DEFAULT 0',   # когда взяли в работу
                'last_ok_at': 'REAL NOT NULL DEFAULT 0',    # когда дошли до конца
                'interval_s': 'REAL NOT NULL DEFAULT 0',    # ожидаемый период
                'runs': 'INTEGER NOT NULL DEFAULT 0',
                'fails': 'INTEGER NOT NULL DEFAULT 0',
                'owner': 'TEXT',                            # host:pid последнего запуска
                'last_error': 'TEXT',
            },
            # Очередь пушей: планировщик КЛАДЁТ задачу, воркер её ОТПРАВЛЯЕТ.
            # Раньше отправка была одним проходом по всей таблице игроков раз в
            # 15 минут, и состояния у неё не было нигде: 100k пушей при 25 msg/s
            # это больше часа, то есть проход не укладывается в собственный
            # интервал, а всё, до чего он не дошёл, теряется вместе с процессом.
            # Строка здесь — единица работы: её видно, её можно повторить, и
            # ровно она отвечает на вопрос «почему этот игрок получил (или не
            # получил) это сообщение».
            'notification_queue': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER NOT NULL',
                'kind': 'TEXT NOT NULL',            # recipe_ready, duel_result, ...
                # категория частотного лимита: kind'ов на неё несколько
                # (recipe_ready и recipe_burning — одна категория 'recipe').
                # Отдельной колонкой, а не списком в коде: лимит считается
                # запросом по уже отправленному, и группировать надо в БД
                'category': 'TEXT NOT NULL DEFAULT "other"',
                'payload': 'TEXT',                  # json: параметры текста и цель диплинка
                'scheduled_at': 'REAL NOT NULL DEFAULT 0',
                # scheduled -> sending -> sent -> opened; отказы: failed (кончились
                # попытки), blocked (бота заблокировали), cancelled (событие
                # протухло до отправки). 'sending' — заявка воркера: она нужна,
                # чтобы два воркера не взяли одну строку, и снимается по lease,
                # если воркера убили посреди отправки
                'status': 'TEXT NOT NULL DEFAULT "scheduled"',
                'dedup_key': 'TEXT NOT NULL',
                'attempts': 'INTEGER NOT NULL DEFAULT 0',
                'claimed_at': 'REAL NOT NULL DEFAULT 0',
                'claimed_by': 'TEXT',               # host:pid воркера
                'sent_at': 'REAL NOT NULL DEFAULT 0',
                'opened_at': 'REAL NOT NULL DEFAULT 0',
                'last_error': 'TEXT',
                'created_at': 'REAL NOT NULL DEFAULT 0',
            },
            'season_results': {  # снапшот топа прошедших сезонов + выданные награды
                'id': 'INTEGER PRIMARY KEY',
                'season_id': 'INTEGER',
                'user_id': 'INTEGER',
                'rank': 'INTEGER',
                'earned': 'REAL DEFAULT 0',
                'reward_cookies': 'REAL DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
            },
        }

        # DDL и разовые миграции идут без предохранителя по времени: создание
        # уникального индекса по таблице игроков — законно долгая работа
        self._set_statement_timeout(ms=0)
        # Схема правится ПОД МЕЖПРОЦЕССНЫМ ЗАМКОМ целиком, а не по миграции:
        # гонка есть у каждой из них, а не только у переноса аналитики
        with self._migration_lock():
            self._migrate_schema(db_file)
        self._set_statement_timeout(ms=self.PG_STATEMENT_TIMEOUT_MS)

    def _migrate_schema(self, db_file: str):
        """Привести схему к текущей: таблицы, колонки, индексы, разовые миграции.

        Отдельным методом — чтобы у замка миграций была ровно одна точка входа.
        Всё, что здесь делается, обязано быть идемпотентным: сюда заходит каждый
        процесс на старте, просто по очереди.
        """
        # журнал применённых миграций. Создаётся ДО всего остального: на него
        # опирается и _auto_migrate (__after_create__), и дедуп
        self.cursor.execute(
            self._create_table_sql("schema_migrations", self.MIGRATIONS_SCHEMA))
        self._auto_migrate()
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_board_user ON board(user_id)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ach_user ON achievements(user_id)")
        # UNIQUE обязателен: _ensure_quest_rows полагается на INSERT OR IGNORE
        self.cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dq_user_day "
            "ON daily_quests(user_id, day, quest_key)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_season_earned ON users(season_earned)")
        # аналитика: воронка строится по (событие, окно), разбор жалобы — по
        # игроку. Индексы те же, что были у events, плюс два новых
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_name "
            "ON analytics_events(event, created_at)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_user "
            "ON analytics_events(user_id, created_at)")
        # Курсор выгрузки: «отдай следующую тысячу невыгруженных по возрастанию
        # id». Частичный индекс понимают оба движка, и он размером с очередь, а
        # не с таблицей: после выгрузки строка из него выпадает
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_pending "
            "ON analytics_events(id) WHERE exported_at = 0")
        # соединение с книгой операций: «что игрок получил за это событие».
        # Тоже частичный — operation_id есть у меньшинства событий
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_op "
            "ON analytics_events(economy_operation_id) "
            "WHERE economy_operation_id IS NOT NULL")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id, status)")
        # boosts читается из click_multiplier на КАЖДЫЙ батч кликов, под
        # BEGIN IMMEDIATE — без индекса это full scan в самой горячей ручке
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_boosts_user ON boosts(user_id, expires_at)")
        # ref_count зовётся из merge_cells_unlocked_for на каждый /api/state;
        # UNIQUE висел только на referred_id, поиск по referrer_id сканировал всё
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id)")
        # finalize_seasons делает SELECT DISTINCT season_id на каждый запрос
        # четырёх ручек; лидерборд считает по (season_id, level) размер лиги
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_season ON users(season_id, level)")
        # Порядок лидерборда целиком (см. LEADERBOARD_ORDER): топ-100 лиги
        # снимается первыми подходящими строками индекса, без сортировки всей
        # таблицы сезона. Лиги отдельной колонки не имеют — это диапазон
        # уровней (game_config.LEAGUES), поэтому в индексе на её месте стоит
        # level, по нему же идёт и отбор лиги, и второй ключ сортировки.
        # user_id последним ключом обязателен: без него при равных заработке и
        # уровне порядок недетерминирован и место игрока прыгает на каждый F5.
        # DESC в объявлении индекса понимают оба движка (SQLite с 3.3,
        # PostgreSQL всегда), поэтому диалект тут не разветвляется.
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_leaderboard "
            "ON users(season_id, season_earned DESC, level DESC, user_id)")
        # fulfill_pending перебирает зависшие покупки на каждом /auth
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_purchases_user ON purchases(user_id, status)")
        # сверка баланса читает книгу по игроку, разбор инцидента — по причине
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_user ON economy_ledger(user_id, created_at)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ledger_reason ON economy_ledger(reason, created_at)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ops_user ON economy_ops(user_id, created_at)")
        # чистилки ходят по возрасту строки, а обе таблицы — самые быстрорастущие
        # в базе: событие на каждое открытие приложения, токен на каждый клейм
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ops_created ON economy_ops(created_at)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_analytics_created "
            "ON analytics_events(created_at)")
        # выборка кандидатов на пуш: пробег по всей таблице юзеров каждую минуту
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_notify "
            "ON users(notify_blocked, last_seen_at, last_notified_at)")
        # Очередь пушей. Первый индекс — самый горячий запрос всей системы
        # уведомлений: воркер берёт партию по (статус, время) каждую секунду.
        # Без него это скан очереди, а очередь на сотне тысяч игроков больше
        # самой таблицы игроков.
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notify_due "
            "ON notification_queue(status, scheduled_at)")
        # частотный лимит: сколько пушей игрок уже получил за сутки и по каким
        # категориям. Спрашивается перед КАЖДОЙ отправкой
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notify_user_sent "
            "ON notification_queue(user_id, sent_at)")
        # чистилка очереди ходит по возрасту строки
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_notify_created "
            "ON notification_queue(created_at)")
        # Планировщик пушей: три выборки кандидатов, каждая раз в 15 минут по
        # всей таблице игроков. Частичный индекс по закваскам понимают оба
        # движка (SQLite с 3.8, PostgreSQL всегда), и он невелик: закваска
        # стоит у единиц процентов аудитории одновременно.
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_recipe "
            "ON users(recipe_started_at) WHERE recipe_key IS NOT NULL")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_farm_idle "
            "ON users(notify_blocked, farm_collected_at)")
        # дуэли и заказы планировщик смотрит по статусу, а не по игроку:
        # существующие индексы обоих таблиц начинаются с user_id и для такого
        # запроса бесполезны
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_duels_status ON duels(status, ends_at)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status, created_at)")
        self._dedupe_and_unique(db_file)
        self._install_invariants()
        self.connection.commit()

    # Наборы колонок, которые обязаны быть уникальными; код и так это проверяет,
    # но параллельные запросы могли бы создать дубли — БД теперь не даст.
    # Ключ словаря = имя набора; поля:
    #   cols   — колонки индекса (обязательно)
    #   table  — таблица, если она называется не так, как ключ
    #   where  — условие ЧАСТИЧНОГО уникального индекса (одинаково на обоих
    #            движках). Дедуп обязан нести то же условие, иначе он удалит
    #            строки, которые индекс бы разрешил
    #   index  — имя индекса, если оно не выводится из table+cols
    UNIQUES = {
        "board": {"cols": ("user_id", "cell")},
        "farm": {"cols": ("user_id", "building_key")},
        "upgrades": {"cols": ("user_id", "upgrade_key")},
        "skins": {"cols": ("user_id", "skin_key")},
        "achievements": {"cols": ("user_id", "key")},
        "promo_redemptions": {"cols": ("user_id", "code")},
        "ref_claims": {"cols": ("user_id", "milestone_key")},
        # награда пасса забирается один раз за сезон
        "bp_claims": {"cols": ("user_id", "season_id", "track", "level")},
        # заявка в очереди дуэлей — одна на игрока. Два /duel/find вплотную
        # вставляли по строке, и игрок стоял в очереди дважды: его забирали два
        # разных соперника, а сам он видел только одну из дуэлей
        "duels_waiting": {"cols": ("user_a",), "table": "duels",
                          "where": "status = 'waiting'",
                          "index": "uq_duels_waiting"},
        # активный заказ — один на игрока, и это факт БАЗЫ, а не соглашение
        # кода: два /orders/take вплотную проходили обе проверки и оставляли
        # игрока с двумя активными заказами, из которых прогресс шёл обоим, а
        # видел он один. Второй оффер того же слота — та же история на выписке
        "orders_active": {"cols": ("user_id",), "table": "orders",
                          "where": "status = 'active'",
                          "index": "uq_orders_active"},
        "orders_offer": {"cols": ("user_id", "slot"), "table": "orders",
                         "where": "status = 'offer'",
                         "index": "uq_orders_offer"},
        "season_results": {"cols": ("season_id", "user_id")},
        "click_batches": {"cols": ("user_id", "batch_id")},
        "collection": {"cols": ("user_id", "item_level")},
        # один Stars-платёж — одна запись (charge_id уникален, NULL допустим);
        # имя индекса историческое, менять нельзя — иначе создастся второй
        "purchases": {"cols": ("tg_payment_id",),
                      "where": "tg_payment_id IS NOT NULL",
                      "index": "uq_purchases_charge"},
        # право от одного источника выдаётся один раз на сезон: повторная
        # выдача не должна плодить строки, иначе возврат снимет одну, а флаг
        # останется поднятым второй
        "entitlements": {"cols": ("user_id", "kind", "source", "source_ref",
                                  "season_id")},
        # книга: повтор операции не создаёт второе движение
        "economy_ledger": {"cols": ("operation_id", "currency", "seq")},
        # токен операции: на PostgreSQL второй worker заблокируется на этом
        # индексе, дождётся коммита первого и прочитает готовый ответ
        "economy_ops": {"cols": ("operation_id",)},
        # Имя ключа состояния — одно на всю базу. Уникальность тут несёт
        # смысл: снимок конфига ивента пишется гонкой двух воркеров через
        # INSERT ... WHERE NOT EXISTS, и без индекса они оба вставили бы строку
        "app_state": {"cols": ("name",), "index": "uq_app_state_name"},
        # Ключ события аналитики. Уникальность нужна не игре, а ПРИЁМНИКУ
        # выгрузки: он дедуплицирует повторную заливку по event_id, и если
        # ключи начнут повторяться у нас, дедупликация съест разные события
        "analytics_events": {"cols": ("event_id",),
                             "index": "uq_analytics_event_id"},
        # Одно событие — одно сообщение. Планировщик идемпотентен НЕ потому, что
        # помнит, кого уже разобрал, а потому что ключ события уникален: проход
        # раз в 15 минут видит один и тот же готовый рецепт до самого сбора, и
        # без этого индекса игрок получал бы про него пуш каждые 15 минут.
        # Ключ несёт и сам момент события (recipe_started_at, id дуэли, номер
        # сезона) — иначе следующая закваска того же игрока считалась бы той же
        "notification_queue": {"cols": ("dedup_key",),
                               "index": "uq_notification_dedup"},
    }

    # Схлопывание дублей С УЧЁТОМ ДАННЫХ: там, где лишнюю строку нельзя просто
    # выбросить, сначала переносим её содержимое на выжившую (MIN(id))
    DEDUPE_MERGE = {
        # ферма: у выжившей строки — суммарное количество зданий
        "farm": "UPDATE farm SET count = (SELECT SUM(f2.count) FROM farm f2 "
                " WHERE f2.user_id = farm.user_id AND f2.building_key = farm.building_key) "
                "WHERE id IN (SELECT MIN(id) FROM farm GROUP BY user_id, building_key "
                "             HAVING COUNT(*) > 1)",
        # доска: в клетке выживает печенька максимального уровня
        "board": "DELETE FROM board WHERE EXISTS (SELECT 1 FROM board b2 "
                 " WHERE b2.user_id = board.user_id AND b2.cell = board.cell "
                 " AND (b2.item_level > board.item_level "
                 "      OR (b2.item_level = board.item_level AND b2.id < board.id)))",
        # ачивки: если хоть один дубль заклеймлен — сохраняем claimed=1
        "achievements":
            "UPDATE achievements SET claimed = (SELECT MAX(a2.claimed) FROM achievements a2 "
            " WHERE a2.user_id = achievements.user_id AND a2.key = achievements.key) "
            "WHERE id IN (SELECT MIN(id) FROM achievements GROUP BY user_id, key "
            "             HAVING COUNT(*) > 1)",
        # платежи: fulfilled важнее paid — переносим статус на выжившую строку
        "purchases":
            "UPDATE purchases SET status = 'fulfilled' "
            "WHERE tg_payment_id IS NOT NULL AND status != 'fulfilled' AND EXISTS "
            "(SELECT 1 FROM purchases p2 WHERE p2.tg_payment_id = purchases.tg_payment_id "
            " AND p2.status = 'fulfilled')",
    }

    @staticmethod
    def _unique_parts(key: str, spec: dict):
        """(таблица, колонки, where, имя индекса) для набора уникальности."""
        table = spec.get("table", key)
        cols = spec["cols"]
        name = spec.get("index") or f"uq_{table}_{'_'.join(cols)}"
        return table, cols, spec.get("where"), name

    def _has_duplicates(self) -> bool:
        for key, spec in self.UNIQUES.items():
            table, cols, where, _ = self._unique_parts(key, spec)
            cond = f"WHERE {where} " if where else ""
            if self.q1(f"SELECT 1 AS x FROM {table} {cond}"
                       f"GROUP BY {', '.join(cols)} HAVING COUNT(*) > 1 LIMIT 1"):
                return True
        return False

    def _backup(self, db_file: str):
        """Копия базы перед разрушительной миграцией.

        Дедуп УДАЛЯЕТ строки, поэтому копия обязательна. На PostgreSQL её
        делает pg_dump, и если его нет — старт отменяется: удалять живые данные
        без копии хуже, чем не подняться."""
        if self.DIALECT == "postgres":
            path = self.snapshot(keep=10 ** 6)
            if not path:
                raise RuntimeError(
                    "Найдены дубли, нужен дедуп, но снимок сделать нечем "
                    "(pg_dump не найден). Удалять строки без копии нельзя — "
                    "поставь postgresql-client и запусти снова.")
            print(f"[*] Миграция: найдены дубли, бэкап сохранён в {path}")
            return
        path = f"{db_file}.pre-dedup-{int(time.time())}.bak"
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
            print(f"[*] Миграция: найдены дубли, бэкап сохранён в {path}")
        finally:
            dest.close()

    def _backups_folder(self) -> str:
        """Куда складывать снимки. У PostgreSQL файла базы нет, поэтому папка
        считается от рабочего каталога процесса — того же, где лежит лог."""
        base = os.path.dirname(os.path.abspath(self.db_file))
        folder = os.path.join(base, "backups")
        os.makedirs(folder, exist_ok=True)
        return folder

    def snapshot(self, keep: int = 7) -> str | None:
        """Горячий бэкап базы. Старые снимки чистим, оставляя последние `keep`.

        Никаких бэкапов не было вообще — единственная копия делалась один раз
        перед dedup-миграцией. На SQLite снимок делает backup API (корректно
        работает на живой базе в WAL-режиме, останавливать сервис не нужно), на
        PostgreSQL — pg_dump."""
        if self.DIALECT == "postgres":
            return self._snapshot_postgres(keep)
        if self.db_file == ":memory:":
            return None
        folder = self._backups_folder()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = os.path.join(folder, f"{os.path.basename(self.db_file)}.{stamp}.bak")
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
        finally:
            dest.close()
        self._prune_snapshots(folder, os.path.basename(self.db_file) + ".",
                              ".bak", keep)
        return path

    @staticmethod
    def _prune_snapshots(folder: str, prefix: str, suffix: str, keep: int):
        # Чистим ТОЛЬКО снимки этой базы: в одной папке легко оказываются файлы
        # с другим префиксом (переименовали базу, рядом второй экземпляр), и
        # тогда лексикографический порядок перестаёт быть хронологическим —
        # свежий снимок уезжает в начало списка и удаляется сразу после
        # создания. Внутри одного префикса дальше идёт UTC-метка в сортируемом
        # формате, поэтому порядок имён и есть порядок времени.
        old = sorted(f for f in os.listdir(folder)
                     if f.startswith(prefix) and f.endswith(suffix))
        for name in old[:-keep] if keep > 0 else old:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass

    def _snapshot_postgres(self, keep: int) -> str | None:
        """Снимок через pg_dump в custom-формате (сжат, восстанавливается
        pg_restore выборочно по таблицам).

        Пароль передаётся ПЕРЕМЕННОЙ ОКРУЖЕНИЯ, а не в URL аргументом: argv
        видно всей машине в ps, и строка подключения с паролем попала бы в
        любой снимок процессов, включая тот, что приложат к тикету."""
        exe = shutil.which("pg_dump")
        if not exe:
            print("[!] Бэкап пропущен: pg_dump не найден в PATH")
            return None
        p = urllib.parse.urlparse(self.url)
        name = urllib.parse.unquote(p.path.lstrip("/")) or "postgres"
        folder = self._backups_folder()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = os.path.join(folder, f"{name}.{stamp}.dump")
        args = [exe, "--format=custom", "--no-owner", "--no-privileges",
                "--file", path]
        if p.hostname:
            args += ["--host", p.hostname]
        if p.port:
            args += ["--port", str(p.port)]
        if p.username:
            args += ["--username", urllib.parse.unquote(p.username)]
        args.append(name)
        env = dict(os.environ)
        if p.password:
            env["PGPASSWORD"] = urllib.parse.unquote(p.password)
        # timeout: снимок обязан закончиться, иначе задача планировщика
        # держала бы замок и следующий бэкап не случился бы никогда
        res = subprocess.run(args, env=env, capture_output=True, text=True,
                             timeout=3600)
        if res.returncode != 0:
            # stderr pg_dump пароля не содержит — он не в argv и не в выводе
            raise RuntimeError(
                f"pg_dump вернул {res.returncode}: {res.stderr.strip()[:500]}")
        self._prune_snapshots(folder, name + ".", ".dump", keep)
        return path

    # ---------- диалект: DDL ----------

    def _ddl(self, col: str, spec: str) -> str:
        """Объявление колонки в терминах текущего движка.

        Схема выше написана на типах SQLite — это исходник, и переписывать её
        под второй движок нельзя: тогда пришлось бы держать две схемы и следить,
        чтобы они не разъехались. Перевод делается здесь, в одном месте."""
        if self.DIALECT == "sqlite":
            return f"{col} {spec}"
        kind, _, rest = spec.partition(" ")
        # DEFAULT "en": в SQLite двойные кавычки — строковый литерал, в
        # PostgreSQL это ИМЯ ОБЪЕКТА, и такой DEFAULT падает с «column "en"
        # does not exist». Одинарные кавычки понимают оба движка.
        rest = rest.replace('"', "'")
        if kind == "INTEGER" and rest.startswith("PRIMARY KEY") and col == "id":
            # INTEGER PRIMARY KEY в SQLite — псевдоним rowid, то есть выданный
            # базой автоинкремент; ближайший аналог — BIGSERIAL. Условие на
            # имя колонки обязательно: economy_opening.user_id объявлен
            # INTEGER PRIMARY KEY, но значение туда кладёт код (это telegram id),
            # и последовательность там подставляла бы свои числа поверх чужих.
            return f"{col} BIGSERIAL {rest}"
        return f"{col} {self.PG_TYPES.get(kind, kind)} {rest}".rstrip()

    def _create_table_sql(self, table: str, columns: dict,
                          constraints=()) -> str:
        parts = [self._ddl(col, spec) for col, spec in columns.items()]
        parts += list(constraints)
        return f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(parts)})"

    def _columns(self, table: str) -> list[str]:
        """Имена колонок существующей таблицы (пусто, если таблицы нет)."""
        if self.DIALECT == "sqlite":
            self.cursor.execute(f"PRAGMA table_info({table})")
            return [row["name"] for row in self.cursor.fetchall()]
        # current_schema(), а не 'public': база может лежать в своей схеме
        # (search_path в строке подключения), и жёсткое 'public' нашло бы
        # чужую таблицу того же имени или не нашло бы своей
        return [r["column_name"] for r in self.q(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? AND table_schema = current_schema()", (table,))]

    def _dialect_list(self, spec: dict, key: str) -> list[str]:
        """Значение `__constraints__`/`__after_create__` для текущего движка.

        Значением может быть список (годится обоим) или dict по диалектам с
        необязательным ключом '*' для общей части."""
        value = spec.get(key)
        if not value:
            return []
        if isinstance(value, dict):
            return list(value.get(self.DIALECT, ())) + list(value.get("*", ()))
        return list(value)

    # ---------- миграции ----------

    def _wait_for_lock(self, grab) -> None:
        """Дождаться замка, повторяя попытку `grab()` до победы или потолка.

        Ожидание вынесено из способа блокировки: и pg_try_advisory_lock, и
        файловая блокировка умеют отвечать только «получилось или нет», а ждать
        надо одинаково — с сообщением в лог (иначе молчащий старт выглядит
        зависшим) и с потолком по времени."""
        if grab():
            return
        log.info("миграции уже идут в соседнем процессе, ждём замок (до %.0f с)",
                 self.MIGRATION_LOCK_TIMEOUT)
        started = time.monotonic()
        while time.monotonic() - started < self.MIGRATION_LOCK_TIMEOUT:
            time.sleep(0.2)
            if grab():
                log.info("замок миграций получен через %.1f с",
                         time.monotonic() - started)
                return
        raise RuntimeError(
            f"Замок миграций занят дольше {self.MIGRATION_LOCK_TIMEOUT:.0f} с — "
            f"процесс не стартует. Работать по схеме, которую прямо сейчас "
            f"меняет соседний процесс, нельзя; проверь, не завис ли он.")

    @contextmanager
    def _migration_lock(self):
        """Межпроцессный замок вокруг ВСЕГО прогона миграций.

        Зачем он нужен. Слой БД поднимается в КАЖДОМ процессе, а при
        WEB_CONCURRENCY > 1 воркеры стартуют одновременно. Отметка в
        schema_migrations от гонки не спасает: она пишется ПОСЛЕ выполнения, то
        есть все воркеры одинаково видят «не применено» и лезут мигрировать
        разом. Для переноса аналитики это означало бы DROP TABLE events из-под
        читающего соседа и задвоенные строки в analytics_events.

        Проигравший ЖДЁТ, а не проскакивает вперёд: работать по схеме, которой
        сейчас меняют форму, нельзя — первый же запрос ушёл бы в таблицу,
        которой через миллисекунду не станет. Дождавшись, он перечитывает
        отметки (см. _migration) и просто ничего не делает.

        Что будет, если процесс с замком убьют. Замок снимает операционная
        система: на PostgreSQL advisory-замок сессионный и умирает вместе с
        соединением, на SQLite блокировка файла — вместе с дескриптором. Ни в
        одном из случаев не остаётся «залипшего» замка, который пришлось бы
        снимать руками; следующий старт спокойно его подберёт и догонит
        недоделанную миграцию (она для этого идемпотентна).
        """
        if self.DIALECT == "postgres":
            # statement_timeout к этому моменту уже снят (см. __init__), так что
            # ожидание замка не убьётся предохранителем
            key = self.MIGRATION_LOCK_KEY
            self._wait_for_lock(
                lambda: bool(self.q1("SELECT pg_try_advisory_lock(?) AS ok",
                                     (key,))["ok"]))
            try:
                yield
            finally:
                # Снимаем явно, а не полагаемся на конец сессии: соединение
                # живёт всё время жизни потока, и незакрытый замок держал бы
                # следующие старты этой же машины
                self.q1("SELECT pg_advisory_unlock(?) AS ok", (key,))
            return

        if self.db_file == ":memory:":
            # у in-memory базы каждый процесс живёт в своей копии — делить нечего
            yield
            return

        try:                       # POSIX
            import fcntl
            msvcrt = None
        except ImportError:        # Windows
            import msvcrt
            fcntl = None

        # Блокируется отдельный файл, а не сама база: блокировки самой базы
        # берёт и снимает sqlite на каждом операторе, и опереться на них нельзя
        path = f"{self.db_file}.migrate.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)

        def grab() -> bool:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                return False       # занято другим процессом
            return True

        try:
            self._wait_for_lock(grab)
            yield
        finally:
            # закрытие дескриптора снимает и блокировку — на обоих семействах
            # ОС и при любом выходе, включая исключение и kill
            os.close(fd)

    def _migration(self, name: str) -> bool:
        """True, если миграция ещё не применялась к этой базе.

        Выполнив тело, обязательно позвать _mark(name). Разово — потому что
        дедуп РАЗРУШИТЕЛЕН: он удаляет строки. Раньше все десять DELETE'ов
        выполнялись на каждом импорте, и любой новый частичный индекс означал
        бы удаление живых данных при каждом старте."""
        return not self.q1(
            "SELECT 1 AS x FROM schema_migrations WHERE name = ?", (name,))

    def _mark(self, name: str):
        self.exec("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?) "
                  "ON CONFLICT (name) DO NOTHING", (name, time.time()))

    def _dedupe_and_unique(self, db_file: str):
        """Разово схлопывает дубли С УЧЁТОМ ДАННЫХ, затем вешает UNIQUE-индексы.

        Дедуп идёт один раз на набор уникальности (журнал schema_migrations),
        создание индексов — на каждом старте, но оно идемпотентно."""
        pending = [k for k in self.UNIQUES if self._migration(f"dedupe:{k}")]
        if pending and db_file != ":memory:" and self._has_duplicates():
            self._backup(db_file)

        for key in pending:
            table, cols, where, _ = self._unique_parts(key, self.UNIQUES[key])
            merge = self.DEDUPE_MERGE.get(key)
            if merge:
                self.cursor.execute(merge)
            # условие частичного индекса дублируется в обе половины запроса:
            # и в отбор удаляемых строк, и в подзапрос выживших
            pre = f"{where} AND " if where else ""
            sub = f"WHERE {where} " if where else ""
            self.cursor.execute(
                f"DELETE FROM {table} WHERE {pre}id NOT IN "
                f"(SELECT MIN(id) FROM {table} {sub}GROUP BY {', '.join(cols)})")
            self._mark(f"dedupe:{key}")

        for key, spec in self.UNIQUES.items():
            table, cols, where, name = self._unique_parts(key, spec)
            tail = f" WHERE {where}" if where else ""
            try:
                self.cursor.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                    f"ON {table}({', '.join(cols)}){tail}")
            except INTEGRITY_ERRORS as e:
                # дедуп уже отработал, значит дубли появились ПОСЛЕ него —
                # это баг в коде, а не наследие. Молча удалять живые строки
                # нельзя, поэтому старт отменяется
                raise RuntimeError(
                    f"В таблице {table} есть дубли по {cols}, уникальный индекс "
                    f"{name} не создаётся. Дедуп уже применялся — разберись с "
                    f"причиной, автоматически удалять строки небезопасно.") from e

    def _install_invariants(self):
        """Серверные инварианты на балансы.

        users — существующая таблица, а CHECK нельзя добавить через ALTER, так
        что роль ограничения играет триггер. Ловит не игровую ситуацию, а
        арифметику, вышедшую из-под контроля: NaN (в SQLite он ложится как
        NULL), бесконечность, переполнение. Пол в -1e6, а не в нуле: откат
        покупки имеет право увести баланс в ноль, остаток уходит в cookie_debt,
        и небольшой минус тут не аварийная ситуация, а запас на округления."""
        if not self._migration("invariants:users_balance"):
            return
        if self.DIALECT == "sqlite":
            self.cursor.execute(
                "CREATE TRIGGER IF NOT EXISTS trg_users_balance_sane "
                "BEFORE UPDATE OF cookies ON users "
                "WHEN NEW.cookies IS NULL OR NEW.cookies < -1e6 OR NEW.cookies > 1e15 "
                "BEGIN SELECT RAISE(ABORT, 'balance_insane'); END")
        else:
            # На PostgreSQL условие переезжает внутрь функции: WHEN там тоже
            # есть, но NaN приходится проверять сравнением с 'NaN' — «x <> x»
            # для него ложно, а вот «x = x» истинно, и обычная проверка на
            # вменяемость его бы не заметила. Текст ошибки тот же —
            # 'balance_insane', по нему тесты и обработчики отличают инвариант.
            self.cursor.execute(
                "CREATE OR REPLACE FUNCTION cookie_users_balance_sane() "
                "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                "IF NEW.cookies IS NULL "
                "OR NEW.cookies = 'NaN'::double precision "
                "OR NEW.cookies < -1e6 OR NEW.cookies > 1e15 THEN "
                "RAISE EXCEPTION 'balance_insane'; END IF; "
                "RETURN NEW; END $$")
            self.cursor.execute(
                "DROP TRIGGER IF EXISTS trg_users_balance_sane ON users")
            self.cursor.execute(
                "CREATE TRIGGER trg_users_balance_sane "
                "BEFORE UPDATE OF cookies ON users FOR EACH ROW "
                "EXECUTE FUNCTION cookie_users_balance_sane()")
        self._mark("invariants:users_balance")

    def _auto_migrate(self):
        """ Умная система: создает таблицы или добавляет новые столбцы на лету """
        for table_name, spec in self.tables_schema.items():
            # ключи с двумя подчёркиваниями — не колонки: __constraints__
            # дописываются в CREATE TABLE, __after_create__ выполняется разово
            columns = {k: v for k, v in spec.items() if not k.startswith("__")}
            self.cursor.execute(self._create_table_sql(
                table_name, columns,
                self._dialect_list(spec, "__constraints__")))

            existing_columns = self._columns(table_name)

            for col_name, col_type in columns.items():
                if col_name not in existing_columns:
                    print(f"[*] Миграция: Добавлен новый столбец {col_name} в {table_name}")
                    self.cursor.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN "
                        f"{self._ddl(col_name, col_type)}")

            after = self._dialect_list(spec, "__after_create__")
            key = f"after_create:{table_name}"
            if after and self._migration(key):
                for stmt in after:
                    self.cursor.execute(stmt)
                self._mark(key)

        if self._migration("analytics:events_v2"):
            self._migrate_events_to_analytics()
            self._mark("analytics:events_v2")
        if self._migration("backfill_best_item_level"):
            self._backfill_best_item_level()
            self._mark("backfill_best_item_level")
        if self._migration("backfill_bp_claims"):
            self._backfill_bp_claims()
            self._mark("backfill_bp_claims")
        if self._migration("backfill_entitlements"):
            self._backfill_entitlements()
            self._mark("backfill_entitlements")
        self.connection.commit()

    def _migrate_events_to_analytics(self):
        """Переносит старую таблицу `events` в `analytics_events` и сносит её.

        Смысл в последнем шаге. Оставить обе таблицы означало бы две системы
        аналитики: половина кода пишет в одну, половина в другую, а вопрос «что
        случилось на прошлой неделе» получает два ответа. Поэтому старая
        таблица именно УДАЛЯЕТСЯ, и делается это после копирования строк.

        Полей у старых строк меньше — источника, платформы, варианта и версии
        конфига в них не было. Они и остаются пустыми: выдумать их задним
        числом нельзя, а поставить сегодняшнюю версию конфига месячной строке
        значит соврать ровно в том поле, ради которого версия и заводилась.

        event_id выводится из старого id ('legacy:<id>'), а не генерируется:
        повторный прогон миграции (восстановились из копии, накатили заново) не
        должен заливать приёмнику те же события под новыми ключами. Конкатенация
        `||` — общий синтаксис обоих движков.

        exported_at = 0: старые строки наружу не уезжали ни разу, и первая же
        выгрузка обязана их забрать."""
        if "event" not in self._columns("events"):
            return                       # свежая база: переносить нечего
        # Сначала — то, что аналитикой никогда не было: снимки конфига ивентов
        # и отметки kill switch жили в этой же таблице. Уехав в аналитику, они
        # были бы удалены её TTL (то есть ивент воскрес бы сам) и выгружены
        # наружу как события, которых не было.
        # NOT EXISTS — на случай, если прошлый прогон убили посреди переноса:
        # уникального индекса на app_state.name в этот момент ещё нет (его
        # вешает _dedupe_and_unique, он идёт после миграций), и без проверки
        # повторный старт вставил бы снимок конфига второй раз
        state = self.exec(
            "INSERT INTO app_state (name, value, created_at) "
            "SELECT event, COALESCE(value, 0), COALESCE(created_at, 0) "
            "FROM events WHERE event LIKE ? AND NOT EXISTS "
            "(SELECT 1 FROM app_state s WHERE s.name = events.event)",
            (EVENT_CFG_PREFIX,))
        moved = self._copy_events_batched()
        # DROP, а не переименование: имя `events` не должно остаться доступным
        # ни коду, ни руке в psql — иначе первый же ручной INSERT воскресит
        # вторую систему
        self.cursor.execute("DROP TABLE IF EXISTS events")
        print(f"[*] Миграция: аналитика переехала в analytics_events, "
              f"перенесено строк {moved}, состояние ивентов в app_state "
              f"({state}), старая таблица events удалена")

    def _copy_events_batched(self) -> int:
        """Переносит строки events в analytics_events ПАЧКАМИ, с прогрессом.

        Одним оператором по всей таблице этого делать нельзя. events росла по
        строке на каждое открытие приложения и жила 30 дней, то есть на бою в
        ней миллионы строк: такой INSERT ... SELECT — это минуты блокирующей
        записи прямо в старте процесса, без единой строчки в логе. Пачками
        старт хотя бы не выглядит зависшим, а база успевает дышать между ними
        (каждая пачка коммитится сама — соединение в автокоммите).

        Идемпотентность держится на event_id ('legacy:<id>'): убитый на
        середине перенос догоняется повторным прогоном, а уже скопированные
        строки отсекает ON CONFLICT DO NOTHING. Ради этого уникальный индекс
        создаётся ЗДЕСЬ, а не позже в _dedupe_and_unique: без него у ON CONFLICT
        нет арбитра."""
        self._ensure_analytics_event_id_unique()
        total = self.q1("SELECT COUNT(*) AS n FROM events WHERE event NOT LIKE ?",
                        (EVENT_CFG_PREFIX,))["n"]
        if not total:
            return 0
        log.info("миграция аналитики: переносим %d строк пачками по %d",
                 total, self.ANALYTICS_MIGRATION_BATCH)
        moved, low = 0, 0
        while True:
            # Правая граница пачки берётся по id, а не OFFSET'ом: OFFSET на
            # каждом шаге перечитывает всё начало таблицы (перенос становится
            # квадратичным), а окно фиксированной ШИРИНЫ по id гоняло бы пустые
            # обороты — id разрежены, старые строки выкусывала чистилка по TTL
            top = self.q1(
                "SELECT MAX(id) AS top FROM (SELECT id FROM events "
                "WHERE event NOT LIKE ? AND id >= ? ORDER BY id LIMIT ?) t",
                (EVENT_CFG_PREFIX, low, self.ANALYTICS_MIGRATION_BATCH))["top"]
            if top is None:
                break                    # строк выше low не осталось
            moved += self.exec(
                "INSERT INTO analytics_events (event_id, user_id, event, value, "
                "created_at, exported_at) "
                "SELECT 'legacy:' || id, COALESCE(user_id, 0), COALESCE(event, ''), "
                "       COALESCE(value, 0), COALESCE(created_at, 0), 0 FROM events "
                "WHERE event NOT LIKE ? AND id >= ? AND id <= ? "
                "ON CONFLICT (event_id) DO NOTHING",
                (EVENT_CFG_PREFIX, low, top))
            low = top + 1
            # «вставлено» может отставать от «просмотрено»: на повторном прогоне
            # часть строк уже перенесена и отсекается конфликтом. Поэтому в логе
            # обе величины — иначе догоняющий перенос выглядит стоящим на месте
            log.info("миграция аналитики: дошли до id %d, вставлено %d из %d",
                     top, moved, total)
        return moved

    def _ensure_analytics_event_id_unique(self):
        """Уникальный индекс по event_id — арбитр для ON CONFLICT DO NOTHING.

        Обычно его вешает _dedupe_and_unique, но тот работает ПОСЛЕ миграций, а
        идемпотентность переноса нужна во время. Имя то же самое, так что второй
        раз индекс не создастся.

        Дубли по event_id на этот момент бывают ровно одного происхождения:
        недобитый прогон СТАРОГО кода, где два воркера копировали events
        одновременно. Схлопываем их тем же правилом, что и дедуп, — выживает
        строка с минимальным id, — иначе индекс не создать, а без индекса
        миграция теряет идемпотентность."""
        create = ("CREATE UNIQUE INDEX IF NOT EXISTS uq_analytics_event_id "
                  "ON analytics_events(event_id)")
        try:
            self.cursor.execute(create)
            return
        except INTEGRITY_ERRORS:
            pass
        removed = self.exec(
            "DELETE FROM analytics_events WHERE id NOT IN "
            "(SELECT MIN(id) FROM analytics_events GROUP BY event_id)")
        log.warning("миграция аналитики: схлопнуто %d задвоенных событий "
                    "(след прошлой гонки воркеров)", removed)
        self.cursor.execute(create)

    def _backfill_entitlements(self):
        """Записывает уже поднятый premium-пасс как право источника 'legacy'.

        Возврат Stars снимает своё право и пересчитывает флаг по остаткам. Без
        переноса у старых игроков прав нет вовсе, и первый же возврат покупки
        обнулил бы пасс, полученный за 25 рефералов или перенесённый с прошлого
        сезона. 'legacy' снять нельзя ничем — это и есть цель: миграция может
        только сохранить право, но не отобрать.

        Отдельная строка на bp_premium_next: перенос на следующий сезон — такой
        же флаг, и обнулять его чужим возвратом так же нельзя.

        INSERT без ON CONFLICT: уникальный индекс на entitlements в этот момент
        ещё не создан (_dedupe_and_unique зовётся после _auto_migrate), арбитра
        конфликта не существует. Разовость даёт журнал миграций."""
        cur = self.cursor
        cur.execute("SELECT user_id, season_id, bp_premium, bp_premium_next "
                    "FROM users WHERE bp_premium = 1 OR bp_premium_next = 1")
        rows = cur.fetchall()
        now = time.time()
        moved = 0
        for row in rows:
            season = row["season_id"] or 0
            for flag, target in ((row["bp_premium"], season),
                                 (row["bp_premium_next"], season + 1)):
                if not flag:
                    continue
                cur.execute(
                    "INSERT INTO entitlements (user_id, kind, source, source_ref, "
                    "season_id, created_at) "
                    "VALUES (?, 'bp_premium', 'legacy', '', ?, ?)",
                    (row["user_id"], target, now))
                moved += 1
        if moved:
            print(f"[*] Миграция: {moved} прав premium-пасса перенесено в "
                  f"entitlements ({len(rows)} игроков)")

    def _backfill_best_item_level(self):
        """Проставляет рекорд тира старым аккаунтам ПО ФАКТУ их прогресса.

        Без этого миграция дарит уровни: first_item_xp платится за каждый тир
        от best_item_level+1 до нового, а у всех существующих игроков колонка
        приезжает нулём. Ветеран с доской 20 тира получил бы за первый же мердж
        рекорды за тиры 1..20 — это вся ветка уровней разом.

        Берём максимум из доски и альбома коллекции. Обновляем только нули,
        поэтому повторный запуск ничего не портит."""
        cur = self.cursor
        cur.execute("SELECT COUNT(*) c FROM users WHERE best_item_level = 0")
        if not cur.fetchone()["c"]:
            return
        cur.execute(f"""
            UPDATE users SET best_item_level = {self.GREATEST}(
                COALESCE((SELECT MAX(item_level) FROM board b
                          WHERE b.user_id = users.user_id), 0),
                COALESCE((SELECT MAX(item_level) FROM collection c
                          WHERE c.user_id = users.user_id), 0))
            WHERE best_item_level = 0""")
        if cur.rowcount:
            print(f"[*] Миграция: рекорд тира проставлен {cur.rowcount} игрокам")

    def _backfill_bp_claims(self):
        """Переносит забранные награды пасса из json-списков в строки bp_claims.

        Без переноса миграция ДАРИТ награды: код после неё смотрит только на
        строки, а у всех существующих игроков строк нет — весь пройденный пасс
        можно забрать второй раз.

        INSERT без ON CONFLICT намеренно: уникальный индекс на bp_claims в этот
        момент ещё не создан (_dedupe_and_unique зовётся после _auto_migrate),
        так что арбитр конфликта не существует. Разовость даёт журнал миграций, а
        дубли внутри одного json-списка снимает set; всё, что всё-таки просочится,
        схлопнет _dedupe_and_unique следом.

        Битый json (обрезанная строка от древнего сбоя) молча пропускаем: потерять
        отметку об одной награде дешевле, чем не подняться вовсе."""
        cur = self.cursor
        cur.execute("SELECT user_id, season_id, bp_claimed_free, bp_claimed_premium "
                    "FROM users WHERE COALESCE(bp_claimed_free, '[]') <> '[]' "
                    "   OR COALESCE(bp_claimed_premium, '[]') <> '[]'")
        rows = cur.fetchall()
        now = time.time()
        moved = 0
        for row in rows:
            for track, col in (("free", "bp_claimed_free"),
                               ("premium", "bp_claimed_premium")):
                try:
                    levels = json.loads(row[col] or "[]")
                except (ValueError, TypeError):
                    continue
                if not isinstance(levels, list):
                    continue
                for lvl in sorted({int(x) for x in levels
                                   if isinstance(x, (int, float))}):
                    cur.execute(
                        "INSERT INTO bp_claims (user_id, season_id, track, level, "
                        "claimed_at) VALUES (?, ?, ?, ?, ?)",
                        (row["user_id"], row["season_id"] or 0, track, lvl, now))
                    moved += 1
        if moved:
            print(f"[*] Миграция: {moved} наград пасса перенесено в bp_claims "
                  f"({len(rows)} игроков)")

    # ---------- соединение (по потокам) ----------

    def _connect(self):
        """Новое соединение с прогретыми прагмами.

        Прагмы, кроме journal_mode, действуют НА СОЕДИНЕНИЕ, поэтому ставятся
        здесь, а не один раз в __init__."""
        if self.DIALECT == "postgres":
            return self._connect_postgres()
        if self.db_file == ":memory:":
            # у in-memory базы каждое соединение — своя пустая база, поэтому
            # такое соединение одно на процесс (тесты и только они)
            if self._memory_conn is not None:
                return self._memory_conn
        # timeout=10 говорит базе: если занято, подожди 10 сек, а не падай сразу
        conn = sqlite3.connect(self.db_file, check_same_thread=False, timeout=10)
        # автокоммит на каждый statement; многошаговые операции — явно через tx()
        conn.isolation_level = None
        # Чтобы получать результаты как словари, а не кортежи (удобнее читать)
        conn.row_factory = sqlite3.Row
        # WAL — МЕГА-ВАЖНО для онлайна и скорости
        conn.execute("PRAGMA journal_mode=WAL")
        # synchronous=FULL заставляет ждать fsync на КАЖДОМ коммите: замер на
        # этой машине — 347 транзакций в секунду против 6484 на NORMAL. Под WAL
        # NORMAL не бьёт базу: потерять можно только последние коммиты и только
        # при отключении питания (падение процесса и kill -9 безопасны), а от
        # этого есть ежедневный снимок. 20-кратная пропускная способность за
        # риск потерять последние секунды — сделка, ради которой WAL и брали
        conn.execute("PRAGMA synchronous=NORMAL")
        # внешние ключи в SQLite выключены по умолчанию и включаются на каждое
        # соединение отдельно; на PostgreSQL они всегда на
        conn.execute("PRAGMA foreign_keys=ON")
        if self.db_file == ":memory:":
            self._memory_conn = conn
        return conn

    def _connect_postgres(self):
        """Соединение PostgreSQL с теми же свойствами, что у SQLite-варианта.

        autocommit=True — это НЕ «без транзакций», а ровно то же поведение, что
        `isolation_level = None` у sqlite3: одиночный стейтмент коммитится сам,
        многошаговые операции берутся в явную транзакцию в tx(). Без этого
        psycopg открыл бы транзакцию на первом же SELECT и держал её до
        commit(), а у нас соединение живёт всё время жизни потока — то есть
        каждый воркер вечно висел бы в idle in transaction, блокируя VACUUM."""
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self.url, autocommit=True, row_factory=dict_row,
                               connect_timeout=10)
        self._set_statement_timeout(conn, self.PG_STATEMENT_TIMEOUT_MS)
        return conn

    def _set_statement_timeout(self, conn=None, ms: int = 0):
        """Предохранитель от зависшего стейтмента (только PostgreSQL).

        Это не бюджет задержки, а именно предохранитель: у SQLite аналога нет
        (там писатель один и его ждут), а на PostgreSQL один запрос по забытому
        индексу способен держать блокировки строк, пока его кто-нибудь не
        заметит. На время миграций снимается: CREATE UNIQUE INDEX по таблице
        игроков законно идёт минуты, и убивать его по таймауту значит не дать
        процессу подняться вообще."""
        if self.DIALECT != "postgres":
            return
        (conn or self.connection).execute(f"SET statement_timeout = {int(ms)}")

    @property
    def connection(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
            self._local.cur = conn.cursor()
            self._local.depth = 0
        return conn

    @property
    def cursor(self):
        _ = self.connection      # гарантирует, что соединение потока поднято
        return self._local.cur

    @property
    def _tx_depth(self) -> int:
        _ = self.connection
        return self._local.depth

    @_tx_depth.setter
    def _tx_depth(self, value: int):
        _ = self.connection
        self._local.depth = value

    # ---------- универсальные хелперы ----------

    def _sql(self, sql: str) -> str:
        """Плейсхолдеры на всех call site'ах остаются '?'; под postgres их
        переписывает шим, чтобы не править сотни запросов при переезде."""
        return sql if self.DIALECT == "sqlite" else sql.replace("?", "%s")

    @contextmanager
    def tx(self):
        """Атомарный блок: все exec() внутри коммитятся одним куском или
        откатываются целиком. Вложенные tx() присоединяются к внешнему."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        if self.DIALECT == "postgres":
            # BEGIN IMMEDIATE не переводится: он берёт ЕДИНСТВЕННУЮ на файл
            # блокировку писателя, поэтому и нуждался в ретраях. PostgreSQL
            # пишет параллельно и блокирует построчно — ждать здесь нечего, а
            # редкий конфликт сериализации (40001) прилетает уже из тела
            # транзакции и превращается в 409 обработчиком в main.py.
            self._tx_depth = 1
            try:
                with self.connection.transaction():
                    yield
            finally:
                self._tx_depth = 0
            return
        cur = self.cursor
        for attempt in range(TX_RETRIES):
            try:
                cur.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as e:
                # база занята другим писателем. Глубину НЕ трогаем до успешного
                # BEGIN: иначе неудачная попытка оставила бы счётчик отравленным
                # на весь процесс, и следующие tx() молча не коммитили бы
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                if attempt == TX_RETRIES - 1:
                    raise
                obs.inc("db_tx_retries_total")
                time.sleep(0.02 * (2 ** attempt))
        self._tx_depth = 1
        try:
            yield
            self.connection.commit()
        except BaseException:
            try:
                self.connection.rollback()
            except sqlite3.Error:
                pass          # соединение добьёт _drop_dirty_connection ниже
            raise
        finally:
            self._tx_depth = 0
            self._drop_dirty_connection()

    def _drop_dirty_connection(self):
        """Выбросить соединение потока, если на нём осталась открытая транзакция.

        Соединения живут по потоку и переиспользуются. Транзакция, которую не
        удалось ни закоммитить, ни откатить (COMMIT упал по busy, а следом упал
        и ROLLBACK), остаётся открытой НАВСЕГДА — и дальше это соединение
        читает снапшот на момент её начала. Снаружи это выглядит не как ошибка,
        а как путешествие во времени: игрок, который только что
        зарегистрировался, получает `err_no_user`, а его клики уходят в никуда.
        Плюс открытая транзакция держит блокировку писателя, то есть один
        застрявший поток тормозит всех остальных.

        Поэтому: не чиним — выбрасываем. Следующее обращение из этого потока
        поднимет чистое соединение, а закрытие снимет и транзакцию, и
        блокировку."""
        if self.DIALECT != "sqlite":
            return
        conn = getattr(self._local, "conn", None)
        if conn is None or not conn.in_transaction:
            return
        obs.inc("db_dirty_connections_total")
        log.error("соединение потока осталось в транзакции — выбрасываем его")
        # in-memory база живёт ровно в одном соединении: закрыть его — значит
        # стереть всю базу. Там остаётся последняя попытка отката
        if self.db_file == ":memory:":
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.conn = None
        self._local.cur = None
        self._local.depth = 0

    @staticmethod
    @contextmanager
    def _measure(op: str):
        """Время одного обращения к базе.

        Метка — только read/write, без самого SQL: текст запроса в метке дал бы
        по ряду на каждый вариант, а их здесь сотни. Какой именно запрос
        тормозит, отвечает не метрика, а лог медленных запросов на стороне
        самой базы (`log_min_duration_statement`)."""
        started = time.perf_counter()
        try:
            yield
        finally:
            obs.inc("db_queries_total", op=op)
            obs.observe("db_query_seconds", time.perf_counter() - started, op=op)

    def q(self, sql, params=()):
        """SELECT: список dict"""
        cur = self.cursor
        with self._measure("read"):
            cur.execute(self._sql(sql), params)
            return [dict(r) for r in cur.fetchall()]

    def q1(self, sql, params=()):
        """SELECT: одна строка dict или None"""
        cur = self.cursor
        with self._measure("read"):
            cur.execute(self._sql(sql), params)
            row = cur.fetchone()
        return dict(row) if row else None

    def exec(self, sql, params=()) -> int:
        """INSERT/UPDATE/DELETE, возвращает ЧИСЛО ЗАТРОНУТЫХ СТРОК.

        Раньше возвращался lastrowid, но он ВРЁТ после проигнорированного
        конфликтного INSERT: остаётся значение от прошлой вставки, и проверка
        идемпотентности на нём выдала бы награду повторно. rowcount снимаем
        немедленно — любой следующий стейтмент сбрасывает его в -1.
        Вне tx() — автокоммит; внутри tx() коммитит внешний блок."""
        cur = self.cursor
        with self._measure("write"):
            cur.execute(self._sql(sql), params)
        rc = cur.rowcount
        # lastrowid есть только у sqlite3: в psycopg его нет вовсе, и обращение
        # к нему уронило бы КАЖДУЮ запись. Кому нужен выданный id — просит его
        # через RETURNING (q1w), это работает на обоих движках
        self.last_insert_id = getattr(cur, "lastrowid", None)
        if not self._tx_depth:
            self.connection.commit()
        return rc

    def q1w(self, sql, params=()):
        """Пишущий стейтмент с RETURNING: одна строка dict или None.

        None означает, что запись не прошла (условие UPDATE не сошлось или
        INSERT ушёл в ON CONFLICT DO NOTHING) — то есть тот же сигнал, что и
        rowcount == 0, но вместе с данными строки, за одно обращение."""
        cur = self.cursor
        with self._measure("write"):
            cur.execute(self._sql(sql), params)
            row = cur.fetchone()
        if not self._tx_depth:
            self.connection.commit()
        return dict(row) if row else None

    def qw(self, sql, params=()):
        """Пишущий стейтмент с RETURNING: СПИСОК строк.

        То же, что q1w, но на пачку. Нужен ровно там, где «пометить своим» и
        «прочитать содержимое» обязаны быть ОДНИМ стейтментом: партия из очереди
        пушей, взятая двумя запросами (SELECT, потом UPDATE), достаётся сразу
        двум воркерам, и игрок получает два одинаковых сообщения."""
        cur = self.cursor
        with self._measure("write"):
            cur.execute(self._sql(sql), params)
            rows = cur.fetchall()
        if not self._tx_depth:
            self.connection.commit()
        return [dict(r) for r in rows]

    # ---------- очередь уведомлений ----------

    def claim_notifications(self, limit: int, now: float,
                            owner: str) -> list[dict]:
        """Взять партию готовых к отправке задач. Каждая строка достаётся ровно
        одному воркеру — на этом держится вся развязка планировщика и отправки.

        Диалектных различий здесь ровно одно, и оно про ОЖИДАНИЕ. В SQLite
        писатель на файл один: пока идёт этот UPDATE, второй воркер до очереди
        не доберётся вовсе, и разойтись по разным строкам им негде и незачем.
        PostgreSQL пишет параллельно, и без `FOR UPDATE SKIP LOCKED` второй
        воркер встал бы на строках первого и дождался бы... ровно тех же строк,
        уже не подходящих под условие. То есть работал бы всегда один воркер.

        `LIMIT` внутри подзапроса, а не в самом UPDATE: `UPDATE ... LIMIT`
        SQLite понимает только в специальной сборке, а `WHERE id IN (SELECT ...
        LIMIT ?)` — везде. `RETURNING` умеют оба движка (SQLite с 3.35), и он
        здесь не удобство: без него содержимое взятых строк пришлось бы читать
        отдельным SELECT'ом, а к этому моменту их уже мог бы переписать
        сборщик просроченных заявок."""
        skip = " FOR UPDATE SKIP LOCKED" if self.DIALECT == "postgres" else ""
        return self.qw(
            "UPDATE notification_queue SET status = 'sending', "
            "attempts = attempts + 1, claimed_at = ?, claimed_by = ? "
            "WHERE id IN (SELECT id FROM notification_queue "
            "WHERE status = 'scheduled' AND scheduled_at <= ? "
            f"ORDER BY scheduled_at LIMIT ?{skip}) "
            "RETURNING *", (now, owner, now, limit))

    # ---------- юзеры ----------

    def get_user(self, user_id):
        return self.q1("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def create_user(self, user_id, username, first_name, referrer_id=None, source_code=None):
        """Возвращает (строка игрока, создали ли её ИМЕННО МЫ).

        Второй элемент — не удобство, а разрешение на разовые действия
        регистрации: бонус за реферала, счётчик источника, флаг just_registered.
        Два запроса /auth от одного нового игрока (двойной тап по кнопке Mini
        App) оба видят пустую базу, но вставка проходит ровно у одного; тот, кто
        проиграл, получает чужую строку и False, и платить по ней нельзя."""
        now = time.time()
        fresh = self.q1w(
            "INSERT INTO users (user_id, username, first_name, referrer_id, "
            "source_code, energy_updated_at, passive_collected_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (user_id) DO NOTHING "
            "RETURNING user_id",
            (user_id, username, first_name, referrer_id, source_code, now, now, now),
        )
        return self.get_user(user_id), fresh is not None

    # Колонки, изменение которых игрок увидеть не может: служебные отметки
    # времени и флаг доставки пушей. Они меняются в фоне (нотификатор, каждый
    # GET /api/state), и если бы они двигали user_revision, версия состояния
    # устаревала бы сама по себе — клиент вернул бы её обратно и получил 409
    # на первое же осмысленное действие. Ревизия считает изменения СМЫСЛА,
    # а не изменения строки.
    SILENT_COLUMNS = frozenset({"last_seen_at", "last_notified_at",
                                "notify_blocked", "tz_offset_min"})

    def update_user(self, user_id, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        if fields and not set(fields) <= self.SILENT_COLUMNS:
            cols += ", user_revision = user_revision + 1"
        self.exec(f"UPDATE users SET {cols} WHERE user_id = ?", (*fields.values(), user_id))

    # ---------- лидерборд ----------
    #
    # Запросы таблицы лиги живут здесь, а не в ручке, ровно по одной причине:
    # порядок и модель ранга обязаны быть ОДНИМИ на все места, где лидерборд
    # считается. Их было три (топ ручки, «моё место» той же ручки, снапшот
    # итогов сезона), и все три расходились: топ нумеровал строки подряд,
    # «моё место» схлопывало равных, а порядок при равных заработке и уровне
    # вообще не был определён — место игрока менялось от запроса к запросу.

    # Полный порядок: третий ключ делает его СТРОГИМ, то есть воспроизводимым
    # между запросами, воркерами и движками. Ровно этот же порядок несёт
    # idx_users_leaderboard.
    LEADERBOARD_ORDER = "season_earned DESC, level DESC, user_id ASC"
    LEADERBOARD_TOP = 100

    @staticmethod
    def league_cond(level_min: int, level_max: int | None) -> tuple[str, list]:
        """Условие «игрок в этой лиге» и его параметры.

        Верхней границы у старшей лиги нет — там условие из одной половины,
        и собирать его в каждом call site значило бы разъезжающиеся варианты."""
        cond = "level >= ?"
        params: list = [level_min]
        if level_max is not None:
            cond += " AND level <= ?"
            params.append(level_max)
        return cond, params

    def leaderboard_top(self, season: int, level_min: int, level_max: int | None,
                        limit: int | None = None) -> list[dict]:
        """Топ лиги со проставленным rank (competition rank)."""
        cond, lp = self.league_cond(level_min, level_max)
        rows = self.q(
            f"SELECT user_id, username, first_name, level, season_earned "
            f"FROM users WHERE season_id = ? AND {cond} "
            f"ORDER BY {self.LEADERBOARD_ORDER} LIMIT ?",
            [season, *lp, limit or self.LEADERBOARD_TOP])
        return competition_ranks(rows)

    def leaderboard_count(self, season: int, level_min: int,
                          level_max: int | None) -> int:
        """Сколько всего игроков в лиге (знаменатель «ты N-й из M»)."""
        cond, lp = self.league_cond(level_min, level_max)
        return self.q1(
            f"SELECT COUNT(*) c FROM users WHERE season_id = ? AND {cond}",
            [season, *lp])["c"]

    def leaderboard_rank(self, season: int, level_min: int, level_max: int | None,
                         season_earned: float, level: int) -> int:
        """Место игрока по той же модели, что и rank в leaderboard_top.

        Считается как «сколько игроков строго впереди» + 1 — это и есть
        competition rank: у равных по (заработок, уровень) место одинаковое,
        а следующий за парой вторых получает четвёртое. Считать место игрока
        нумерацией строк нельзя: он может не входить в топ-100 вовсе."""
        cond, lp = self.league_cond(level_min, level_max)
        return self.q1(
            f"SELECT COUNT(*) c FROM users WHERE season_id = ? AND {cond} "
            f"AND (season_earned > ? OR (season_earned = ? AND level > ?))",
            [season, *lp, season_earned, season_earned, level])["c"] + 1


# Ранг считается по (заработок за сезон, уровень) — user_id в ключ ранга НЕ
# входит: он разводит порядок строк, но не должен разводить места. Иначе двое
# с одинаковым результатом получали бы разные места и разные призы по признаку,
# которого в игре не видно.
LEADERBOARD_RANK_KEY = ("season_earned", "level")


def competition_ranks(rows: list[dict]) -> list[dict]:
    """Проставляет rows[i]['rank'] по модели competition rank: 1, 2, 2, 4.

    Строки обязаны прийти уже отсортированными по LEADERBOARD_ORDER. Модель
    выбрана из двух: ordinal rank (1,2,3,4) нумерует строки подряд и потому не
    совпадает ни с каким счётным запросом — «моё место» из COUNT(*) и место в
    таблице расходились у любой пары с равным результатом. Competition rank
    выражается счётом «сколько строго впереди», поэтому одинаково считается и
    по списку, и одним COUNT(*) для игрока вне топа."""
    rank = 0
    prev = None
    for i, row in enumerate(rows):
        key = tuple(row[c] for c in LEADERBOARD_RANK_KEY)
        if key != prev:
            rank = i + 1        # пропуск мест после группы равных — это и есть 1,2,2,4
            prev = key
        row["rank"] = rank
    return rows


_shared: "DataBase | None" = None


def shared() -> DataBase:
    """Единственный экземпляр базы на процесс.

    Раньше он жил в game_logic, и любому новому модулю оставалось либо тянуть
    game_logic (кольцевой импорт), либо завести ВТОРОЙ DataBase. Второй — это
    второе соединение, то есть своя транзакция: запись такого модуля не попала
    бы в db.tx() вызывающего и коммитилась бы отдельно. Ровно то, чего книга
    операций не переживает."""
    global _shared
    if _shared is None:
        # оба ключа читаются ЖИВЫМ окружением: тесты подменяют их после
        # импорта модуля (см. комментарий в __init__)
        _shared = DataBase(os.environ.get("DATABASE_PATH", "data.db"),
                           os.environ.get("DATABASE_URL", ""))
    return _shared
