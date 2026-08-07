"""Приватность и права игрока на свои данные: /privacy, /terms, экспорт, удаление.

Зачем отдельный модуль. Раньше в приложении не было ни страницы политики, ни
способа забрать свои данные, ни способа уйти. Для магазина приложений и для
любого европейского игрока это блокер запуска, а дописывать такие вещи в
игровые роутеры нельзя: они живут по другим правилам (лимиты, ревизии
состояния, книга операций), и правило «удали игроку всё» слишком легко
превращается там в «удали игроку всё, включая платёжную историю».

Три вещи, которые здесь важнее кода:

1. Текст страниц описывает то, что код ДЕЙСТВИТЕЛЬНО делает. Перечень данных
   собран по схеме в `db.py` и по вызовам `game_logic.track`, сроки хранения —
   по константам `EVENTS_TTL_DAYS`, `OPS_TTL_DAYS`, `BACKUP_KEEP`. Красивая
   политика, обещающая то, чего в коде нет, хуже её отсутствия: это уже не
   недоработка, а заявление, которое можно проверить.

2. Удаление НЕ трогает книгу операций и платежи. `economy_ledger` физически
   защищена триггером (append-only): её нельзя ни удалить, ни переписать —
   ровно поэтому её нельзя и «анонимизировать» апдейтом. Обезличивание делается
   с другой стороны: строка игрока в `users` остаётся надгробием без имени,
   и числовой идентификатор в книге перестаёт на кого-либо указывать.

3. Повторное нажатие «удалить» не должно ни падать, ни списывать баланс второй
   раз. Защёлка — условный UPDATE самой строки игрока: он берёт блокировку
   строки и на обоих движках возвращает 0 второму запросу.
"""
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from server import economy
from server import game_config as cfg
from server import game_logic as gl
from server.auth import tg_admin, tg_user
from server.game_logic import db

router = APIRouter()
log = logging.getLogger(__name__)

# Имя, которое остаётся в строке удалённого игрока. Оно же — признак удаления:
# отдельной колонки под флаг нет, а добавлять её значило бы править db.py,
# который сейчас в чужих руках. Проверка идёт по нему и по пустому username.
TOMBSTONE = "[deleted]"

# Куда писать след административного доступа. Отдельной таблицы нет намеренно:
# админ в проекте один, объём — единицы строк в день, а analytics_events уже
# есть, уже чистится по расписанию и уже видна админке. Цена решения честная и
# указана на странице политики: журнал живёт столько же, сколько аналитика,
# поэтому каждое обращение дублируется в лог приложения, у которого свой срок.
ADMIN_ACCESS_EVENT = "admin_access"

# Таблицы, где строка принадлежит ИГРОКУ и удаляется вместе с аккаунтом.
# Порядок значения не имеет: внешних ключей между ними нет.
WIPED_TABLES = (
    "board", "farm", "upgrades", "skins", "collection", "orders",
    "achievements", "daily_quests", "bp_claims", "ref_claims",
    "boosts", "entitlements", "click_batches", "economy_ops",
    "analytics_events", "season_results",
)

# Что читает экспорт: таблица -> колонка с идентификатором игрока. Дуэли и
# рефералы ссылаются на игрока двумя колонками и разобраны отдельно.
EXPORT_TABLES = (
    ("analytics_events", "user_id"),
    ("orders", "user_id"),
    ("collection", "user_id"),
    ("click_batches", "user_id"),
    ("farm", "user_id"),
    ("upgrades", "user_id"),
    ("skins", "user_id"),
    ("board", "user_id"),
    ("achievements", "user_id"),
    ("daily_quests", "user_id"),
    ("bp_claims", "user_id"),
    ("ref_claims", "user_id"),
    ("boosts", "user_id"),
    ("entitlements", "user_id"),
    ("promo_redemptions", "user_id"),
    ("purchases", "user_id"),
    ("season_results", "user_id"),
    ("economy_ledger", "user_id"),
    ("economy_ops", "user_id"),
    ("economy_opening", "user_id"),
)

# Валюты, по которым сверка требует равенства «сумма движений = колонка»
# (см. economy.CURRENCY_COLUMN и LEDGERED_PARTIAL). Обнулять их прямым UPDATE
# нельзя: расхождение осталось бы навсегда и ежедневно светилось бы в
# drift_report. Обнуление проводится ЧЕРЕЗ книгу — это законная трата.
BURNED_CURRENCIES = (
    ("cookies", "cookies"),
    ("xp", "xp"),
    ("prestige_points", "prestige_points"),
    ("offline_hours", "offline_bonus_hours"),
)

