"""Внутриигровой магазин за печеньки: ферма (автофарм), апгрейды, скины."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import game_config as cfg
from server import game_logic as gl
from server.auth import tg_user
from server.game_logic import db

router = APIRouter(prefix="/api/farm")


def _user(tg: dict) -> dict:
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "err_no_user")
    return user


@router.get("")
async def farm_state(tg: dict = Depends(tg_user)):
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
        "offline_cap_hours": cfg.FARM_OFFLINE_CAP_HOURS,
    }


class KeyIn(BaseModel):
    key: str


@router.post("/buy_building")
async def buy_building(body: KeyIn, tg: dict = Depends(tg_user)):
    user = _user(tg)
    b = cfg.FARM_BUILDINGS.get(body.key)
    if not b:
        raise HTTPException(400, "err_no_item")
    if user["level"] < b["req_level"]:
        raise HTTPException(400, f"err_req_level|{b['req_level']}")
    # доход до покупки забираем по старой ставке, чтобы новая не задним числом
    gl.collect_farm(user)
    user = db.get_user(tg["id"])
    owned = gl.farm_counts(tg["id"]).get(body.key, 0)
    cost = cfg.building_cost(body.key, owned)
    if user["cookies"] < cost:
        raise HTTPException(400, "err_no_cookies")
    with db.tx():  # списание и здание — одним куском
        db.update_user(tg["id"], cookies=user["cookies"] - cost)
        db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, ?, 1) "
                "ON CONFLICT(user_id, building_key) DO UPDATE SET count = count + 1",
                (tg["id"], body.key))
        gl.quest_progress(tg["id"], "buildings", 1)
    return await farm_state(tg)


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
    if user["cookies"] < u["cost"]:
        raise HTTPException(400, "err_no_cookies")
    # фарм-доход до апгрейда — по старой ставке
    gl.collect_farm(user)
    user = db.get_user(tg["id"])
    with db.tx():  # списание и апгрейд — одним куском
        db.update_user(tg["id"], cookies=user["cookies"] - u["cost"])
        db.exec("INSERT OR IGNORE INTO upgrades (user_id, upgrade_key) VALUES (?, ?)",
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
    if user["cookies"] < s["cost"]:
        raise HTTPException(400, "err_no_cookies")
    with db.tx():  # списание и скин — одним куском
        db.update_user(tg["id"], cookies=user["cookies"] - s["cost"])
        db.exec("INSERT OR IGNORE INTO skins (user_id, skin_key) VALUES (?, ?)",
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
