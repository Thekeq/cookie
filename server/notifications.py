"""Очередь уведомлений: планировщик КЛАДЁТ задачи, воркер их ОТПРАВЛЯЕТ.

Что было. Один проход раз в 15 минут читал всю таблицу игроков, на каждого
делал ещё несколько запросов и тут же слал сообщение. Модель не масштабируется
по трём разным причинам сразу:

  * 100 тысяч пушей при 25 msg/s — это больше часа, то есть проход не
    укладывается в собственный интервал;
  * состояния отправки не существует нигде: процесс убили посреди прохода —
    половина игроков не получила ничего, и узнать, кто именно, невозможно;
  * решение «слать или не слать» принималось в момент отправки по данным,
    прочитанным в начале прохода, — событие могло протухнуть час назад.

Что стало. Единица работы — строка в `notification_queue`. Планировщик только
дописывает строки (быстро, пачками, идемпотентно по `dedup_key`), воркеры
разбирают очередь партиями и шлют. Их может быть сколько угодно: партию
забирает ОДИН UPDATE (см. db.claim_notifications), и одна строка достаётся
ровно одному воркеру.

Три вещи проверяются НЕ при планировании, а прямо перед отправкой, и это
принципиально — между планированием и отправкой проходят часы:

  1. Тихие часы по часовому поясу игрока. Пуш в четыре утра стоит дороже, чем
     не отправленный пуш: игрок блокирует бота, и больше ему не написать уже
     никогда.
  2. Частотный лимит — общий и по категории. Иначе игрок, у которого сошлись
     дуэль, заказ и конец сезона, получает три сообщения подряд.
  3. Актуальность события. «Тесто готово» через шесть часов после того, как его
     забрали, — это не напоминание, а ложь.

Отложенное сообщение возвращается в очередь со своим новым временем, а не
выбрасывается: событие никуда не делось, просто сейчас не время.
"""
import datetime
import json
import logging
import time

import db as db_module
from server import game_config as cfg
from server import game_logic as gl
from server import obs
from server import settings
from server.i18n import norm_lang, tr

log = logging.getLogger(__name__)

# ---------- политика ----------

# Тихие часы В МЕСТНОМ времени игрока: с 23:00 до 09:00 не пишем.
QUIET_START_H = 23
QUIET_END_H = 9

# Часовой пояс по языку — для тех, у кого tz_offset_min ещё не известен.
# Грубо, но радикально лучше UTC: у русскоязычной аудитории «полночь по UTC» —
# это три часа ночи.
LANG_TZ_MIN = {"ru": 180, "uk": 120, "en": 0}

# Частотные лимиты на игрока: сколько сообщений за скользящие сутки и какой
# минимальный промежуток между любыми двумя. NOTIFY_MIN_INTERVAL_H (20 часов)
# на очередь не переносится: он был единственной защитой от спама, когда
# триггеров было три; с девятью триггерами он означал бы, что игрок не узнает
# ни про дуэль, ни про конец сезона.
DAILY_CAP = 4
MIN_GAP_S = 45 * 60
CATEGORY_CAP = {
    "recipe": 2,        # готово + подгорает
    "duel": 2,          # заканчивается + результат
    "order": 1,
    "season": 1,
    "event": 1,
    "offline": 1,
    "comeback": 1,
    "legacy": 1,        # стрик и энергия — старые триггеры
}
DEFAULT_CATEGORY_CAP = 1

# Ретраи. Первая задержка минута — ровно на «Telegram моргнул»; дальше растёт,
# потому что пятая попытка через минуту после четвёртой ничего не меняет.
MAX_ATTEMPTS = 5
RETRY_BACKOFF_S = (60, 300, 900, 3600)
# Потолок на retry_after от Telegram: он умеет присылать и сутки (это про
# конкретный чат), а держать строку в очереди дольше её собственного TTL смысла
# нет — событие протухнет раньше.
MAX_RETRY_AFTER_S = 6 * 3600

# Заявка воркера протухает: процесс убили посреди отправки, строка осталась в
# 'sending'. Больше, чем самая долгая отправка, и меньше, чем терпение игрока.
LEASE_S = 300

# Сколько строк воркер берёт за раз и с какой скоростью шлёт. 25 msg/s — предел
# Telegram на бота; партия в 200 — это 8 секунд работы, то есть заявка не
# успевает протухнуть даже при полностью занятом канале.
BATCH = 200
RATE_PER_SEC = 25.0

# Сколько кандидатов планировщик разбирает за один проход и на один триггер.
# Ограничение не про скорость, а про предсказуемость: проход обязан заканчиваться
# за секунды при любом размере базы, а недобранное доберёт следующий.
PLAN_CHUNK = 2000

# TTL разобранных строк. Очередь — не аналитика: строка нужна ровно до тех пор,
# пока по ней могут прийти ретрай или открытие.
TTL_DAYS = 14

