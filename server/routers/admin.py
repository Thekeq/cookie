"""Админка: промокоды, source-ссылки, статистика, выгрузка аналитики и
инструмент поддержки. Только для ADMIN_ID.

Своего журналирования здесь нет и заводить его нельзя: `legal.admin_audit`
висит на этом роутере целиком (main.py), поэтому КАЖДАЯ ручка отсюда уже
записана — кто, каким методом, к какой ручке и к какому игроку. Второй механизм
дал бы две несогласованные версии одного журнала.
"""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server.auth import tg_admin
from server import game_config as cfg
from server import support
from server import game_logic as gl
from server.game_logic import db
from server.settings import BOT_USERNAME

router = APIRouter(prefix="/api/admin", dependencies=[Depends(tg_admin)])


# ---------- промокоды ----------

class PromoCreate(BaseModel):
    # промокод короче 10 символов перебирается словарём за минуты
    code: str = Field(min_length=10, max_length=64)
    reward_cookies: float = Field(default=0, ge=0)
    reward_energy: float = Field(default=0, ge=0)
    max_uses: int = Field(default=0, ge=0)  # 0 = безлимит


@router.get("/promo")
async def list_promo():
    return {"promo_codes": db.q("SELECT * FROM promo_codes ORDER BY created_at DESC")}


