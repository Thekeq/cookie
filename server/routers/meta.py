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
            # награды масштабируются доходом ПРИГЛАСИВШЕГО: для ветерана
            # 1000 печенек — доли секунды, приглашать было незачем
            gl.add_cookies(referrer_id, cfg.scaled_reward(
                cfg.REF_REWARD_REFERRER, "referrer",
                gl.hourly_income(referrer_id)), count_earned=False)
            gl.add_cookies(tg["id"], cfg.REF_REWARD_REFERRED, count_earned=False)

    # синкаем язык Mini App в профиль — бот использует его для /start и пушей
    if user.get("lang") != tg["lang"]:
        db.update_user(tg["id"], lang=tg["lang"])

    # оффлайн-доход начисляем сразу при входе, а не при первом /api/state —
    # иначе шапка первые полминуты показывала «вчерашний» баланс
    passive = gl.collect_passive(db.get_user(tg["id"]))
    farm_income = gl.collect_farm(db.get_user(tg["id"]))
    # довыдаём Stars-покупки, зависшие в 'paid' после сбоя между оплатой и выдачей
    gl.fulfill_pending(tg["id"])
    gl.track(tg["id"], "session")  # аналитика сессий

    state = gl.full_state(tg["id"])
    state["just_registered"] = just_registered
    state["passive_collected"] = passive + farm_income
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
        raise HTTPException(404, "err_no_user")
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
    """Активация промокода.

    Все неудачи отдают ОДИН код ошибки: раньше «кода нет», «код исчерпан» и
    «уже активирован» различались, и это работало оракулом — перебор словаря
    сразу показывал, какие коды существуют.
    """
    code = body.code.strip().upper()
    gl.check_rate_limit(tg["id"], "promo", cfg.PROMO_ATTEMPTS_PER_HOUR, 3600)
    promo = db.q1("SELECT * FROM promo_codes WHERE code = ? AND active = 1", (code,))
    if not promo:
        raise HTTPException(400, "err_promo_invalid")

    with db.tx():  # отметка активации + счётчик + награды — одним куском
        # оба UPDATE/INSERT условные: проверки «уже активировал» и «лимит
        # исчерпан» вне транзакции пробиваются параллельными запросами
        db.exec("INSERT OR IGNORE INTO promo_redemptions (code, user_id, redeemed_at) "
                "VALUES (?, ?, ?)", (code, tg["id"], time.time()))
        if db.cursor.rowcount == 0:
            raise HTTPException(400, "err_promo_invalid")
        db.exec("UPDATE promo_codes SET uses = uses + 1 "
                "WHERE code = ? AND (max_uses = 0 OR uses < max_uses)", (code,))
        if db.cursor.rowcount == 0:
            raise HTTPException(400, "err_promo_invalid")
        if promo["reward_cookies"]:
            gl.add_cookies(tg["id"], promo["reward_cookies"], count_earned=False)
        if promo["reward_energy"]:
            user = gl.refresh_energy(db.get_user(tg["id"]))
            # клэмп по потолку: излишек всё равно срезался бы первым
            # refresh_energy, и игрок молча терял выданное
            db.update_user(tg["id"], energy=min(gl.energy_cap(user),
                                                user["energy"] + promo["reward_energy"]))
    return {"reward_cookies": promo["reward_cookies"], "reward_energy": promo["reward_energy"],
            "cookies": db.get_user(tg["id"])["cookies"]}


# ---------- батл-пасс ----------