# Категория и цель диплинка на каждый вид. Вкладки Mini App: clicker, merge,
# bakery, farm, progress, profile; у progress и profile есть ещё сегменты.
KINDS: dict[str, tuple[str, str, str, float]] = {
    # kind: (категория, вкладка, сегмент, сколько событие живёт)
    "recipe_ready":   ("recipe",   "farm",     "",      6 * 3600),
    "recipe_burning": ("recipe",   "farm",     "",      2 * 3600),
    "offline_cap":    ("offline",  "farm",     "",      12 * 3600),
    "duel_ending":    ("duel",     "progress", "top",   3 * 3600),
    "duel_result":    ("duel",     "progress", "top",   48 * 3600),
    "order_waiting":  ("order",    "bakery",   "",      12 * 3600),
    "season_end":     ("season",   "progress", "top",   24 * 3600),
    "bp_unclaimed":   ("season",   "progress", "bp",    48 * 3600),
    "event_start":    ("event",    "clicker",  "",      24 * 3600),
    "comeback":       ("comeback", "clicker",  "",      24 * 3600),
    "streak":         ("legacy",   "clicker",  "",      4 * 3600),
    "energy_full":    ("legacy",   "clicker",  "",      12 * 3600),
}

# Виды, которые не тревожат игрока, если он только что был в игре: интерфейс
# показал бы ему это сам. Дуэль, конец сезона и старт ивента сюда не входят —
# у них своё время, и «зайди попозже» их не заменяет.
AMBIENT = ("recipe_ready", "recipe_burning", "offline_cap", "order_waiting",
           "bp_unclaimed", "streak", "energy_full")

# Готовые переводы старых триггеров лежат в общем словаре i18n — берём оттуда,
# а не заводим вторую копию тех же трёх строк.
I18N_KEYS = {"streak": "notif_streak", "energy_full": "notif_energy",
             "offline_cap": "notif_farm"}

TEXTS: dict[str, dict[str, str]] = {
    "recipe_ready": {
        "en": "🥐 The dough has risen! Collect your offline income "
              "with a x{mult} multiplier 🍪",
        "uk": "🥐 Тісто підійшло! Забери офлайн-дохід з множником x{mult} 🍪",
        "ru": "🥐 Тесто подошло! Забери оффлайн-доход с множителем x{mult} 🍪",
    },
    "recipe_burning": {
        "en": "🔥 Your dough is about to burn — less than an hour left "
              "to collect the bonus!",
        "uk": "🔥 Тісто ось-ось підгорить — менше години, щоб забрати бонус!",
        "ru": "🔥 Тесто вот-вот подгорит — меньше часа, чтобы забрать бонус!",
    },
    "duel_ending": {
        "en": "⚔️ Your duel ends soon — a few more clicks may decide it!",
        "uk": "⚔️ Дуель скоро завершиться — кілька кліків можуть вирішити все!",
        "ru": "⚔️ Дуэль скоро закончится — пара кликов могут решить всё!",
    },
    "duel_result": {
        "en": "🏆 The duel is over — check the result and claim your prize!",
        "uk": "🏆 Дуель завершено — подивись результат і забери приз!",
        "ru": "🏆 Дуэль закончилась — посмотри результат и забери приз!",
    },
    "order_waiting": {
        "en": "📋 An order is still waiting in the bakery — finish it "
              "and get paid 🍪",
        "uk": "📋 Замовлення досі чекає в пекарні — заверши й отримай нагороду 🍪",
        "ru": "📋 Заказ всё ещё ждёт в пекарне — закончи его и получи награду 🍪",
    },
    "season_end": {
        "en": "🏁 The season ends in {hours} h! Last chance to climb "
              "the leaderboard.",
        "uk": "🏁 Сезон завершується через {hours} год! Останній шанс "
              "піднятися в таблиці.",
        "ru": "🏁 Сезон заканчивается через {hours} ч! Последний шанс "
              "подняться в таблице.",
    },
    "bp_unclaimed": {
        "en": "🎁 You have {count} unclaimed battle pass rewards — "
              "they burn out with the season!",
        "uk": "🎁 У тебе {count} незабраних нагород бойової перепустки — "
              "вони згорять разом із сезоном!",
        "ru": "🎁 У тебя {count} незабранных наград батл-пасса — "
              "они сгорят вместе с сезоном!",
    },
    "event_start": {
        "en": "🎉 Weekend event is live: x{mult} to your income. "
              "Two days only!",
        "uk": "🎉 Івент вихідних почався: x{mult} до доходу. Лише два дні!",
        "ru": "🎉 Ивент выходных начался: x{mult} к доходу. Только два дня!",
    },
    "comeback": {
        "en": "🍪 Your bakery has been idle for {days} days — the ovens "
              "are cold. Come back and fire them up!",
        "uk": "🍪 Твоя пекарня простоює {days} дн. — печі захололи. "
              "Повертайся й розпалюй!",
        "ru": "🍪 Твоя пекарня простаивает {days} дн. — печи остыли. "
              "Возвращайся и разжигай!",
    },
}

BUTTONS: dict[str, dict[str, str]] = {
    "farm": {"en": "🏭 Open the farm", "uk": "🏭 Відкрити ферму",
             "ru": "🏭 Открыть ферму"},
    "bakery": {"en": "📋 Open the bakery", "uk": "📋 Відкрити пекарню",
               "ru": "📋 Открыть пекарню"},
    "progress": {"en": "📈 Open progress", "uk": "📈 Відкрити прогрес",
                 "ru": "📈 Открыть прогресс"},
    "clicker": {"en": "🍪 Play!", "uk": "🍪 Грати!", "ru": "🍪 Играть!"},
}


def _db():
    return db_module.shared()


def category_of(kind: str) -> str:
    return KINDS.get(kind, ("other", "clicker", "", 3600))[0]


