import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager

# сколько раз повторить BEGIN IMMEDIATE, если база занята другим писателем
TX_RETRIES = 5


class DataBase:
    # Диалект и два имени, которые расходятся между движками. Всё остальное в
    # новом SQL — общий синтаксис (ON CONFLICT DO NOTHING, RETURNING,
    # UPDATE ... WHERE <guard>), так что переезд на PostgreSQL сведётся к смене
    # DIALECT и драйвера соединения.
    DIALECT = "sqlite"
    GREATEST = "MAX"     # на postgres -> "GREATEST"
    LEAST = "MIN"        # на postgres -> "LEAST"

    def __init__(self, db_file=None):
        # Путь можно переопределить (тесты используют временную БД). Это
        # единственный ключ, который читается ЖИВЫМ os.environ, а не через
        # server.settings: тесты и симуляторы подменяют его уже после импорта
        # модуля, и снимок, сделанный при загрузке настроек, был бы для них
        # путём к боевой базе.
        db_file = db_file or os.environ.get("DATABASE_PATH", "data.db")
        self.db_file = db_file
        # соединение, курсор и глубина транзакции живут ПО ПОТОКАМ: один общий
        # курсор на процесс — это гонка за rowcount (любой SELECT из другого
        # потока сбрасывал его в -1 между записью и проверкой)
        self._local = threading.local()
        self._memory_conn = None
        self.last_insert_id = None

        # журнал применённых миграций. Создаётся ДО всего остального: на него
        # опирается и _auto_migrate (__after_create__), и дедуп
        self.cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "name TEXT PRIMARY KEY, applied_at REAL NOT NULL DEFAULT 0)")

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
            'events': {  # аналитика: одно событие = одна строка
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'event': 'TEXT',
                'value': 'REAL DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
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
                # NaN: на SQLite он ложится как NULL и его ловит NOT NULL,
                # на PostgreSQL NaN хранится честно — и его ловит CHECK
                '__constraints__': [
                    "CHECK (amount = amount)",
                    "CHECK (balance_after = balance_after)",
                    "CHECK (amount > -1e15 AND amount < 1e15)",
                    "CHECK (balance_after > -1e15 AND balance_after < 1e15)",
                ],
                # книга только дописывается: правка задним числом уничтожает
                # весь смысл сверки
                '__after_create__': [
                    "CREATE TRIGGER IF NOT EXISTS trg_ledger_no_update "
                    "BEFORE UPDATE ON economy_ledger "
                    "BEGIN SELECT RAISE(ABORT, 'economy_ledger is append-only'); END",
                    "CREATE TRIGGER IF NOT EXISTS trg_ledger_no_delete "
                    "BEFORE DELETE ON economy_ledger "
                    "BEGIN SELECT RAISE(ABORT, 'economy_ledger is append-only'); END",
                ],
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
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_name ON events(event, created_at)")
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
        # четырёх ручек; лидерборд фильтрует по (season_id, level)
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_season ON users(season_id, level)")
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
            "CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at)")
        # выборка кандидатов на пуш: пробег по всей таблице юзеров каждую минуту
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_notify "
            "ON users(notify_blocked, last_seen_at, last_notified_at)")
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
        """Копия базы перед разрушительной миграцией (sqlite backup API)."""
        path = f"{db_file}.pre-dedup-{int(time.time())}.bak"
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
            print(f"[*] Миграция: найдены дубли, бэкап сохранён в {path}")
        finally:
            dest.close()

    def snapshot(self, keep: int = 7) -> str | None:
        """Горячий бэкап базы через sqlite backup API.

        Никаких бэкапов не было вообще — единственная копия делалась один раз
        перед dedup-миграцией. sqlite3.backup корректно работает на живой базе
        в WAL-режиме, поэтому останавливать сервис не нужно.
        Старые снимки чистим, оставляя последние `keep`."""
        if self.db_file == ":memory:":
            return None
        folder = os.path.join(os.path.dirname(os.path.abspath(self.db_file)), "backups")
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = os.path.join(folder, f"{os.path.basename(self.db_file)}.{stamp}.bak")
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
        finally:
            dest.close()
        old = sorted(f for f in os.listdir(folder) if f.endswith(".bak"))
        for name in old[:-keep]:
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass
        return path

    # ---------- миграции ----------

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
            except sqlite3.IntegrityError as e:
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
        self.cursor.execute(
            "CREATE TRIGGER IF NOT EXISTS trg_users_balance_sane "
            "BEFORE UPDATE OF cookies ON users "
            "WHEN NEW.cookies IS NULL OR NEW.cookies < -1e6 OR NEW.cookies > 1e15 "
            "BEGIN SELECT RAISE(ABORT, 'balance_insane'); END")
        self._mark("invariants:users_balance")

    def _auto_migrate(self):
        """ Умная система: создает таблицы или добавляет новые столбцы на лету """
        for table_name, spec in self.tables_schema.items():
            # ключи с двумя подчёркиваниями — не колонки: __constraints__
            # дописываются в CREATE TABLE, __after_create__ выполняется разово
            columns = {k: v for k, v in spec.items() if not k.startswith("__")}
            parts = [f"{col} {ctype}" for col, ctype in columns.items()]
            parts += list(spec.get("__constraints__", ()))
            self.cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(parts)})")

            self.cursor.execute(f"PRAGMA table_info({table_name})")
            existing_columns = [row['name'] for row in self.cursor.fetchall()]

            for col_name, col_type in columns.items():
                if col_name not in existing_columns:
                    print(f"[*] Миграция: Добавлен новый столбец {col_name} в {table_name}")
                    self.cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")

            after = spec.get("__after_create__")
            key = f"after_create:{table_name}"
            if after and self._migration(key):
                for stmt in after:
                    self.cursor.execute(stmt)
                self._mark(key)

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
        self.connection          # гарантирует, что соединение потока поднято
        return self._local.cur

    @property
    def _tx_depth(self) -> int:
        self.connection
        return self._local.depth

    @_tx_depth.setter
    def _tx_depth(self, value: int):
        self.connection
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
                time.sleep(0.02 * (2 ** attempt))
        self._tx_depth = 1
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._tx_depth = 0

    def q(self, sql, params=()):
        """SELECT: список dict"""
        cur = self.cursor
        cur.execute(self._sql(sql), params)
        return [dict(r) for r in cur.fetchall()]

    def q1(self, sql, params=()):
        """SELECT: одна строка dict или None"""
        cur = self.cursor
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
        cur.execute(self._sql(sql), params)
        rc = cur.rowcount
        self.last_insert_id = cur.lastrowid
        if not self._tx_depth:
            self.connection.commit()
        return rc

    def q1w(self, sql, params=()):
        """Пишущий стейтмент с RETURNING: одна строка dict или None.

        None означает, что запись не прошла (условие UPDATE не сошлось или
        INSERT ушёл в ON CONFLICT DO NOTHING) — то есть тот же сигнал, что и
        rowcount == 0, но вместе с данными строки, за одно обращение."""
        cur = self.cursor
        cur.execute(self._sql(sql), params)
        row = cur.fetchone()
        if not self._tx_depth:
            self.connection.commit()
        return dict(row) if row else None

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
    SILENT_COLUMNS = frozenset({"last_seen_at", "last_notified_at", "notify_blocked"})

    def update_user(self, user_id, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        if fields and not set(fields) <= self.SILENT_COLUMNS:
            cols += ", user_revision = user_revision + 1"
        self.exec(f"UPDATE users SET {cols} WHERE user_id = ?", (*fields.values(), user_id))


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
        _shared = DataBase(os.environ.get("DATABASE_PATH", "data.db"))
    return _shared
