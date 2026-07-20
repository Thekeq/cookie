"""Админка: промокоды, source-ссылки, статистика. Только для ADMIN_ID."""
import os
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server.auth import tg_admin
from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db

router = APIRouter(prefix="/api/admin", dependencies=[Depends(tg_admin)])

BOT_USERNAME = os.getenv("BOT_USERNAME", "")


# ---------- промокоды ----------

class PromoCreate(BaseModel):
    code: str
    reward_cookies: float = 0
    reward_energy: float = 0
    max_uses: int = 0  # 0 = безлимит


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
    code: str
    title: str = ""


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
    text: str
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
        "SELECT event, COUNT(*) c FROM events WHERE created_at > ? "
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