def ttl_of(kind: str) -> float:
    return KINDS.get(kind, ("other", "clicker", "", 3600))[3]


# ---------- тексты и диплинки ----------

def render(kind: str, lang: str, payload: dict) -> str:
    """Текст сообщения. Отсутствующий перевод — это английский, а не пустота."""
    lang = norm_lang(lang)
    if kind in I18N_KEYS:
        return tr(lang, I18N_KEYS[kind], **payload) if payload \
            else tr(lang, I18N_KEYS[kind])
    row = TEXTS.get(kind)
    if not row:
        return kind
    text = row.get(lang) or row["en"]
    try:
        return text.format(**payload)
    except (KeyError, IndexError, ValueError):
        # подстановки в очереди могли устареть вместе с текстом; сообщение без
        # числа лучше, чем упавший воркер
        return text


def start_param(kind: str, notification_id: int = 0) -> str:
    """Значение startapp: вкладка (и сегмент), плюс номер уведомления.

    Формат `tab-<вкладка>[-<сегмент>][-n<id>]`. Дефисы, а не подчёркивания:
    Telegram разрешает в startapp только [A-Za-z0-9_-], а подчёркивание уже
    занято внутри ключей источников (`src_...`) и рефералок (`ref_...`)."""
    _, tab, seg, _ttl = KINDS.get(kind, ("other", "clicker", "", 0))
    parts = ["tab", tab]
    if seg:
        parts.append(seg)
    if notification_id:
        parts.append(f"n{int(notification_id)}")
    return "-".join(parts)


def parse_start_param(param: str) -> dict:
    """Разбор startapp обратно: {'tab', 'segment', 'notification_id'}.

    Живёт здесь, а не во фронте, чтобы формат ссылки имел ровно одного автора:
    ссылку СТРОИТ бот, а разбирают и Mini App, и /auth (отметка «открыл»)."""
    parts = [p for p in (param or "").split("-") if p]
    if not parts or parts[0] != "tab":
        return {}
    nid = 0
    if parts[-1][:1] == "n" and parts[-1][1:].isdigit():
        nid = int(parts[-1][1:])
        parts = parts[:-1]
    return {"tab": parts[1] if len(parts) > 1 else "",
            "segment": parts[2] if len(parts) > 2 else "",
            "notification_id": nid}


def deep_link(kind: str, notification_id: int = 0) -> tuple[str, str]:
    """('webapp'|'url'|'', адрес) — кнопка сразу на нужную вкладку.

    web_app-кнопка открывает Mini App внутри Telegram и приносит подписанный
    initData, поэтому она первая по выбору. t.me-ссылка — запасной вариант для
    конфигурации без WEBAPP_URL (и для клиентов, где web_app недоступен)."""
    param = start_param(kind, notification_id)
    if settings.WEBAPP_URL:
        return "webapp", f"{settings.WEBAPP_URL}?tgWebAppStartParam={param}"
    if settings.BOT_USERNAME:
        return "url", f"https://t.me/{settings.BOT_USERNAME}/app?startapp={param}"
    return "", ""


def button_text(kind: str, lang: str) -> str:
    tab = KINDS.get(kind, ("other", "clicker", "", 0))[1]
    row = BUTTONS.get(tab) or BUTTONS["clicker"]
    return row.get(norm_lang(lang)) or row["en"]


# ---------- тихие часы и частотные лимиты ----------

def tz_offset_min(user: dict) -> int:
    """Смещение игрока от UTC в минутах. NULL в базе — оцениваем по языку."""
    off = user.get("tz_offset_min")
    if off is None:
        return LANG_TZ_MIN.get(norm_lang(user.get("lang")), 0)
    return int(off)


def set_timezone(user_id: int, offset_min: int):
    """Запомнить пояс игрока (значение присылает Mini App: -new Date()
    .getTimezoneOffset()). Границы — на случай мусора от клиента."""
    off = max(-14 * 60, min(14 * 60, int(offset_min)))
    _db().update_user(user_id, tz_offset_min=off)


def quiet_until(user: dict, when: float) -> float:
    """0, если время нормальное; иначе момент конца тихих часов (UTC-эпоха)."""
    off = tz_offset_min(user) * 60
    local = datetime.datetime.fromtimestamp(when + off, datetime.timezone.utc)
    if not (local.hour >= QUIET_START_H or local.hour < QUIET_END_H):
        return 0.0
    target = local.replace(hour=0, minute=0, second=0, microsecond=0) \
        + datetime.timedelta(hours=QUIET_END_H)
    if local.hour >= QUIET_START_H:
        target += datetime.timedelta(days=1)
    return target.timestamp() - off


def cap_delay(user_id: int, category: str, now: float) -> float:
    """0 — слать можно; иначе через сколько секунд лимит освободится.

    Один запрос на сообщение: скользящее окно суток по уже отправленному. Не
    счётчик в users — счётчик пришлось бы обнулять по расписанию, и игрок,
    выбравший лимит в 23:59, получал бы вторую пачку в 00:01."""
    rows = _db().q(
        "SELECT category, sent_at FROM notification_queue "
        "WHERE user_id = ? AND sent_at > ? AND status IN ('sent', 'opened') "
        "ORDER BY sent_at", (user_id, now - 86400))
    if not rows:
        return 0.0
    delay = 0.0
    last = rows[-1]["sent_at"]
    if now - last < MIN_GAP_S:
        delay = max(delay, last + MIN_GAP_S - now)
    if len(rows) >= DAILY_CAP:
        delay = max(delay, rows[-DAILY_CAP]["sent_at"] + 86400 - now)
    same = [r for r in rows if r["category"] == category]
    cap = CATEGORY_CAP.get(category, DEFAULT_CATEGORY_CAP)
    if len(same) >= cap:
        delay = max(delay, same[-cap]["sent_at"] + 86400 - now)
    return max(0.0, delay)