# Игровые колонки, которые обнуляются напрямую: в сверку они не входят
# (energy и bp_xp — исключения книги, остальное просто счётчики и отметки).
# cookie_debt здесь НЕТ намеренно: долг после возврата Stars — это учёт, а не
# прогресс, и обнулять его удалением аккаунта значит выдавать бесплатный товар.
SCRUBBED_COLUMNS = {
    "username": None, "first_name": TOMBSTONE, "lang": "en",
    "source_code": None, "referrer_id": None,
    "total_clicks": 0, "total_merges": 0, "best_item_level": 0,
    "click_level": 1, "energy": 0, "level": 1, "bp_xp": 0,
    "bp_premium": 0, "bp_premium_next": 0, "rest_day": None, "rest_mult": 1,
    "active_skin": "classic", "season_earned": 0, "daily_streak": 0,
    "daily_claimed_at": 0, "notify_blocked": 1, "channel_claimed": 0,
    "golden_next_at": 0, "golden_expires_at": 0, "golden_effect": None,
    "combo_mult": 1, "combo_last_at": 0, "prestige_count": 0,
    "clicks_day": None, "clicks_day_count": 0, "quest_reroll_day": None,
    "streak_freeze_week": None, "shiny_pity": 0, "recipe_key": None,
    "recipe_started_at": 0, "tutorial_done": 0, "orders_completed": 0,
    "orders_day": None, "orders_day_count": 0,
}


# ---------- журнал административного доступа ----------

def _access_target(request: Request) -> int:
    """Кого именно смотрели, если это следует из запроса.

    В админке сейчас нет ручки «покажи игрока N», но поиск по игроку — первое,
    что там появится (см. пункт 31 плана), и журнал должен быть готов раньше,
    а не после. Пока источников два: параметр пути и строка запроса."""
    for source in (request.path_params, request.query_params):
        for key in ("user_id", "uid", "player_id"):
            raw = source.get(key)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                return 0
    return 0


async def admin_audit(request: Request, admin: dict = Depends(tg_admin)) -> dict:
    """След «кто и куда ходил» на КАЖДЫЙ запрос админки.

    Вешается на роутер целиком в main.py, а не декоратором на ручки: ручку
    можно забыть прикрыть, и забудут её ровно ту, из-за которой журнал и
    заводили. В admin.py своего журналирования нет вовсе (там пишутся только
    изменения — промокоды, источники, рассылка), поэтому второго механизма это
    не создаёт.

    Пишет и в базу, и в лог приложения. В базе строку видно из админки, но её
    заберёт TTL аналитики; в логе она живёт по правилам ротации логов и
    переживает TTL.

    Падение записи не отменяет запрос: журнал доступа не должен становиться
    способом уронить админку."""
    path = request.scope.get("route")
    path = getattr(path, "path", None) or request.url.path
    target = _access_target(request)
    if gl.track(f"{ADMIN_ACCESS_EVENT}:{request.method} {path}", admin["id"],
                value=target, target_user_id=target) is None:
        log.warning("журнал админского доступа не записан")
    log.info("админский доступ: admin=%s %s %s target=%s",
             admin["id"], request.method, path, target or "-")
    return admin


@router.get("/api/admin/access-log", dependencies=[Depends(admin_audit)])
async def access_log(limit: int = 200):
    """Кто из администраторов и к чему обращался. Только для ADMIN_ID.

    Хранится столько же, сколько аналитика (EVENTS_TTL_DAYS), — об этом прямо
    сказано на странице политики; более длинная история собирается из логов
    приложения по строке «админский доступ»."""
    limit = max(1, min(int(limit), 1000))
    rows = db.q("SELECT user_id AS admin_id, event, value AS target_user_id, "
                "created_at FROM analytics_events WHERE event LIKE ? "
                "ORDER BY created_at DESC LIMIT ?",
                (ADMIN_ACCESS_EVENT + ":%", limit))
    return {"access_log": rows, "count": len(rows),
            "retention_days": cfg.EVENTS_TTL_DAYS}


# ---------- экспорт ----------

@router.get("/api/legal/export")
async def export_data(tg: dict = Depends(tg_user)):
    """Всё, что о звонящем хранится в базе, одним JSON.

    Тяжёлая ручка (полная книга операций игрока), поэтому со своим лимитом
    поверх общего: она читается редко и вручную."""
    gl.check_rate_limit(tg["id"], "legal_export", 5, 3600)
    uid = tg["id"]
    user = db.get_user(uid)
    if not user:
        raise HTTPException(404, "err_no_user")

    data = {t: db.q(f"SELECT * FROM {t} WHERE {col} = ? ORDER BY 1", (uid,))
            for t, col in EXPORT_TABLES}
    # обе стороны связей: приглашения и дуэли описывают ДВУХ игроков, и в
    # выгрузке одного из них должны быть видны с его стороны
    data["referrals"] = db.q(
        "SELECT * FROM referrals WHERE referrer_id = ? OR referred_id = ?",
        (uid, uid))
    data["duels"] = db.q("SELECT * FROM duels WHERE user_a = ? OR user_b = ?",
                         (uid, uid))
    return {
        "generated_at": time.time(),
        "telegram_user_id": uid,
        "account": user,
        "data": data,
        "retention": _retention(),
        "notes": {
            "ru": "Это полная выгрузка строк базы, относящихся к вашему "
                  "аккаунту. Платёжные данные (номер карты, имя владельца) "
                  "здесь отсутствуют потому, что приложение их не получает: "
                  "оплату проводит Telegram, нам достаётся идентификатор "
                  "платежа и сумма в Stars.",
            "en": "A verbatim dump of every database row tied to your "
                  "account. No card data or billing name is present because "
                  "the app never receives it: Telegram processes the payment "
                  "and we only see the charge id and the Stars amount.",
        },
    }