@router.get("/battlepass")
async def battlepass(tg: dict = Depends(tg_user)):
    gl.finalize_seasons()
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "err_no_user")
    bp_level = cfg.bp_level_for_xp(user["bp_xp"])
    claimed_free = json.loads(user["bp_claimed_free"] or "[]")
    claimed_prem = json.loads(user["bp_claimed_premium"] or "[]")
    income = gl.hourly_income(tg["id"])   # награды пасса масштабируются доходом
    levels = []
    for lvl in range(1, cfg.BP_MAX_LEVEL + 1):
        levels.append({
            "level": lvl,
            "free": cfg.bp_reward(lvl, False, income),
            "premium": cfg.bp_reward(lvl, True, income),
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
    if not user:                      # единственная ручка без этой проверки:
        raise HTTPException(404, "err_no_user")   # раньше падала в 500
    if body.track not in ("free", "premium"):
        raise HTTPException(400, "err_no_item")
    bp_level = cfg.bp_level_for_xp(user["bp_xp"])
    if body.level < 1 or body.level > bp_level:
        raise HTTPException(400, "err_bp_locked")
    if body.track == "premium" and not user["bp_premium"]:
        raise HTTPException(400, "err_need_premium")
    col = "bp_claimed_free" if body.track == "free" else "bp_claimed_premium"
    reward = cfg.bp_reward(body.level, body.track == "premium",
                           gl.hourly_income(tg["id"]))
    with db.tx():  # отметка о клейме и награда — одним куском
        # список перечитывается УЖЕ внутри транзакции: чтение снаружи и запись
        # целиком внутри теряли уровень при гонке, и его можно было забрать
        # повторно
        claimed = json.loads(db.get_user(tg["id"])[col] or "[]")
        if body.level in claimed:
            raise HTTPException(400, "err_claimed")
        claimed.append(body.level)
        db.update_user(tg["id"], **{col: json.dumps(claimed)})
        if reward["cookies"]:
            gl.add_cookies(tg["id"], reward["cookies"], count_earned=False)
        if reward.get("energy"):
            fresh = gl.refresh_energy(db.get_user(tg["id"]))
            # клэмп по потолку: излишек срезал бы первый же refresh_energy
            db.update_user(tg["id"], energy=min(gl.energy_cap(fresh),
                                                fresh["energy"] + reward["energy"]))
    return {"reward": reward, "cookies": db.get_user(tg["id"])["cookies"]}


# ---------- магазин (Stars) ----------

@router.get("/shop")
async def shop(tg: dict = Depends(tg_user)):
    """Тексты локализуются по X-Lang; для пачек с income_hours считаем
    персональную сумму — покупатель видит, сколько получит именно он."""
    from server.i18n import tr
    income = gl.hourly_income(tg["id"])
    user = db.get_user(tg["id"])
    items = []
    for k, (_t, _d, s, effect) in cfg.SHOP_ITEMS.items():
        blocked = gl.purchase_blocked(tg["id"], k)
        # товар, который этому игроку ещё рано покупать (апгрейд тира без
        # базового), просто не показываем — вместо него виден базовый
        if blocked == "err_needs_base":
            continue
        item = {"key": k, "title": tr(tg["lang"], f"shop_{k}_t"),
                "desc": tr(tg["lang"], f"shop_{k}_d"), "stars": s,
                "owned": blocked == "err_owned"}
        if effect.get("type") == "cookies" and "income_hours" in effect:
            item["amount"] = max(effect["min_amount"],
                                 income * effect["income_hours"])
        items.append(item)
    # владелец младшего тира оффлайн-капа видит апгрейд по цене разницы,
    # а не полный тир, за который он переплатил бы 400⭐
    keys = {i["key"] for i in items if not i["owned"]}
    if "offline_cap_12h_up" in keys:
        items = [i for i in items if i["key"] != "offline_cap_12h"]
    return {"items": items}


class BuyIn(BaseModel):
    item_key: str


@router.post("/shop/invoice")
async def create_invoice(body: BuyIn, tg: dict = Depends(tg_user)):
    """Создаёт invoice-ссылку на оплату Stars через бота."""
    if body.item_key not in cfg.SHOP_ITEMS:
        raise HTTPException(400, "err_no_item")
    # постоянный апгрейд уже куплен (или тир ещё рано) — не даём заплатить
    # впустую. Дубль этой же проверки стоит в pre_checkout: invoice-ссылка
    # многоразовая, и старую можно оплатить в обход магазина
    blocked = gl.purchase_blocked(tg["id"], body.item_key)
    if blocked:
        raise HTTPException(400, blocked)
    from server.i18n import tr
    _t, _d, stars, _effect = cfg.SHOP_ITEMS[body.item_key]
    title = tr(tg["lang"], f"shop_{body.item_key}_t")
    desc = tr(tg["lang"], f"shop_{body.item_key}_d")

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
    """Сезонный топ ВНУТРИ СВОЕЙ ЛИГИ: лига определяется уровнем (новичок
    соревнуется с новичками), а место внутри лиги — заработком ЗА СЕЗОН, ведь
    именно он и обнуляется. Раньше сортировка шла по уровню, который сезон не
    сбрасывает: таблица стояла на месте, а престиж ронял игрока на дно.
    Топ-10 каждой лиги получают призы в конце сезона."""
    gl.finalize_seasons()
    season = gl.current_season()
    me = db.get_user(tg["id"])
    my_level = me["level"] if me else 1
    lkey, lo, hi = cfg.league_of(my_level)
    cond = "level >= ?" + (" AND level <= ?" if hi is not None else "")
    lparams = [lo] + ([hi] if hi is not None else [])

    top = db.q(
        f"SELECT user_id, username, first_name, level, season_earned "
        f"FROM users WHERE season_id = ? AND {cond} "
        f"ORDER BY season_earned DESC, level DESC LIMIT 100", [season] + lparams)
    for i, row in enumerate(top):
        row["rank"] = i + 1
        row["name"] = row.pop("first_name") or row.pop("username") or "Player"
        row.pop("username", None)
        row["is_me"] = row["user_id"] == tg["id"]
        row["prize"] = cfg.season_reward(i + 1, row["season_earned"])

    my_rank = None
    if me:
        my_rank = db.q1(
            f"SELECT COUNT(*) c FROM users WHERE season_id = ? AND {cond} AND "
            f"(season_earned > ? OR (season_earned = ? AND level > ?))",
            [season] + lparams + [me["season_earned"], me["season_earned"],
                                  me["level"]])["c"] + 1
    return {
        "top": top,
        "me": {"rank": my_rank, "season_earned": me["season_earned"] if me else 0},
        "players_total": db.q1(
            f"SELECT COUNT(*) c FROM users WHERE season_id = ? AND {cond}",
            [season] + lparams)["c"],
        "league": {"key": lkey, "min_level": lo, "max_level": hi,
                   "all": [k for k, _lo in cfg.LEAGUES]},
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
        "reward": cfg.scaled_reward(cfg.CHANNEL_REWARD, "channel",
                                    gl.hourly_income(tg["id"])),
        "claimed": bool(user and user["channel_claimed"]),
    }


@router.post("/channel/claim")
async def channel_claim(tg: dict = Depends(tg_user)):
    if not CHANNEL_USERNAME:
        raise HTTPException(400, "err_no_channel")
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "err_no_user")
    if user["channel_claimed"]:
        raise HTTPException(400, "err_claimed")

    from bot.loader import bot
    try:
        member = await bot.get_chat_member(f"@{CHANNEL_USERNAME}", tg["id"])
    except Exception:
        raise HTTPException(400, "err_check_failed")
    if member.status in ("left", "kicked"):
        raise HTTPException(400, "err_not_subscribed")

    reward = cfg.scaled_reward(cfg.CHANNEL_REWARD, "channel",
                               gl.hourly_income(tg["id"]))
    with db.tx():
        # условный UPDATE закрывает гонку: два запроса могли пройти проверку
        # выше до await get_chat_member — награду получит только один
        db.exec("UPDATE users SET channel_claimed = 1 "
                "WHERE user_id = ? AND channel_claimed = 0", (tg["id"],))
        if db.cursor.rowcount == 0:
            raise HTTPException(400, "err_claimed")
        gl.add_cookies(tg["id"], reward, count_earned=False)
    return {"reward": reward, "cookies": db.get_user(tg["id"])["cookies"]}