# ---------- запись в очередь ----------

def enqueue(user_id: int, kind: str, payload: dict | None = None, *,
            dedup_key: str, scheduled_at: float | None = None) -> bool:
    """Положить задачу. False — такая уже лежит (повтор прохода планировщика).

    Ключ дедупликации обязателен и передаётся явно: он описывает СОБЫТИЕ, а не
    вид сообщения, и знает о событии только вызывающий."""
    now = time.time()
    ok = _db().q1w(
        "INSERT INTO notification_queue (user_id, kind, category, payload, "
        "scheduled_at, status, dedup_key, attempts, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, 0, ?) "
        "ON CONFLICT (dedup_key) DO NOTHING RETURNING id",
        (user_id, kind, category_of(kind), json.dumps(payload or {}),
         scheduled_at if scheduled_at is not None else now, dedup_key, now))
    if ok:
        obs.inc("notifications_total", result="queued")
    return ok is not None


def _enqueue_bulk(kind: str, dedup_sql: str, where: str, params: list,
                  payload: dict, now: float, limit: int = PLAN_CHUNK) -> int:
    """Массовая постановка одним INSERT ... SELECT (конец сезона, старт ивента).

    Здесь важны обе половины. `ON CONFLICT DO NOTHING` делает проход
    идемпотентным, а `NOT EXISTS` — конечным: без него каждый следующий проход
    брал бы тех же первых `limit` игроков и до остальных не дошёл бы никогда.
    Вместе они дают простое свойство: проход раз в 15 минут разбирает базу
    кусками и сам останавливается, когда разберёт всю.

    `dedup_sql` собирается из чисел и проверенных ключей — параметров в нём нет
    намеренно: он подставляется в запрос дважды, и разъехавшиеся половины
    означали бы вечно пустой `NOT EXISTS`."""
    return _db().exec(
        "INSERT INTO notification_queue (user_id, kind, category, payload, "
        "scheduled_at, status, dedup_key, attempts, created_at) "
        f"SELECT u.user_id, ?, ?, ?, ?, 'scheduled', {dedup_sql}, 0, ? "
        f"FROM users u WHERE {where} "
        f"AND NOT EXISTS (SELECT 1 FROM notification_queue q "
        f"WHERE q.dedup_key = {dedup_sql}) "
        "ORDER BY u.user_id LIMIT ? "
        "ON CONFLICT (dedup_key) DO NOTHING",
        [kind, category_of(kind), json.dumps(payload), now, now,
         *params, limit])


# ---------- чтение и смена состояния ----------

def claim(limit: int = BATCH, now: float | None = None,
          owner: str = "") -> list[dict]:
    """Партия задач, готовых к отправке. Каждая строка — ровно одному воркеру."""
    from server import scheduler          # локально: scheduler тянет cache
    now = time.time() if now is None else now
    return _db().claim_notifications(limit, now, owner or scheduler.OWNER)


def requeue_stale(now: float | None = None) -> int:
    """Вернуть в очередь заявки убитых воркеров.

    Без этого одна перезагрузка процесса подвешивает партию навсегда: строка
    осталась в 'sending', и её больше не возьмёт никто. Попытка при этом уже
    посчитана — бесконечного круга не будет."""
    now = time.time() if now is None else now
    n = _db().exec(
        "UPDATE notification_queue SET status = 'scheduled' "
        "WHERE status = 'sending' AND claimed_at < ?", (now - LEASE_S,))
    if n:
        log.warning("вернули в очередь %d зависших пушей", n)
        obs.inc("notifications_total", value=n, result="requeued")
    return n


def mark_sent(row: dict, now: float | None = None):
    now = time.time() if now is None else now
    d = _db()
    d.exec("UPDATE notification_queue SET status = 'sent', sent_at = ?, "
           "last_error = NULL WHERE id = ?", (now, row["id"]))
    # общий промежуток между пушами по-прежнему живёт в users: его читают и
    # старый проход, и админка, и он же индексирован под выборку кандидатов
    d.update_user(row["user_id"], last_notified_at=now)
    obs.inc("notifications_total", result="sent")


def mark_blocked(row: dict):
    """Игрок заблокировал бота: гасим и строку, и всю его будущую очередь."""
    d = _db()
    d.exec("UPDATE notification_queue SET status = 'blocked' WHERE id = ?",
           (row["id"],))
    d.exec("UPDATE notification_queue SET status = 'cancelled' "
           "WHERE user_id = ? AND status IN ('scheduled', 'sending')",
           (row["user_id"],))
    d.exec("UPDATE users SET notify_blocked = 1 WHERE user_id = ?",
           (row["user_id"],))
    obs.inc("notifications_total", result="blocked")