def _retention() -> dict:
    """Сроки — из конфига, а не из текста. Поменяют константу — поменяется и
    ответ ручки, и таблица на странице политики."""
    return {
        "analytics_events_days": cfg.EVENTS_TTL_DAYS,
        "analytics_export_grace_days": cfg.ANALYTICS_EXPORT_GRACE_DAYS,
        "idempotency_tokens_days": cfg.OPS_TTL_DAYS,
        "click_batches_hours": 1,
        "backup_snapshots": cfg.BACKUP_KEEP,
        "backup_interval_hours": cfg.BACKUP_INTERVAL_H,
        "backup_max_age_days": round(cfg.BACKUP_KEEP * cfg.BACKUP_INTERVAL_H / 24, 1),
    }


# ---------- удаление ----------

class DeleteRequest(BaseModel):
    # Слово подтверждения обязательно: удаление аккаунта не должно случаться от
    # промаха по кнопке или от чужого CSRF-подобного запроса.
    confirm: str = Field(max_length=32)


@router.post("/api/legal/delete")
async def delete_account(body: DeleteRequest, tg: dict = Depends(tg_user)):
    """Самостоятельное удаление аккаунта.

    Что происходит: игровые строки удаляются, строка в `users` остаётся
    надгробием без имени и без прогресса, книга операций и платежи остаются
    нетронутыми. Подробный разбор — в docstring `_delete` и на /privacy."""
    if body.confirm.strip().upper() != "DELETE":
        raise HTTPException(400, "err_confirm_required")
    gl.check_rate_limit(tg["id"], "legal_delete", 5, 3600)
    uid = tg["id"]
    if not db.get_user(uid):
        raise HTTPException(404, "err_no_user")
    return _delete(uid)


