"""Внутриигровой магазин за печеньки: ферма (автофарм), апгрейды, скины."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from server import economy
from server import game_config as cfg
from server import game_logic as gl
from server.auth import tg_user
from server.deps import op_token
from server.game_logic import db

router = APIRouter(prefix="/api/farm")


def _user(tg: dict) -> dict:
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "err_no_user")
    return user


@router.get("")
async def farm_state(tg: dict = Depends(tg_user)):
    # единственный путь к сбору дохода, у которого не было лимитера вовсе.
    # От двойного начисления защищает CAS в collect_farm, это второй слой
    gl.check_rate_limit(tg["id"], "farm", cfg.STATE_PER_MINUTE, 60)
    return _farm_payload(tg)


def _farm_payload(tg: dict) -> dict:
    """Экран фермы отдельно от ручки и синхронно: покупка заворачивается в
    replayable, а там нельзя await — ответ считается внутри транзакции."""
    user = _user(tg)
    collected = gl.collect_farm(user)
    counts = gl.farm_counts(tg["id"])
    owned_upgrades = gl.user_upgrades(tg["id"])
    owned_skins = {r["skin_key"] for r in
                   db.q("SELECT skin_key FROM skins WHERE user_id = ?", (tg["id"],))}
    owned_skins.add("classic")

    buildings = []
    for key, b in cfg.FARM_BUILDINGS.items():
        owned = counts.get(key, 0)
        buildings.append({
            "key": key, "owned": owned, "cps_each": b["cps"],
            "cost": cfg.building_cost(key, owned),
            "req_level": b["req_level"],
            "unlocked": user["level"] >= b["req_level"],
        })
    upgrades = []
    for key, u in cfg.COOKIE_UPGRADES.items():
        upgrades.append({
            "key": key, "cost": u["cost"], "effect": u["effect"], "value": u["value"],
            "req_level": u["req_level"],
            "unlocked": user["level"] >= u["req_level"],
            "owned": key in owned_upgrades,
        })
    skins = []
    for key, s in cfg.COOKIE_SKINS_SHOP.items():
        skins.append({
            "key": key, "cost": s["cost"], "emoji": s["emoji"],
            "req_level": s["req_level"],
            "unlocked": user["level"] >= s["req_level"],
            "owned": key in owned_skins,
            "active": (user["active_skin"] or "classic") == key,
        })
    # эксклюзивные скины (за рефералов) показываем только владельцам
    for key, s in cfg.REF_EXCLUSIVE_SKIN.items():
        if key in owned_skins:
            skins.append({
                "key": key, "cost": 0, "emoji": s["emoji"], "req_level": 1,
                "unlocked": True, "owned": True,
                "active": (user["active_skin"] or "classic") == key,
            })
    return {
        "collected": collected,
        "cps": gl.farm_cps(tg["id"]),
        "cookies": db.get_user(tg["id"])["cookies"],
        "buildings": buildings,
        "upgrades": upgrades,
        "skins": skins,
        "offline_cap_hours": gl.farm_offline_cap_hours(user),
    }


class KeyIn(BaseModel):
    key: str = Field(max_length=64)


@router.post("/buy_building")
async def buy_building(body: KeyIn, tg: dict = Depends(tg_user),
                       op: str = Depends(op_token)):
    return economy.replayable(op, tg["id"], "buy_building",
                              lambda: _buy_building(body, tg))


def _buy_building(body: "KeyIn", tg: dict) -> dict:
    user = _user(tg)
    b = cfg.FARM_BUILDINGS.get(body.key)
    if not b:
        raise HTTPException(400, "err_no_item")
    if user["level"] < b["req_level"]:
        raise HTTPException(400, f"err_req_level|{b['req_level']}")
    # сбор дохода (по старой ставке) + проверка цены + покупка — одна транзакция:
    # проверяем тот же баланс, что видит игрок, без гонок параллельных покупок
    with db.tx():
        user = gl.collect_all(tg["id"])
        owned = gl.farm_counts(tg["id"]).get(body.key, 0)
        cost = cfg.building_cost(body.key, owned)
        if user["cookies"] < cost:
            raise HTTPException(400, "err_no_cookies")
        try:
            gl.spend_cookies(tg["id"], cost, "farm_building",
                             ref_type="building", ref_id=body.key)
        except gl.NoFunds:
            raise HTTPException(400, "err_no_cookies")
        db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, building_key) DO UPDATE SET count = count + 1",
                (tg["id"], body.key))
        gl.quest_progress(tg["id"], "buildings", 1)
        gl.order_progress(tg["id"], "buildings", 1, spent=cost)
    if not owned and not gl.farm_counts(tg["id"]).keys() - {body.key}:
        gl.track(tg["id"], "first_building")
    return _farm_payload(tg)


@router.post("/buy_upgrade")
async def buy_upgrade(body: KeyIn, tg: dict = Depends(tg_user)):
    user = _user(tg)
    u = cfg.COOKIE_UPGRADES.get(body.key)
    if not u:
        raise HTTPException(400, "err_no_item")
    if user["level"] < u["req_level"]:
        raise HTTPException(400, f"err_req_level|{u['req_level']}")
    if body.key in gl.user_upgrades(tg["id"]):
        raise HTTPException(400, "err_owned")
    # сбор натикавшего дохода + проверка цены + покупка — одна транзакция
    with db.tx():
        user = gl.collect_all(tg["id"])
        if user["cookies"] < u["cost"]:
            raise HTTPException(400, "err_no_cookies")
        try:
            gl.spend_cookies(tg["id"], u["cost"], "farm_upgrade",
                             ref_type="upgrade", ref_id=body.key)
        except gl.NoFunds:
            raise HTTPException(400, "err_no_cookies")
        db.exec("INSERT INTO upgrades (user_id, upgrade_key) VALUES (?, ?) "
                "ON CONFLICT (user_id, upgrade_key) DO NOTHING",
                (tg["id"], body.key))
    return await farm_state(tg)


@router.post("/buy_skin")
async def buy_skin(body: KeyIn, tg: dict = Depends(tg_user)):
    user = _user(tg)
    s = cfg.COOKIE_SKINS_SHOP.get(body.key)
    if not s:
        raise HTTPException(400, "err_no_item")
    if user["level"] < s["req_level"]:
        raise HTTPException(400, f"err_req_level|{s['req_level']}")
    owned = {r["skin_key"] for r in
             db.q("SELECT skin_key FROM skins WHERE user_id = ?", (tg["id"],))} | {"classic"}
    if body.key in owned:
        raise HTTPException(400, "err_owned")
    with db.tx():  # сбор дохода + проверка цены + покупка — одна транзакция
        user = gl.collect_all(tg["id"])
        if user["cookies"] < s["cost"]:
            raise HTTPException(400, "err_no_cookies")
        try:
            gl.spend_cookies(tg["id"], s["cost"], "skin_purchase",
                             ref_type="skin", ref_id=body.key)
        except gl.NoFunds:
            raise HTTPException(400, "err_no_cookies")
        db.exec("INSERT INTO skins (user_id, skin_key) VALUES (?, ?) "
                "ON CONFLICT (user_id, skin_key) DO NOTHING",
                (tg["id"], body.key))
    return await farm_state(tg)


@router.post("/set_skin")
async def set_skin(body: KeyIn, tg: dict = Depends(tg_user)):
    _user(tg)
    owned = {r["skin_key"] for r in
             db.q("SELECT skin_key FROM skins WHERE user_id = ?", (tg["id"],))} | {"classic"}
    if body.key not in owned:
        raise HTTPException(400, "err_not_owned")
    db.update_user(tg["id"], active_skin=body.key)
    return await farm_state(tg)