def cancel(row: dict, reason: str):
    """Событие протухло — сообщение не отправляем и не повторяем."""
    _db().exec("UPDATE notification_queue SET status = 'cancelled', "
               "last_error = ? WHERE id = ?", (reason[:300], row["id"]))
    obs.inc("notifications_total", result="cancelled")


def defer(row: dict, until: float, reason: str = ""):
    """Вернуть в очередь на другое время (тихие часы, частотный лимит).

    Попытка ОТКАТЫВАЕТСЯ: отложенное сообщение — это не неудачная отправка, и
    съедать им лимит ретраев значит терять сообщения тем аккуратнее, чем лучше
    работает политика тишины."""
    _db().exec(
        "UPDATE notification_queue SET status = 'scheduled', scheduled_at = ?, "
        "attempts = ?, last_error = ? WHERE id = ?",
        (until, max(0, (row.get("attempts") or 1) - 1), reason[:300], row["id"]))
    obs.inc("notifications_total", result="deferred")


def mark_failed(row: dict, error: str, retry_after: float | None = None,
                now: float | None = None):
    """Отправка не удалась. Пока попытки есть — назад в очередь.

    `retry_after` от Telegram уважается как есть: это не совет, а условие, при
    нарушении которого следующий ответ будет таким же, только злее."""
    now = time.time() if now is None else now
    attempts = row.get("attempts") or 1
    if retry_after is not None:
        wait = min(float(retry_after), MAX_RETRY_AFTER_S)
    else:
        wait = RETRY_BACKOFF_S[min(attempts, len(RETRY_BACKOFF_S)) - 1]
    if attempts >= MAX_ATTEMPTS and retry_after is None:
        _db().exec("UPDATE notification_queue SET status = 'failed', "
                   "last_error = ? WHERE id = ?", (error[:300], row["id"]))
        obs.inc("notifications_total", result="failed")
        return
    _db().exec(
        "UPDATE notification_queue SET status = 'scheduled', scheduled_at = ?, "
        "last_error = ? WHERE id = ?", (now + wait, error[:300], row["id"]))
    obs.inc("notifications_total", result="retry")


def mark_opened(notification_id: int, user_id: int,
                now: float | None = None) -> bool:
    """Игрок пришёл по кнопке. Зовётся из /auth по startParam диплинка.

    Условие по user_id обязательно: номер уведомления ходит в открытой ссылке,
    и без него любой мог бы пометить открытым чужое сообщение — а это метрика,
    по которой решают, слать такой пуш дальше или нет."""
    now = time.time() if now is None else now
    n = _db().exec(
        "UPDATE notification_queue SET status = 'opened', opened_at = ? "
        "WHERE id = ? AND user_id = ? AND status = 'sent'",
        (now, int(notification_id), user_id))
    if n:
        obs.inc("notifications_total", result="opened")
    return n == 1


def payload_of(row: dict) -> dict:
    try:
        data = json.loads(row.get("payload") or "{}")
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------- актуальность события ----------

def still_relevant(row: dict, user: dict, now: float) -> bool:
    """Проверка прямо перед отправкой: событие ещё живо?

    Между планированием и отправкой проходят часы, и почти каждое событие может
    закрыться само: тесто забрали, дуэль дозакрыли, сезон сменился."""
    kind, p = row["kind"], payload_of(row)
    if now - (row.get("created_at") or now) > ttl_of(kind):
        return False        # протухло по возрасту
    d = _db()

    if kind in ("recipe_ready", "recipe_burning"):
        st = gl.recipe_status(user, now)
        return (st["state"] == "ready"
                and int(user.get("recipe_started_at") or 0) == p.get("started"))
    if kind == "offline_cap":
        # ферму не собирали с момента планирования — метка не сдвинулась
        return int(user.get("farm_collected_at") or 0) == p.get("at")
    if kind == "comeback":
        # игрок не вернулся сам. Здесь именно <=, а не равенство: округлять
        # отметку до секунды нельзя — «вернулся» решается сравнением, и
        # усечённое до int значение всегда меньше собственного оригинала
        return (user.get("last_seen_at") or 0) <= p.get("seen", 0)
    if kind == "duel_ending":
        r = d.q1("SELECT status, ends_at FROM duels WHERE id = ?", (p.get("duel"),))
        return bool(r and r["status"] == "active" and r["ends_at"] > now)
    if kind == "duel_result":
        r = d.q1("SELECT status, user_a, claimed_a, claimed_b FROM duels "
                 "WHERE id = ?", (p.get("duel"),))
        if not r or r["status"] != "done":
            return False
        mine = "claimed_a" if r["user_a"] == user["user_id"] else "claimed_b"
        return not r[mine]
    if kind == "order_waiting":
        return bool(d.q1("SELECT id FROM orders WHERE id = ? AND status = 'active'",
                         (p.get("order"),)))
    if kind == "season_end":
        return gl.current_season(now) == p.get("season")
    if kind == "bp_unclaimed":
        return (user.get("season_id") == p.get("season")
                and unclaimed_bp(user) > 0)
    if kind == "event_start":
        ev = gl.active_event(now)
        return bool(ev and ev["key"] == p.get("event"))
    if kind == "streak":
        # награду за день ещё не забрали: отметка не сдвинулась. Сравнение по
        # секундам, потому что в payload лежит целое: отметки времени в базе
        # дробные, и точное сравнение float с int не совпало бы НИКОГДА
        return (int(user.get("daily_claimed_at") or 0) == p.get("claimed")
                and streak_due(user, now))
    if kind == "energy_full":
        return energy_full(user, now)
    return True