def _delete(uid: int) -> dict:
    """Одна транзакция: защёлка, списание в книгу, зачистка, запись о факте.

    Защёлка от двойной отправки — первый же UPDATE с условием
    `first_name <> TOMBSTONE`. Он берёт блокировку строки, и второй запрос либо
    ждёт (PostgreSQL), либо выстраивается в очередь писателей (SQLite
    BEGIN IMMEDIATE), а затем ПЕРЕПРОВЕРЯЕТ условие и получает 0 строк. Проверка
    же вида «сначала SELECT, потом UPDATE» здесь не годится: два запроса из
    двух вкладок оба увидели бы живой аккаунт и оба списали бы баланс в книгу.

    COALESCE в условии не косметика: `NULL <> '[deleted]'` даёт NULL, то есть
    ложь, и игрок без имени (в Telegram оно необязательно) не смог бы удалиться.

    Баланс гасится ЧЕРЕЗ книгу, а не обнулением колонки: `economy.reconcile`
    сравнивает сумму движений с колонкой, и прямой UPDATE оставил бы вечное
    расхождение — то есть ежедневную ложную тревогу об утечке валюты.
    """
    now = time.time()
    with db.tx():
        latched = db.exec(
            "UPDATE users SET first_name = ?, username = NULL "
            "WHERE user_id = ? AND COALESCE(first_name, '') <> ?",
            (TOMBSTONE, uid, TOMBSTONE))
        if latched == 0:
            # уже надгробие: повтор отдаёт тот же ответ и ничего не делает
            log.info("повторный запрос на удаление аккаунта %s — уже удалён", uid)
            return {"ok": True, "already_deleted": True, "deleted_rows": {},
                    "kept": _kept_explained()}

        user = db.get_user(uid)
        op_id = f"gdpr_delete:{uid}:{int(now)}"
        burned = {}
        for currency, column in BURNED_CURRENCIES:
            amount = float(user.get(column) or 0)
            if abs(amount) < 1e-9:
                continue
            economy.record(uid, currency, -amount, "account_deleted",
                           balance_after=0.0, operation_id=op_id,
                           ref_type="account", ref_id=str(uid))
            burned[currency] = amount

        fields = dict(SCRUBBED_COLUMNS)
        for _currency, column in BURNED_CURRENCIES:
            fields[column] = 0
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.exec(f"UPDATE users SET {cols}, user_revision = user_revision + 1 "
                f"WHERE user_id = ?", (*fields.values(), uid))

        deleted = {}
        for table in WIPED_TABLES:
            n = db.exec(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
            if n:
                deleted[table] = n
        # заявка в очереди дуэлей — единственная строка duels, которая целиком
        # наша: соперника в ней ещё нет, и удаление никого не обкрадывает
        n = db.exec("DELETE FROM duels WHERE user_a = ? AND "
                    "(user_b IS NULL OR user_b = 0) AND status = 'waiting'", (uid,))
        if n:
            deleted["duels_waiting"] = n

        # Отметка о самом удалении — ПОСЛЕ зачистки analytics_events, иначе её
        # же и снесло бы. Это единственная строка аналитики, которая остаётся.
        db.exec("INSERT INTO analytics_events (event_id, user_id, event, value, "
                "created_at, config_version, exported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 0)",
                (uuid.uuid4().hex, uid, "account_deleted", 1, now,
                 cfg.CONFIG_VERSION))

    log.warning("аккаунт %s удалён по запросу игрока: строк удалено %s, "
                "списано в книгу %s", uid, sum(deleted.values()), burned)
    return {"ok": True, "already_deleted": False, "deleted_rows": deleted,
            "burned": burned, "kept": _kept_explained()}


def _kept_explained() -> dict:
    """Что осталось и почему. Ответ ручки повторяет страницу политики дословно:
    человек, нажавший «удалить», не обязан идти читать её отдельно."""
    return {
        "economy_ledger": "Книга движений валюты. Защищена триггером базы как "
                          "append-only: строку нельзя ни удалить, ни изменить. "
                          "Нужна для сверки экономики и разбора споров. "
                          "Остаётся только числовой идентификатор Telegram.",
        "purchases": "Платежи Stars: сумма, идентификатор платежа Telegram, "
                     "статус, что было выдано. Нужны для бухгалтерии и "
                     "возвратов — возврат Stars оформляется по этой строке.",
        "economy_opening": "Входящие остатки: без них книга не сходится.",
        "promo_redemptions": "Факт активации промокода — защита от повторной "
                             "активации того же кода.",
        "referrals": "Кто кого пригласил. Это одновременно запись ВТОРОГО "
                     "игрока и признак накрутки рефералов.",
        "duels": "Завершённые и активные дуэли: в строке два игрока.",
        "users": "Строка остаётся ОБЕЗЛИЧЕННОЙ: имя и username стёрты, "
                 "прогресс обнулён. Она нужна, чтобы идентификатор в книге и "
                 "платежах не указывал в пустоту.",
        "cookie_debt": "Долг после возврата Stars не обнуляется: это учёт, а "
                       "не прогресс.",
    }


# ---------- страницы ----------

_PAGE_CSS = """
:root { color-scheme: light dark; }
body { margin: 0 auto; padding: 24px 16px 64px; max-width: 760px;
       font: 16px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 20px; margin: 32px 0 8px; }
h3 { font-size: 17px; margin: 20px 0 6px; }
code { font-size: 90%; padding: 1px 4px; border-radius: 4px;
       background: rgba(127,127,127,.18); }
table { border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 15px; }
th, td { border: 1px solid rgba(127,127,127,.35); padding: 6px 8px;
         text-align: left; vertical-align: top; }
.upd { opacity: .7; font-size: 14px; margin: 0 0 24px; }
.lang { margin: 24px 0; padding-top: 16px; border-top: 2px solid rgba(127,127,127,.35); }
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{title}</title><style>{_PAGE_CSS}</style></head>"
        f"<body>{body}</body></html>")


def _retention_rows_ru() -> str:
    r = _retention()
    return (
        "<table><tr><th>Что</th><th>Сколько хранится</th><th>Чем задано</th></tr>"
        f"<tr><td>Аналитические события (<code>analytics_events</code>)</td>"
        f"<td>{r['analytics_events_days']} суток после выгрузки в наше "
        f"аналитическое хранилище, затем удаляются задачей "
        f"<code>events_prune</code>; ещё не выгруженные — не дольше "
        f"{r['analytics_events_days'] + r['analytics_export_grace_days']} суток"
        f"</td><td><code>EVENTS_TTL_DAYS</code>, "
        f"<code>ANALYTICS_EXPORT_GRACE_DAYS</code></td></tr>"
        f"<tr><td>Токены повторной отправки с сохранёнными ответами "
        f"(<code>economy_ops</code>)</td><td>{r['idempotency_tokens_days']} суток"
        f"</td><td><code>OPS_TTL_DAYS</code></td></tr>"
        f"<tr><td>Отметки обработанных пачек кликов (<code>click_batches</code>)"
        f"</td><td>{r['click_batches_hours']} час</td><td>чистится при "
        f"следующей пачке кликов</td></tr>"
        f"<tr><td>Резервные копии базы (зашифрованные AES-256-GCM)</td>"
        f"<td>{r['backup_snapshots']} снимков раз в "
        f"{int(r['backup_interval_hours'])} ч — до "
        f"{r['backup_max_age_days']} суток</td>"
        f"<td><code>BACKUP_KEEP</code>, <code>BACKUP_INTERVAL_H</code></td></tr>"
        f"<tr><td>Журнал административного доступа</td>"
        f"<td>{r['analytics_events_days']} суток в базе; дольше — в логах "
        f"приложения</td><td><code>EVENTS_TTL_DAYS</code></td></tr>"
        "<tr><td>Профиль, доска, ферма, заказы, достижения</td>"
        "<td>пока существует аккаунт</td><td>удаляются по запросу игрока</td></tr>"
        "<tr><td>Книга операций и платежи</td><td>бессрочно</td>"
        "<td>бухгалтерия и защита от мошенничества</td></tr></table>")


def _retention_rows_en() -> str:
    r = _retention()
    return (
        "<table><tr><th>Data</th><th>Retention</th><th>Set by</th></tr>"
        f"<tr><td>Analytics events (<code>analytics_events</code>)</td>"
        f"<td>{r['analytics_events_days']} days after the row is exported to "
        f"our own analytics storage, then deleted by the "
        f"<code>events_prune</code> job; rows not yet exported are kept no "
        f"longer than "
        f"{r['analytics_events_days'] + r['analytics_export_grace_days']} days"
        f"</td><td><code>EVENTS_TTL_DAYS</code>, "
        f"<code>ANALYTICS_EXPORT_GRACE_DAYS</code></td></tr>"
        f"<tr><td>Retry tokens with stored responses (<code>economy_ops</code>)</td>"
        f"<td>{r['idempotency_tokens_days']} days</td>"
        f"<td><code>OPS_TTL_DAYS</code></td></tr>"
        f"<tr><td>Processed click-batch markers</td>"
        f"<td>{r['click_batches_hours']} hour</td><td>pruned on the next batch</td></tr>"
        f"<tr><td>Database backups (AES-256-GCM encrypted)</td>"
        f"<td>{r['backup_snapshots']} snapshots taken every "
        f"{int(r['backup_interval_hours'])}h — up to {r['backup_max_age_days']} days"
        f"</td><td><code>BACKUP_KEEP</code>, <code>BACKUP_INTERVAL_H</code></td></tr>"
        f"<tr><td>Administrative access log</td>"
        f"<td>{r['analytics_events_days']} days in the database; longer in the "
        f"application log</td><td><code>EVENTS_TTL_DAYS</code></td></tr>"
        "<tr><td>Profile, board, farm, orders, achievements</td>"
        "<td>while the account exists</td><td>erased on request</td></tr>"
        "<tr><td>Economy ledger and payments</td><td>indefinitely</td>"
        "<td>accounting and anti-fraud</td></tr></table>")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy():
    """Политика конфиденциальности. Перечень данных собран по схеме базы и по
    вызовам track(), сроки — по константам конфига."""
    return _page("Политика конфиденциальности — Cookie Clicker", f"""
<h1>Политика конфиденциальности</h1>
<p class="upd">Мини-приложение и бот Telegram. Дальше — только то, что
приложение действительно делает; список составлен по схеме базы данных и по
местам, где код пишет события.</p>

<h2>Кто обрабатывает данные</h2>
<p>Разработчик мини-приложения. Связаться можно через бота — командой
<code>/start</code> и ответом в чат поддержки. Мини-приложение работает внутри
Telegram и получает данные от Telegram; сам Telegram обрабатывает ваши данные
по своей политике, на которую мы не влияем.</p>

<h2>Какие данные собираются</h2>

<h3>1. Учётные данные Telegram</h3>
<p>При открытии приложения Telegram передаёт подписанный блок
<code>initData</code>. Из него сохраняются: <b>числовой идентификатор
пользователя</b>, <b>username</b>, <b>имя</b> (first name), <b>язык
интерфейса</b> (en/uk/ru) и <b>параметр запуска</b> — реферальный код
пригласившего (<code>ref_…</code>) или код рекламной ссылки
(<code>src_…</code>). Ни номера телефона, ни адреса электронной почты, ни
списка контактов, ни фотографии профиля приложение не получает и не хранит.</p>

<h3>2. Игровой прогресс</h3>
<p>Печеньки, опыт, уровень, энергия, всего кликов и слияний, лучший тир,
престиж, серии ежедневных наград, содержимое merge-доски, здания фермы,
купленные апгрейды и скины, коллекция, заказы пекарни, достижения, ежедневные
задания, боевой пропуск, дуэли, приглашённые игроки, отметки времени последнего
входа и последнего уведомления.</p>

<h3>3. Платежи</h3>
<p>Покупки за Telegram Stars: ключ товара, сумма в Stars, идентификатор платежа
Telegram, статус, что именно было выдано и когда, данные о возврате. <b>Реквизитов
карты, имени плательщика и адреса приложение не получает вовсе</b> — оплату
проводит Telegram, до приложения доходит только идентификатор платежа и сумма.</p>

<h3>4. Аналитика</h3>
<p>Своя, внутри той же базы; сторонних счётчиков, рекламных SDK и
трекинговых cookie нет. В строке события (<code>analytics_events</code>)
записываются: имя события, число и серверное время, идентификатор игрока,
версия игрового баланса, источник перехода (органика / рекламная ссылка /
приглашение) и его код, язык интерфейса, <b>код страны — тот, что подставляет
наш обратный прокси по IP-адресу; сам IP-адрес не сохраняется</b>, платформа
Telegram (<code>ios</code>, <code>android</code>, <code>tdesktop</code>,
<code>web</code>), ветка A/B-эксперимента и идентификатор операции в книге
движений валюты, если событию соответствует начисление. Полный список событий,
который умеет писать код:
<code>session</code>, <code>first_merge</code>, <code>first_building</code>,
<code>first_order</code>, <code>order_take</code>, <code>order_done</code>,
<code>order_abandon</code>, <code>order_stale_dropped</code>,
<code>trash_item</code>, <code>item_record</code>, <code>shiny_drop</code>,
<code>quest_reroll</code>, <code>duel_start</code>,
<code>tutorial_complete</code>, <code>account_deleted</code> и записи
административного доступа <code>admin_access:…</code>.</p>

<h3>5. Книга операций (<code>economy_ledger</code>)</h3>
<p>Каждое движение валюты записывается отдельной неизменяемой строкой: валюта,
сумма со знаком, причина, баланс после, время. Это бухгалтерия игры: по ней
разбираются жалобы «пропали печеньки» и выявляются злоупотребления.</p>

<h3>6. Техническое</h3>
<p>Записи журнала веб-сервера с идентификатором запроса и идентификатором
игрока, счётчики ограничения частоты запросов (живут минуты, в Redis) и
подписанный сервером токен сессии, который хранится только у клиента.</p>
<p>Единственный сторонний запрос интерфейса — библиотека мини-приложений
<code>telegram-web-app.js</code> с <code>telegram.org</code>: её требует сама
платформа, и Telegram при этом видит ваш IP-адрес и User-Agent, которые он и
так знает. Шрифты и весь остальной код раздаются с нашего сервера. Сторонних
счётчиков, рекламных SDK и трекинговых cookie в приложении нет.</p>

<h2>Зачем это нужно</h2>
<ul>
<li>Идентификатор Telegram — единственный способ узнать ваш сохранённый
прогресс: без него игра не работает.</li>
<li>Имя и username — показать вас в таблице лидеров и в списке приглашённых.</li>
<li>Язык — язык сообщений бота и интерфейса.</li>
<li>Платежи и книга операций — выдать купленное, провести возврат, вести учёт и
отличить ошибку от злоупотребления.</li>
<li>Аналитика — понять, где игроки застревают. Строится по когортам, не по
конкретному человеку.</li>
</ul>

<h2>Кому передаются</h2>
<p>Никому. Данные не продаются, не передаются рекламодателям и не отдаются
третьим лицам, кроме случаев, когда этого требует закон. Инфраструктура —
сервер приложения, база данных и хранилище зашифрованных резервных копий.</p>

<h2>Сроки хранения</h2>
{_retention_rows_ru()}

<h2>Ваши права</h2>
<h3>Забрать свои данные</h3>
<p><code>GET /api/legal/export</code> из открытого мини-приложения возвращает
JSON со всеми строками базы, которые вас касаются, включая книгу операций и
платежи.</p>
<h3>Удалить аккаунт</h3>
<p><code>POST /api/legal/delete</code> с телом <code>{{"confirm": "DELETE"}}</code>.
Действие необратимо и не требует подтверждения от нас.</p>
<p><b>Что удаляется полностью:</b> merge-доска, ферма, апгрейды, скины,
коллекция, заказы, достижения, ежедневные задания, награды боевого пропуска и
реферальные награды, бусты, права боевого пропуска, отметки пачек кликов,
токены повторной отправки, аналитические события, итоги прошлых сезонов и
незанятые заявки на дуэль.</p>
<p><b>Что обезличивается:</b> строка игрока в таблице <code>users</code>
остаётся, но username стирается, имя заменяется на <code>[deleted]</code>, а
прогресс обнуляется. Строка нужна ровно затем, чтобы числовой идентификатор в
книге операций и в платежах не указывал в пустоту.</p>
<p><b>Что остаётся и почему:</b></p>
<ul>
<li><b>Книга операций</b> — таблица защищена триггером базы данных как
append-only: её строку физически нельзя ни удалить, ни изменить, поэтому нельзя
и переписать в ней ссылку на игрока. После удаления там остаётся только
числовой идентификатор Telegram, за которым больше нет ни имени, ни
username.</li>
<li><b>Платежи</b> — бухгалтерская и налоговая необходимость, а также
единственное основание для возврата Stars: возврат оформляется по строке
платежа.</li>
<li><b>Входящие остатки</b> (<code>economy_opening</code>) — без них книга
операций перестаёт сходиться.</li>
<li><b>Активации промокодов</b> — защита от повторной активации того же
кода.</li>
<li><b>Записи о приглашениях и завершённых дуэлях</b> — в каждой такой строке
описаны ДВА игрока, и она одновременно является данными второго; кроме того,
это основной признак накрутки рефералов.</li>
<li><b>Долг после возврата Stars</b> — это учёт, а не прогресс, и он не
обнуляется удалением аккаунта.</li>
</ul>
<p>Если после удаления открыть приложение снова, тем же идентификатором
Telegram заводится пустой аккаунт; прежнее имя не восстанавливается.</p>

<h2>Резервные копии</h2>
<p>База копируется раз в {int(cfg.BACKUP_INTERVAL_H)} ч, копии шифруются
AES-256-GCM и хранятся в количестве {cfg.BACKUP_KEEP} штук. Удалить отдельного
игрока из уже сделанного зашифрованного снимка технически нельзя, не сломав
самой возможности восстановления. Поэтому запрос на удаление считается
исполненным в копиях по истечении срока их хранения — не позднее
{round(cfg.BACKUP_KEEP * cfg.BACKUP_INTERVAL_H / 24, 1)} суток с момента
удаления. Процедура записана в <code>deploy/RUNBOOK.md</code>. Если копию
разворачивают в этот промежуток, удаление повторяется на восстановленной
базе.</p>

<h2>Доступ администратора</h2>
<p>Административный доступ есть у одного идентификатора Telegram
(<code>ADMIN_ID</code>). Каждое обращение к админской ручке записывается в
журнал: кто, каким методом, к какой ручке и к какому игроку. Журнал доступен по
<code>GET /api/admin/access-log</code> и дублируется в логи приложения.</p>

<h2>Дети</h2>
<p>Приложение не предназначено для лиц младше 13 лет и намеренно не собирает
данные о возрасте.</p>

<h2>Изменения</h2>
<p>Существенные изменения этой страницы объявляются сообщением бота.</p>

<div class="lang">
<h2>English summary</h2>
<p>This is a Telegram Mini App. Everything below is what the code actually
does.</p>
<p><b>What we store.</b> From Telegram: your numeric user id, username, first
name, interface language (en/uk/ru) and the launch parameter (referral or
campaign code). From the game: cookies, XP, level, energy, click and merge
counters, prestige, streaks, merge board, farm buildings, upgrades, skins,
collection, bakery orders, achievements, daily quests, battle pass, duels,
invited players, last-seen timestamps. From payments: Telegram Stars purchases —
item key, Stars amount, Telegram charge id, status, what was granted, refund
data. <b>No card data, billing name, phone, e-mail, contacts or location</b> —
Telegram processes the payment and we never receive them. From analytics:
first-party event rows only (session, first_merge, first_building, first_order,
order_take/done/abandon, trash_item, item_record, shiny_drop, quest_reroll,
duel_start, tutorial_complete), each carrying the server timestamp, the game
config version, the acquisition source and its code, interface language, the
country code supplied by our reverse proxy (the IP address itself is not
stored), the Telegram platform, the A/B variant and the ledger operation id
when the event has a payout. There are no third-party analytics SDKs, ad
identifiers or tracking cookies. The only third-party request the UI makes is
<code>telegram-web-app.js</code> from <code>telegram.org</code>, required by the
platform itself; fonts and all other assets are served from our own server.</p>
<p><b>Retention.</b></p>
{_retention_rows_en()}
<p><b>Your rights.</b> <code>GET /api/legal/export</code> returns every row we
hold about you as JSON. <code>POST /api/legal/delete</code> with
<code>{{"confirm": "DELETE"}}</code> erases the account.</p>
<p><b>Deleted:</b> board, farm, upgrades, skins, collection, orders,
achievements, daily quests, battle-pass and referral claims, boosts,
entitlements, click-batch markers, retry tokens, analytics events, past season
results, unmatched duel queue entries.</p>
<p><b>Anonymised:</b> the <code>users</code> row is kept as a tombstone —
username cleared, name replaced with <code>[deleted]</code>, progress zeroed —
so that the numeric id referenced by the ledger and by payments no longer points
at an identity.</p>
<p><b>Kept, and why:</b> the economy ledger (append-only, enforced by a database
trigger — rows can be neither deleted nor rewritten; required for accounting and
dispute resolution), payment records (accounting, tax, and the only basis for a
Stars refund), opening balances (the ledger does not reconcile without them),
promo redemptions (prevents redeeming the same code twice), referral and
finished duel rows (each describes two players and is the primary signal of
referral farming), and any outstanding refund debt.</p>
<p><b>Backups.</b> Snapshots are AES-256-GCM encrypted and kept for
{cfg.BACKUP_KEEP} rotations, i.e. up to
{round(cfg.BACKUP_KEEP * cfg.BACKUP_INTERVAL_H / 24, 1)} days. Individual rows
cannot be removed from an encrypted snapshot without destroying its
restorability, so a deletion request is satisfied for backups when the retention
window expires; if a snapshot is restored within that window, the deletion is
re-applied. See <code>deploy/RUNBOOK.md</code>.</p>
<p><b>Administrative access</b> is limited to a single Telegram id and every
admin API call is logged (who, method, endpoint, target player).</p>
</div>
""")


@router.get("/terms", response_class=HTMLResponse)
async def terms():
    """Условия использования. Всё, что здесь сказано про игровые правила
    (возвраты, престиж, сезоны, античит), соответствует поведению кода."""
    return _page("Условия использования — Cookie Clicker", f"""
<h1>Условия использования</h1>
<p class="upd">Открывая мини-приложение, вы соглашаетесь с этими условиями.</p>

<h2>1. Что это</h2>
<p>Бесплатная игра-кликер внутри Telegram. Для игры нужен аккаунт Telegram;
отдельной регистрации нет. Вы также обязаны соблюдать условия самого
Telegram.</p>

<h2>2. Аккаунт</h2>
<p>Аккаунт привязан к идентификатору Telegram. Один игрок — один аккаунт.
Передача, продажа и покупка аккаунтов запрещены.</p>

<h2>3. Игровая валюта</h2>
<p>Печеньки, опыт, энергия, очки престижа и уровни боевого пропуска —
внутриигровые значения. Они не являются деньгами, ценными бумагами или иным
имуществом, не подлежат обмену на деньги и не имеют ценности вне игры.</p>

<h2>4. Покупки за Telegram Stars</h2>
<p>Платные товары продаются за Telegram Stars. Платёж проводит Telegram по
своим правилам; приложение получает только идентификатор платежа и сумму.
Покупка считается исполненной в момент выдачи товара; если выдача не прошла,
она довыполняется автоматически при следующем входе.</p>
<p>Возврат оформляется через Telegram. При возврате выданное снимается: если на
балансе не хватает, разница остаётся долгом и гасится будущим доходом. Возврат
после того, как выданное потрачено, — не способ получить товар бесплатно.</p>

<h2>5. Честная игра</h2>
<p>Запрещены: автокликеры и любая автоматизация, изменение клиента и подделка
запросов, эксплуатация ошибок вместо сообщения о них, накрутка рефералов
(множественные аккаунты, приглашения самого себя), покупка и продажа игровых
ценностей за реальные деньги вне Telegram Stars.</p>
<p>Сервер ограничивает скорость кликов и частоту запросов. Прогресс, полученный
в обход правил, может быть снят, а аккаунт — заблокирован.</p>

<h2>6. Сезоны и сбросы</h2>
<p>Сезонные значения (боевой пропуск, сезонный заработок, таблица лидеров)
обнуляются в конце сезона по расписанию игры. Престиж обнуляет часть прогресса
добровольно и в обмен на постоянный множитель. Это часть правил, а не потеря
данных.</p>

<h2>7. Изменения игры</h2>
<p>Баланс, цены и состав товаров могут меняться. Уже выданное за Stars при этом
не отбирается.</p>

<h2>8. Прекращение</h2>
<p>Вы можете удалить аккаунт в любой момент — см.
<a href="/privacy">политику конфиденциальности</a>. Мы можем ограничить доступ
при нарушении раздела 5; на возврат средств за уже выданные товары это права не
даёт.</p>

<h2>9. Без гарантий</h2>
<p>Игра предоставляется «как есть». Мы стараемся не терять прогресс и делаем
резервные копии раз в {int(cfg.BACKUP_INTERVAL_H)} ч, но не гарантируем
непрерывной работы и не отвечаем за потери, вызванные сбоями Telegram,
провайдера или оборудования.</p>

<h2>10. Данные</h2>
<p>Что собирается и сколько хранится — на странице
<a href="/privacy">политики конфиденциальности</a>.</p>

<div class="lang">
<h2>English summary</h2>
<p>A free clicker game inside Telegram; a Telegram account is required and
Telegram's own terms apply. One player, one account; accounts may not be sold or
transferred. In-game cookies, XP, energy, prestige points and battle-pass levels
are not money or property, have no value outside the game and cannot be cashed
out. Paid items are sold for Telegram Stars: Telegram processes the payment and
we receive only the charge id and the amount; a failed grant is retried on your
next launch. Refunds go through Telegram and revoke what was granted — if your
balance is short, the remainder is recorded as debt. Autoclickers, modified
clients, forged requests, bug exploitation and referral farming are prohibited,
and progress obtained that way may be removed. Seasonal values reset on
schedule; prestige resets progress by your own choice in exchange for a
permanent multiplier. Balance, prices and the item list may change, but items
already granted for Stars are not taken back. You may delete your account at any
time — see the <a href="/privacy">privacy policy</a>. The game is provided "as
is": backups are taken every {int(cfg.BACKUP_INTERVAL_H)} hours, but continuous
availability is not guaranteed.</p>
</div>
""")
