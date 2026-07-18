"""Мета: авторизация/регистрация, рефералка, промокоды, батл-пасс, магазин."""
import json
import os
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import game_config as cfg
from server import game_logic as gl
from server.auth import tg_user
from server.game_logic import db

router = APIRouter(prefix="/api")


# ---------- вход / регистрация ----------

@router.post("/auth")
async def auth(tg: dict = Depends(tg_user)):
    """Первый запрос при открытии Mini App: создаёт юзера, фиксирует ref_/src_."""
    gl.finalize_seasons()
    user = db.get_user(tg["id"])
    just_registered = False
    if not user:
        referrer_id, source_code = None, None
        sp = tg.get("start_param", "") or ""
        if sp.startswith("ref_"):
            try:
                rid = int(sp[4:])
                if rid != tg["id"] and db.get_user(rid):
                    referrer_id = rid
            except ValueError:
                pass
        elif sp.startswith("src_"):
            code = sp[4:]
            if db.q1("SELECT id FROM sources WHERE code = ?", (code,)):
                source_code = code
                db.exec("UPDATE sources SET registrations = registrations + 1 WHERE code = ?", (code,))

        user = db.create_user(tg["id"], tg["username"], tg["first_name"],
                              referrer_id=referrer_id, source_code=source_code)
        db.update_user(tg["id"], season_id=gl.current_season())
        just_registered = True

        # взаимная награда за реферала — сразу обоим
        if referrer_id:
            db.exec("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) "
                    "VALUES (?, ?, ?)", (referrer_id, tg["id"], time.time()))
            gl.add_cookies(referrer_id, cfg.REF_REWARD_REFERRER, count_earned=False)
            gl.add_cookies(tg["id"], cfg.REF_REWARD_REFERRED, count_earned=False)

    state = gl.full_state(tg["id"])
    state["just_registered"] = just_registered
    return state


# ---------- рефералка ----------

@router.get("/referrals")
async def referrals(tg: dict = Depends(tg_user)):
    rows = db.q(
        "SELECT r.referred_id, u.username, u.first_name, u.level, r.created_at "
        "FROM referrals r LEFT JOIN users u ON u.user_id = r.referred_id "
        "WHERE r.referrer_id = ? ORDER BY r.created_at DESC", (tg["id"],))
    return {
        "referrals": rows,
        "count": len(rows),
        "reward_referrer": cfg.REF_REWARD_REFERRER,
        "reward_referred": cfg.REF_REWARD_REFERRED,
        "milestones": gl.ref_milestones_state(tg["id"]),
    }


class MilestoneIn(BaseModel):
    key: str


@router.post("/referrals/milestone")
async def claim_milestone(body: MilestoneIn, tg: dict = Depends(tg_user)):
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "No user")
    try:
        r = gl.claim_ref_milestone(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["milestones"] = gl.ref_milestones_state(tg["id"])
    return r


# ---------- промокоды ----------

class PromoIn(BaseModel):
    code: str


@router.post("/promo/redeem")
async def redeem_promo(body: PromoIn, tg: dict = Depends(tg_user)):
    code = body.code.strip().upper()
    promo = db.q1("SELECT * FROM promo_codes WHERE code = ? AND active = 1", (code,))
    if not promo:
        raise HTTPException(400, "Промокод не найден")
    if promo["max_uses"] and promo["uses"] >= promo["max_uses"]:
        raise HTTPException(400, "Промокод исчерпан")
    if db.q1("SELECT id FROM promo_redemptions WHERE code = ? AND user_id = ?", (code, tg["id"])):
        raise HTTPException(400, "Ты уже активировал этот промокод")

    db.exec("INSERT INTO promo_redemptions (code, user_id, redeemed_at) VALUES (?, ?, ?)",
            (code, tg["id"], time.time()))
    db.exec("UPDATE promo_codes SET uses = uses + 1 WHERE code = ?", (code,))
    if promo["reward_cookies"]:
        gl.add_cookies(tg["id"], promo["reward_cookies"], count_earned=False)
    if promo["reward_energy"]:
        user = gl.refresh_energy(db.get_user(tg["id"]))
        db.update_user(tg["id"], energy=user["energy"] + promo["reward_energy"])
    return {"reward_cookies": promo["reward_cookies"], "reward_energy": promo["reward_energy"],
            "cookies": db.get_user(tg["id"])["cookies"]}


# ---------- батл-пасс ----------

@router.get("/battlepass")
async def battlepass(tg: dict = Depends(tg_user)):
    gl.finalize_seasons()
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "No user")
    bp_level = cfg.bp_level_for_xp(user["bp_xp"])
    claimed_free = json.loads(user["bp_claimed_free"] or "[]")
    claimed_prem = json.loads(user["bp_claimed_premium"] or "[]")
    levels = []
    for lvl in range(1, cfg.BP_MAX_LEVEL + 1):
        levels.append({
            "level": lvl,
            "free": cfg.bp_reward(lvl, False),
            "premium": cfg.bp_reward(lvl, True),
            "reached": bp_level >= lvl,
            "free_claimed": lvl in claimed_free,
            "premium_claimed": lvl in claimed_prem,
        })
    next_lvl = min(cfg.BP_MAX_LEVEL, bp_level + 1)
    return {
        "season": gl.current_season() + 1,  # людям показываем с 1, не с 0
        "season_ends_at": gl.season_end_ts(gl.current_season()),
        "bp_xp": user["bp_xp"],
        "bp_level": bp_level,
        # прогресс внутри текущего уровня — фронт рисует бар по этим двум числам
        "xp_in_level": user["bp_xp"] - cfg.bp_total_xp(bp_level),
        "xp_per_level": cfg.bp_xp_for_level(next_lvl),
        "premium": bool(user["bp_premium"]),
        "premium_price_stars": cfg.BP_PREMIUM_STARS,
        "levels": levels,
    }