# Три исходных правила: стрик сгорает, ферма упёрлась в кап, энергия полная.
# Живут здесь по одной причине: одно и то же правило нужно И планировщику
# (кого ставить в очередь), И воркеру (актуально ли это ещё). Разъехавшись, они
# дали бы сообщения, отменяющие сами себя.

def streak_due(user: dict, now: float) -> bool:
    """Стрик сгорит сегодня: вчера забирал, сегодня ещё нет, до полуночи < 4ч."""
    if (user.get("daily_streak") or 0) < 2 or not user.get("daily_claimed_at"):
        return False
    dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    left = ((24 - dt.hour) * 3600) - dt.minute * 60 - dt.second
    last_day = datetime.datetime.fromtimestamp(
        user["daily_claimed_at"], datetime.timezone.utc).strftime("%Y-%m-%d")
    yesterday = (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    return last_day == yesterday and left < 4 * 3600


def farm_idle(user: dict, now: float) -> bool:
    """Ферма упёрлась в оффлайн-кап и доход простаивает."""
    if not user.get("farm_collected_at"):
        return False
    idle_h = (now - user["farm_collected_at"]) / 3600
    return (idle_h >= gl.farm_offline_cap_hours(user)
            and gl.farm_cps(user["user_id"]) > 0)


def energy_full(user: dict, now: float) -> bool:
    eff = gl.upgrade_effects(user["user_id"])
    cap = gl.energy_cap(user, eff)
    regen = cfg.ENERGY_REGEN_PER_SEC + eff["energy_regen"]
    energy = min(cap, (user["energy"] or 0)
                 + (now - (user["energy_updated_at"] or now)) * regen)
    return energy >= cap


def unclaimed_bp(user: dict) -> int:
    """Сколько наград пасса игрок уже заслужил, но не забрал."""
    reached = cfg.bp_level_for_xp(user.get("bp_xp") or 0)
    if reached < 1:
        return 0
    tracks = 2 if user.get("bp_premium") else 1
    taken = _db().q1(
        "SELECT COUNT(*) c FROM bp_claims WHERE user_id = ? AND season_id = ? "
        "AND level <= ?", (user["user_id"], user.get("season_id") or 0,
                           reached))["c"]
    return max(0, reached * tracks - taken)


# ---------- планировщик ----------

def _fresh(now: float) -> str:
    """Условие «игрок не в игре прямо сейчас» — общая часть фоновых триггеров."""
    return f"u.last_seen_at < {now - cfg.NOTIFY_SKIP_ACTIVE_H * 3600:.0f}"


def _plan_recipes(now: float) -> int:
    """Тесто подошло / вот-вот подгорит.

    Кандидатов ищем по частичному индексу закваски, а не по всей таблице:
    закваска стоит у единиц процентов аудитории. Точное состояние (готово,
    рано, подгорело) считает та же функция, что и в игре, — по одному конфигу.
    """
    d, made = _db(), 0
    longest = max(r["hours"] * r["window"] for r in cfg.RECIPES.values())
    for kind in ("recipe_ready", "recipe_burning"):
        rows = d.q(
            "SELECT user_id, lang, recipe_key, recipe_started_at, "
            "       tz_offset_min FROM users u "
            "WHERE recipe_key IS NOT NULL AND notify_blocked = 0 "
            "AND recipe_started_at > ? AND recipe_started_at <= ? "
            f"AND {_fresh(now)} "
            "AND NOT EXISTS (SELECT 1 FROM notification_queue q WHERE q.dedup_key = "
            f"  '{kind}:' || u.user_id || ':' || CAST(u.recipe_started_at AS INTEGER)) "
            "ORDER BY recipe_started_at LIMIT ?",
            (now - longest * 3600, now, PLAN_CHUNK))
        for u in rows:
            st = gl.recipe_status(u, now)
            if st["state"] != "ready":
                continue        # рано или уже подгорело — сообщать не о чем
            if kind == "recipe_burning" and st["spoils_at"] - now > 3600:
                continue        # про подгорание предупреждаем за час, не раньше
            started = int(u["recipe_started_at"])
            if enqueue(u["user_id"], kind,
                       {"started": started, "mult": round(st["mult"], 2)},
                       dedup_key=f"{kind}:{u['user_id']}:{started}"):
                made += 1
    return made


def _plan_offline_cap(now: float) -> int:
    """Оффлайн-кап заполняется: доход упёрся в потолок и простаивает.

    Порог 80% от капа, а не 100%: смысл сообщения в том, чтобы игрок успел
    забрать доход ДО того, как часы начнут пропадать."""
    cap_s = cfg.FARM_OFFLINE_CAP_HOURS * 3600 * 0.8
    rows = _db().q(
        "SELECT user_id, lang, farm_collected_at, tz_offset_min FROM users u "
        "WHERE notify_blocked = 0 AND farm_collected_at > 0 "
        "AND farm_collected_at + ? + offline_bonus_hours * 2880 <= ? "
        f"AND {_fresh(now)} "
        "AND EXISTS (SELECT 1 FROM farm f WHERE f.user_id = u.user_id AND f.count > 0) "
        "AND NOT EXISTS (SELECT 1 FROM notification_queue q WHERE q.dedup_key = "
        "  'offline_cap:' || u.user_id || ':' || CAST(u.farm_collected_at AS INTEGER)) "
        "ORDER BY farm_collected_at LIMIT ?", (cap_s, now, PLAN_CHUNK))
    made = 0
    for u in rows:
        at = int(u["farm_collected_at"])
        if enqueue(u["user_id"], "offline_cap", {"at": at},
                   dedup_key=f"offline_cap:{u['user_id']}:{at}"):
            made += 1
    return made


# Дни возвращения. Окна не пересекаются (полсуток шириной при шаге в сутки),
# поэтому игрок за один проход попадает ровно в одно.
COMEBACK_DAYS = (2, 3, 7, 14)


def _plan_comeback(now: float) -> int:
    made = 0
    for day in COMEBACK_DAYS:
        lo, hi = now - (day + 0.5) * 86400, now - day * 86400
        rows = _db().q(
            "SELECT user_id, lang, last_seen_at, tz_offset_min FROM users u "
            "WHERE notify_blocked = 0 AND last_seen_at > ? AND last_seen_at <= ? "
            "AND NOT EXISTS (SELECT 1 FROM notification_queue q WHERE q.dedup_key = "
            f"  'comeback:' || u.user_id || ':' || CAST(u.last_seen_at AS INTEGER) "
            f"  || ':{day}') "
            "ORDER BY last_seen_at LIMIT ?", (lo, hi, PLAN_CHUNK))
        for u in rows:
            # в ключ идёт секунда (его же строит SQL через CAST), а в payload —
            # исходная отметка: по ней потом решается, вернулся ли игрок сам
            if enqueue(u["user_id"], "comeback",
                       {"days": day, "seen": u["last_seen_at"]},
                       dedup_key=f"comeback:{u['user_id']}:"
                                 f"{int(u['last_seen_at'])}:{day}"):
                made += 1
    return made


def _plan_duels(now: float) -> int:
    """Дуэль заканчивается (за час до конца) и дуэль закончилась."""
    d, made = _db(), 0
    ending = d.q("SELECT id, user_a, user_b, ends_at FROM duels "
                 "WHERE status = 'active' AND ends_at > ? AND ends_at <= ? "
                 "LIMIT ?", (now, now + 3600, PLAN_CHUNK))
    for row in ending:
        for uid in (row["user_a"], row["user_b"]):
            if uid and enqueue(uid, "duel_ending", {"duel": row["id"]},
                               dedup_key=f"duel_ending:{row['id']}:{uid}"):
                made += 1
    done = d.q("SELECT id, user_a, user_b, claimed_a, claimed_b FROM duels "
               "WHERE status = 'done' AND ends_at > ? "
               "AND (claimed_a = 0 OR claimed_b = 0) LIMIT ?",
               (now - 86400, PLAN_CHUNK))
    for row in done:
        for uid, claimed in ((row["user_a"], row["claimed_a"]),
                             (row["user_b"], row["claimed_b"])):
            if uid and not claimed and enqueue(
                    uid, "duel_result", {"duel": row["id"]},
                    dedup_key=f"duel_result:{row['id']}:{uid}"):
                made += 1
    return made


def _plan_orders(now: float) -> int:
    """Взятый заказ висит без дела: игрок вложился и забыл."""
    idle = now - 20 * 3600
    rows = _db().q(
        "SELECT o.id, o.user_id FROM orders o JOIN users u "
        "  ON u.user_id = o.user_id "
        "WHERE o.status = 'active' AND o.created_at <= ? AND u.notify_blocked = 0 "
        f"AND {_fresh(now)} "
        "AND NOT EXISTS (SELECT 1 FROM notification_queue q WHERE q.dedup_key = "
        "  'order_waiting:' || o.id) "
        "ORDER BY o.created_at LIMIT ?", (idle, PLAN_CHUNK))
    made = 0
    for row in rows:
        if enqueue(row["user_id"], "order_waiting", {"order": row["id"]},
                   dedup_key=f"order_waiting:{row['id']}"):
            made += 1
    return made


def _plan_season_end(now: float) -> int:
    """Сезон заканчивается: одно сообщение всем живым за сутки до конца."""
    season = gl.current_season(now)
    left = gl.season_end_ts(season) - now
    if left <= 0 or left > 24 * 3600:
        return 0
    return _enqueue_bulk(
        "season_end", f"'season_end:{season}:' || u.user_id",
        "u.notify_blocked = 0 AND u.last_seen_at > ? AND u.season_id = ?",
        [now - 14 * 86400, season],
        {"season": season, "hours": max(1, int(left // 3600))}, now)


def _plan_bp_unclaimed(now: float) -> int:
    """Незабранные награды пасса — только когда они реально вот-вот сгорят."""
    season = gl.current_season(now)
    left = gl.season_end_ts(season) - now
    if left <= 0 or left > 3 * 86400:
        return 0
    rows = _db().q(
        "SELECT user_id, lang, bp_xp, bp_premium, season_id, tz_offset_min "
        "FROM users u WHERE notify_blocked = 0 AND bp_xp > 0 AND season_id = ? "
        "AND last_seen_at > ? "
        "AND NOT EXISTS (SELECT 1 FROM notification_queue q WHERE q.dedup_key = "
        f"  'bp_unclaimed:{season}:' || u.user_id) "
        "ORDER BY user_id LIMIT ?", (season, now - 14 * 86400, PLAN_CHUNK))
    made = 0
    for u in rows:
        count = unclaimed_bp(u)
        if count and enqueue(u["user_id"], "bp_unclaimed",
                             {"season": season, "count": count},
                             dedup_key=f"bp_unclaimed:{season}:{u['user_id']}"):
            made += 1
    return made


def _plan_event_start(now: float) -> int:
    """Старт ивента выходных. Ивент детерминирован календарём, поэтому окно
    «первые 6 часов» и есть его старт — отдельной отметки не нужно."""
    ev = gl.active_event(now)
    if not ev or now - ev["started_at"] > 6 * 3600:
        return 0
    key = "".join(ch for ch in ev["key"] if ch.isalnum() or ch == "_")
    return _enqueue_bulk(
        "event_start", f"'event_start:{key}:{int(ev['started_at'])}:' || u.user_id",
        "u.notify_blocked = 0 AND u.last_seen_at > ?",
        [now - 14 * 86400], {"event": ev["key"], "mult": ev["mult"]}, now)


def _plan_legacy(now: float) -> int:
    """Стрик и полная энергия — три исходных триггера бота.

    Окно кандидатов ограничено тремя сутками намеренно: за ним игрока ведёт
    comeback, а без границы этот триггер снова стал бы полным проходом по
    таблице игроков — ровно тем, ради чего очередь и заводилась."""
    day = gl._utc_day(now)
    rows = _db().q(
        "SELECT * FROM users u WHERE notify_blocked = 0 "
        "AND last_seen_at > ? AND last_seen_at < ? "
        "ORDER BY last_seen_at DESC LIMIT ?",
        (now - 3 * 86400, now - cfg.NOTIFY_SKIP_ACTIVE_H * 3600, PLAN_CHUNK))
    made = 0
    for u in rows:
        uid = u["user_id"]
        if streak_due(u, now):
            claimed = int(u["daily_claimed_at"])
            if enqueue(uid, "streak",
                       {"days": u["daily_streak"], "claimed": claimed},
                       dedup_key=f"streak:{uid}:{day}"):
                made += 1
            continue        # приоритет: стрик важнее фермы и энергии
        if farm_idle(u, now):
            at = int(u["farm_collected_at"])
            if enqueue(uid, "offline_cap", {"at": at},
                       dedup_key=f"offline_cap:{uid}:{at}"):
                made += 1
            continue
        if energy_full(u, now) and enqueue(uid, "energy_full", {},
                                           dedup_key=f"energy_full:{uid}:{day}"):
            made += 1
    return made


PLANNERS = (
    ("recipes", _plan_recipes),
    ("legacy", _plan_legacy),
    ("offline_cap", _plan_offline_cap),
    ("comeback", _plan_comeback),
    ("duels", _plan_duels),
    ("orders", _plan_orders),
    ("season_end", _plan_season_end),
    ("bp_unclaimed", _plan_bp_unclaimed),
    ("event_start", _plan_event_start),
)


def plan_pass(now: float | None = None) -> dict:
    """Проход планировщика: только запись в очередь, ни одного сообщения.

    Сломанный триггер не отменяет остальные — иначе одна опечатка в дуэлях
    оставляла бы без сообщений всю игру. Но и молчать нельзя: ошибки собираются
    и улетают наверх, где их запишет журнал задач (scheduler.job)."""
    now = time.time() if now is None else now
    requeue_stale(now)
    made, errors = {}, []
    for name, fn in PLANNERS:
        try:
            made[name] = fn(now)
        except Exception as e:              # noqa: BLE001
            log.exception("планировщик пушей: %s упал", name)
            errors.append(f"{name}: {type(e).__name__}: {e}")
    total = sum(made.values())
    if total:
        log.info("в очередь пушей добавлено %d: %s", total,
                 {k: v for k, v in made.items() if v})
    if errors:
        raise RuntimeError("; ".join(errors)[:500])
    return made


def prune(now: float | None = None) -> int:
    """TTL очереди: разобранные строки живут TTL_DAYS и удаляются.

    Строка нужна ровно до тех пор, пока по ней могут прийти ретрай или
    открытие. Незакрытые статусы не трогаем: 'scheduled' старше TTL — это
    сигнал, что воркер стоит, и удалять такое молча нельзя."""
    now = time.time() if now is None else now
    return _db().exec(
        "DELETE FROM notification_queue WHERE created_at < ? "
        "AND status IN ('sent', 'opened', 'failed', 'blocked', 'cancelled')",
        (now - TTL_DAYS * 86400,))


def stats(now: float | None = None) -> dict:
    """Сводка по очереди — для /healthz и разбора «почему молчат пуши»."""
    now = time.time() if now is None else now
    rows = _db().q("SELECT status, COUNT(*) c FROM notification_queue "
                   "GROUP BY status")
    out = {r["status"]: r["c"] for r in rows}
    oldest = _db().q1("SELECT MIN(scheduled_at) t FROM notification_queue "
                      "WHERE status = 'scheduled' AND scheduled_at <= ?", (now,))
    if oldest and oldest["t"]:
        out["oldest_due_age"] = round(now - oldest["t"], 1)
    return out