@router.post("/promo")
async def create_promo(body: PromoCreate):
    code = body.code.strip().upper()
    if not code:
        raise HTTPException(400, "Пустой код")
    if db.q1("SELECT id FROM promo_codes WHERE code = ?", (code,)):
        raise HTTPException(400, "Такой код уже есть")
    db.exec("INSERT INTO promo_codes (code, reward_cookies, reward_energy, max_uses, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (code, body.reward_cookies, body.reward_energy, body.max_uses, time.time()))
    return {"ok": True, "code": code}


class PromoToggle(BaseModel):
    code: str
    active: bool


@router.post("/promo/toggle")
async def toggle_promo(body: PromoToggle):
    db.exec("UPDATE promo_codes SET active = ? WHERE code = ?",
            (1 if body.active else 0, body.code.strip().upper()))
    return {"ok": True}


# ---------- source-ссылки ----------

class SourceCreate(BaseModel):
    code: str = Field(max_length=64)
    title: str = Field(default="", max_length=128)


@router.get("/sources")
async def list_sources():
    rows = db.q("SELECT * FROM sources ORDER BY created_at DESC")
    for r in rows:
        r["link"] = f"https://t.me/{BOT_USERNAME}?startapp=src_{r['code']}"
    return {"sources": rows}


@router.post("/sources")
async def create_source(body: SourceCreate):
    code = body.code.strip()
    if not code or not code.replace("_", "").isalnum():
        raise HTTPException(400, "Код: латиница/цифры/подчёркивания")
    if db.q1("SELECT id FROM sources WHERE code = ?", (code,)):
        raise HTTPException(400, "Такой код уже есть")
    db.exec("INSERT INTO sources (code, title, created_at) VALUES (?, ?, ?)",
            (code, body.title, time.time()))
    return {"ok": True, "link": f"https://t.me/{BOT_USERNAME}?startapp=src_{code}"}


# ---------- рассылка (фоном, чтобы не держать HTTP-запрос) ----------

class BroadcastIn(BaseModel):
    text: str = Field(max_length=4000)   # лимит сообщения Telegram
    test: bool = False  # true = отправить только себе (превью)


# прогресс текущей рассылки; одна за раз
_bc = {"running": False, "sent": 0, "blocked": 0, "failed": 0, "total": 0,
       "started_at": 0.0, "finished_at": 0.0}


async def _run_broadcast(text: str):
    import asyncio
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
    from bot.loader import bot

    user_ids = [r["user_id"] for r in
                db.q("SELECT user_id FROM users WHERE notify_blocked = 0")]
    _bc.update(running=True, sent=0, blocked=0, failed=0, total=len(user_ids),
               started_at=time.time(), finished_at=0.0)
    try:
        for uid in user_ids:
            try:
                await bot.send_message(uid, text)
                _bc["sent"] += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.send_message(uid, text)
                    _bc["sent"] += 1
                except Exception:
                    _bc["failed"] += 1
            except TelegramForbiddenError:
                db.exec("UPDATE users SET notify_blocked = 1 WHERE user_id = ?", (uid,))
                _bc["blocked"] += 1
            except Exception:
                _bc["failed"] += 1
            await asyncio.sleep(0.04)  # ≈25/сек, безопасно для лимитов Telegram
    finally:
        _bc.update(running=False, finished_at=time.time())


@router.post("/broadcast")
async def broadcast(body: BroadcastIn, tg: dict = Depends(tg_admin)):
    """Превью шлём сразу; полную рассылку запускаем фоновой задачей и
    отдаём прогресс через /broadcast/status."""
    import asyncio
    from bot.loader import bot

    text = body.text.strip()
    if not text:
        raise HTTPException(400, "Пустое сообщение")

    if body.test:
        await bot.send_message(tg["id"], text)
        return {"sent": 1, "blocked": 0, "failed": 0, "test": True}

    if _bc["running"]:
        raise HTTPException(400, "Рассылка уже идёт")
    asyncio.create_task(_run_broadcast(text))
    total = db.q1("SELECT COUNT(*) c FROM users WHERE notify_blocked = 0")["c"]
    return {"started": True, "total": total}


@router.get("/broadcast/status")
async def broadcast_status():
    return dict(_bc)


# ---------- проблемные платежи ----------

@router.get("/payments")
async def problem_payments():
    """Платежи, требующие ручного разбора: деньги получены, товар не выдан.

    'unmatched' — платёж не сошёлся с конфигом (переименовали товар, поменяли
    цену между pre_checkout и оплатой); 'void' — выдать нечего (товар исчез
    или уже куплен навсегда); 'paid' старше часа — выдача не прошла.

    Возвраты идут отдельным списком с prior_status: по нему видно, сняли ли с
    игрока выданное или снимать было нечего, — сам статус 'refunded' об этом
    не говорит, а разбирать спор без этого нечем."""
    stuck = db.q(
        "SELECT p.id, p.user_id, p.item_key, p.stars_amount, p.tg_payment_id, "
        "       p.status, p.created_at, u.username, u.first_name "
        "FROM purchases p LEFT JOIN users u ON u.user_id = p.user_id "
        "WHERE p.status IN ('unmatched', 'void') "
        "   OR (p.status = 'paid' AND p.created_at < ?) "
        "ORDER BY p.created_at DESC LIMIT 100", (time.time() - 3600,))
    return {"problems": stuck, "count": len(stuck),
            "refunded": db.q1("SELECT COUNT(*) c FROM purchases "
                              "WHERE status = 'refunded'")["c"],
            "refunds": db.q(
                "SELECT p.id, p.user_id, p.item_key, p.stars_amount, "
                "       p.tg_payment_id, p.prior_status, p.refunded_at, "
                "       p.refund_stars, p.granted_payload, u.username "
                "FROM purchases p LEFT JOIN users u ON u.user_id = p.user_id "
                "WHERE p.status = 'refunded' "
                "ORDER BY p.refunded_at DESC LIMIT 100"),
            "debtors": db.q(
                "SELECT user_id, username, cookie_debt FROM users "
                "WHERE cookie_debt > 0 ORDER BY cookie_debt DESC LIMIT 100")}


# ---------- статистика ----------

@router.get("/stats")
async def stats():
    now = time.time()
    day_ago = now - 86400
    week_ago = now - 7 * 86400
    total = db.q1("SELECT COUNT(*) c FROM users")["c"]
    new_day = db.q1("SELECT COUNT(*) c FROM users WHERE created_at > ?", (day_ago,))["c"]
    new_week = db.q1("SELECT COUNT(*) c FROM users WHERE created_at > ?", (week_ago,))["c"]
    active_day = db.q1("SELECT COUNT(*) c FROM users WHERE energy_updated_at > ?", (day_ago,))["c"]
    refs = db.q1("SELECT COUNT(*) c FROM referrals")["c"]
    paid = db.q1("SELECT COUNT(*) c, COALESCE(SUM(stars_amount),0) s FROM purchases "
                 "WHERE status IN ('paid', 'fulfilled')")
    by_source = db.q(
        "SELECT COALESCE(source_code, 'organic') src, COUNT(*) c FROM users "
        "GROUP BY source_code ORDER BY c DESC")

    # ретеншен DN: когорта = регистрации N+1..N дней назад; вернулся, если
    # был онлайн спустя >= N дней после регистрации
    def retention(n: int) -> dict:
        cohort = db.q(
            "SELECT created_at, last_seen_at FROM users "
            "WHERE created_at BETWEEN ? AND ?",
            (now - (n + 1) * 86400, now - n * 86400))
        came_back = sum(1 for u in cohort
                        if (u["last_seen_at"] or 0) >= u["created_at"] + n * 86400)
        return {"cohort": len(cohort), "returned": came_back}

    # воронка активации: где теряем игроков в первом сеансе
    funnel = {
        "registered": total,
        "clicked_10": db.q1("SELECT COUNT(*) c FROM users WHERE total_clicks >= 10")["c"],
        "merged_1": db.q1("SELECT COUNT(*) c FROM users WHERE total_merges >= 1")["c"],
        "built_1": db.q1("SELECT COUNT(*) c FROM users WHERE EXISTS "
                         "(SELECT 1 FROM farm WHERE farm.user_id = users.user_id)")["c"],
        "order_1": db.q1("SELECT COUNT(*) c FROM users WHERE orders_completed >= 1")["c"],
        "tutorial_complete": db.q1("SELECT COUNT(*) c FROM users WHERE tutorial_done = 1")["c"],
    }
    bp_done = db.q1("SELECT COUNT(*) c FROM users WHERE bp_xp >= ?",
                    (cfg.bp_total_xp(cfg.BP_MAX_LEVEL),))["c"]
    events_7d = db.q(
        "SELECT event, COUNT(*) c FROM analytics_events WHERE created_at > ? "
        "GROUP BY event ORDER BY c DESC LIMIT 20", (week_ago,))
    return {
        "users_total": total,
        "users_new_24h": new_day,
        "users_new_7d": new_week,
        "active_24h": active_day,
        "referrals_total": refs,
        "purchases_count": paid["c"],
        "stars_earned": paid["s"],
        "by_source": by_source,
        "retention": {"d1": retention(1), "d3": retention(3), "d7": retention(7)},
        "funnel": funnel,
        "bp_completed": bp_done,
        "events_7d": events_7d,
    }


# ---------- аналитика: выгрузка сырых событий ----------
# Зачем ручкой, а не «сходить psql'ом». Выгрузка обязана быть ПОДТВЕРЖДАЕМОЙ:
# TTL сносит строку только после того, как она уехала (analytics_events.
# exported_at), и подтверждать это должен тот же путь, которым её забрали.
# Ручной SELECT такого следа не оставляет, и первый же оборванный на середине
# дамп означал бы тихую дыру в данных.
#
# Отсюда и две ручки вместо одной. Забор ничего не помечает; пометку ставит
# отдельный ack ПОСЛЕ того, как приёмник записал данные у себя. Оборвалась
# сеть на полпути — следующий забор отдаст те же строки заново, а дубликаты
# приёмник задавит по event_id. Обратный порядок (пометить при отдаче) терял
# бы события на каждом разрыве соединения, причём молча.

@router.get("/analytics/export")
async def analytics_export(after_id: int = 0, limit: int = 1000,
                           pending_only: bool = True):
    """Партия сырых событий по возрастанию id. Ничего не помечает.

    `pending_only=false` — повторная выгрузка уже отданного (перезаливка
    хранилища); курсор при этом всё тот же `after_id`."""
    return gl.export_batch(after_id, limit, pending_only)


class ExportAck(BaseModel):
    # Подтверждать можно только то, что реально забрали, поэтому граница — id,
    # а не «всё». ge=1: нулевого id не существует, и «ack 0» скорее означает
    # пустую переменную в скрипте, чем осознанное действие
    through_id: int = Field(ge=1)


@router.post("/analytics/export/ack")
async def analytics_export_ack(body: ExportAck):
    """Подтвердить, что события до `through_id` включительно легли в хранилище."""
    return gl.mark_exported(body.through_id)


# ---------- поддержка и anti-fraud (пункт 31) ----------
# Логика — в server/support.py, здесь только HTTP. Разделение не ради красоты:
# массовую компенсацию зовут и из ручки, и из процесса (`python -c` в рантбуке,
# когда админка недоступна), а функция, живущая внутри эндпоинта, из процесса
# не зовётся.
#
# Журнал доступа отдельно тут не нужен: роутер целиком под legal.admin_audit, и
# `user_id` из пути ручка журнала подхватывает сама (legal._access_target) —
# «кто смотрел карточку какого игрока» пишется без единой строки здесь.


@router.get("/player/{user_id}")
async def player_card(user_id: int, ledger_limit: int = 100):
    """Карточка игрока для разбора обращения: цепочка платежей, книга, сверка,
    флаги, последние события аналитики."""
    try:
        return support.player_card(user_id, ledger_limit)
    except ValueError as e:
        raise HTTPException(404, str(e))


@router.get("/player/{user_id}/payments")
async def player_payments(user_id: int, limit: int = 50):
    """Только цепочка `invoice -> payment -> grant -> refund`."""
    return {"payments": support.payment_chain(user_id, limit)}


class CompensateIn(BaseModel):
    user_id: int
    currency: str = Field(default="cookies", max_length=32)
    amount: float = Field(gt=0)
    # причина из закрытого списка support.COMPENSATION_REASONS: она уезжает в
    # книгу, и свободный текст сделал бы инцидент неподсчитываемым
    reason: str = Field(max_length=64)
    # метка инцидента — она же ключ идемпотентности: повтор с той же меткой
    # ничего не начислит второй раз
    incident: str = Field(max_length=64)
    note: str = Field(default="", max_length=200)


@router.get("/compensate/reasons")
async def compensate_reasons():
    """Чем вообще можно компенсировать. Отдаём с сервера, чтобы список в
    интерфейсе не разъезжался с тем, что примет ручка."""
    return {"reasons": support.COMPENSATION_REASONS,
            "currencies": list(support.COMPENSATION_CURRENCIES),
            "max_amount": support.COMPENSATION_MAX,
            "cohort_limit": support.COHORT_LIMIT}


@router.post("/compensate")
async def compensate(body: CompensateIn):
    """Безопасная компенсация одному игроку — только через книгу операций."""
    try:
        return support.compensate(body.user_id, body.currency, body.amount,
                                  body.reason, body.incident, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


class CohortIn(BaseModel):
    # либо явный список, либо селектор по данным — см. support.cohort
    kind: str = Field(default="", max_length=32)
    value: str = Field(default="", max_length=128)
    since: float = 0
    until: float = 0


@router.post("/compensate/cohort")
async def compensate_cohort(body: CohortIn):
    """Кого задел инцидент. Ничего не начисляет — только список."""
    try:
        ids = support.cohort(body.kind, body.value, body.since, body.until)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"user_ids": ids, "count": len(ids)}


class BulkCompensateIn(CompensateIn):
    # user_id из родителя не используется; когорта задаётся одним из двух
    # способов, и оба заданными быть не могут
    user_id: int = 0
    user_ids: list[int] = Field(default_factory=list)
    cohort: CohortIn | None = None
    # По умолчанию НИЧЕГО не выдаётся, только считается. Массовая раздача —
    # единственное необратимое действие в админке: книга append-only, отнять
    # выданное нечем
    dry_run: bool = True


@router.post("/compensate/bulk")
async def compensate_bulk(body: BulkCompensateIn):
    """Массовая компенсация по когорте инцидента.

    Идемпотентна по (инцидент, валюта, игрок): оборвавшуюся партию доигрывает
    повторный запуск, уже заплатившим второй раз не достаётся."""
    try:
        ids = list(body.user_ids)
        if body.cohort is not None:
            c = body.cohort
            ids += support.cohort(c.kind, c.value, c.since, c.until)
        if not ids:
            raise HTTPException(400, "err_empty_cohort")
        return support.compensate_bulk(ids, body.currency, body.amount,
                                       body.reason, body.incident, body.note,
                                       dry_run=body.dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/fraud")
async def fraud(limit: int = 50):
    """Подозрительные аккаунты по всей базе. Флаг — повод посмотреть, а не
    приговор: автоматических блокировок здесь нет намеренно."""
    return support.suspicious(limit)