class BPClaim(BaseModel):
    level: int
    track: str  # "free" | "premium"


@router.post("/battlepass/claim")
async def bp_claim(body: BPClaim, tg: dict = Depends(tg_user)):
    user = db.get_user(tg["id"])
    bp_level = cfg.bp_level_for_xp(user["bp_xp"])
    if body.level < 1 or body.level > bp_level:
        raise HTTPException(400, "Уровень ещё не достигнут")
    if body.track == "premium" and not user["bp_premium"]:
        raise HTTPException(400, "Нужен Premium Пасс")
    col = "bp_claimed_free" if body.track == "free" else "bp_claimed_premium"
    claimed = json.loads(user[col] or "[]")
    if body.level in claimed:
        raise HTTPException(400, "Уже получено")
    claimed.append(body.level)
    db.update_user(tg["id"], **{col: json.dumps(claimed)})

    reward = cfg.bp_reward(body.level, body.track == "premium")
    if reward["cookies"]:
        gl.add_cookies(tg["id"], reward["cookies"], count_earned=False)
    if reward.get("energy"):
        fresh = gl.refresh_energy(db.get_user(tg["id"]))
        db.update_user(tg["id"], energy=fresh["energy"] + reward["energy"])
    return {"reward": reward, "cookies": db.get_user(tg["id"])["cookies"]}


# ---------- магазин (Stars) ----------

@router.get("/shop")
async def shop(tg: dict = Depends(tg_user)):
    """Для пачек с income_hours считаем персональную сумму — покупатель видит,
    сколько конкретно печенек получит именно он."""
    income = gl.hourly_income(tg["id"])
    items = []
    for k, (title, d, s, effect) in cfg.SHOP_ITEMS.items():
        item = {"key": k, "title": title, "desc": d, "stars": s}
        if effect.get("type") == "cookies" and "income_hours" in effect:
            item["amount"] = max(effect["min_amount"],
                                 income * effect["income_hours"])
        items.append(item)
    return {"items": items}


class BuyIn(BaseModel):
    item_key: str


@router.post("/shop/invoice")
async def create_invoice(body: BuyIn, tg: dict = Depends(tg_user)):
    """Создаёт invoice-ссылку на оплату Stars через бота."""
    if body.item_key not in cfg.SHOP_ITEMS:
        raise HTTPException(400, "Нет такого товара")
    title, desc, stars, _effect = cfg.SHOP_ITEMS[body.item_key]

    from bot.loader import bot  # локальный импорт: бот и сервер живут в одном процессе
    from aiogram.types import LabeledPrice
    link = await bot.create_invoice_link(
        title=title,
        description=desc,
        payload=f"{tg['id']}:{body.item_key}",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=stars)],
    )
    return {"invoice_link": link}


# ---------- лидерборд ----------

@router.get("/leaderboard")
async def leaderboard(tg: dict = Depends(tg_user)):
    """Сезонный топ по season_earned; сезон длится SEASON_LENGTH_DAYS, топ-10 получают награды."""
    gl.finalize_seasons()
    season = gl.current_season()
    top = db.q(
        "SELECT user_id, username, first_name, level, season_earned "
        "FROM users WHERE season_id = ? ORDER BY season_earned DESC LIMIT 100", (season,))
    for i, row in enumerate(top):
        row["rank"] = i + 1
        row["name"] = row.pop("first_name") or row.pop("username") or "Player"
        row.pop("username", None)
        row["is_me"] = row["user_id"] == tg["id"]
        row["prize"] = cfg.season_reward(i + 1, row["season_earned"])

    me = db.get_user(tg["id"])
    my_rank = None
    if me:
        my_rank = db.q1(
            "SELECT COUNT(*) c FROM users WHERE season_id = ? AND season_earned > ?",
            (season, me["season_earned"]))["c"] + 1
    return {
        "top": top,
        "me": {"rank": my_rank, "season_earned": me["season_earned"] if me else 0},
        "players_total": db.q1("SELECT COUNT(*) c FROM users WHERE season_id = ?",
                               (season,))["c"],
        "season": season + 1,
        "season_ends_at": gl.season_end_ts(season),
        "last_result": gl.my_last_season_result(tg["id"]),
    }


# ---------- подписка на канал ----------

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").lstrip("@")


@router.get("/channel")
async def channel(tg: dict = Depends(tg_user)):
    user = db.get_user(tg["id"])
    return {
        "channel": CHANNEL_USERNAME,
        "reward": cfg.CHANNEL_REWARD,
        "claimed": bool(user and user["channel_claimed"]),
    }


@router.post("/channel/claim")
async def channel_claim(tg: dict = Depends(tg_user)):
    if not CHANNEL_USERNAME:
        raise HTTPException(400, "Канал не настроен")
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "No user")
    if user["channel_claimed"]:
        raise HTTPException(400, "Уже получено")

    from bot.loader import bot
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", tg["id"])
    except Exception:
        raise HTTPException(400, "Не удалось проверить подписку")
    if member.status in ("left", "kicked"):
        raise HTTPException(400, "Сначала подпишись на канал")

    db.update_user(tg["id"], channel_claimed=1)
    gl.add_cookies(tg["id"], cfg.CHANNEL_REWARD, count_earned=False)
    return {"reward": cfg.CHANNEL_REWARD, "cookies": db.get_user(tg["id"])["cookies"]}
