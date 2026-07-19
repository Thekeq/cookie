"""Основной геймплей: state, кликер, merge-доска, уровни, достижения."""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from server import game_config as cfg
from server import game_logic as gl
from server.auth import tg_user
from server.game_logic import db

router = APIRouter(prefix="/api")


def _ensure_user(tg: dict) -> dict:
    user = db.get_user(tg["id"])
    if not user:
        raise HTTPException(404, "err_no_user")
    return user


# ---------- state ----------

@router.get("/state")
async def get_state(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    gl.finalize_seasons()
    passive = gl.collect_passive(user)
    farm_income = gl.collect_farm(db.get_user(tg["id"]))
    state = gl.full_state(tg["id"])
    state["passive_collected"] = passive + farm_income
    return state


# ---------- ежедневная награда ----------

@router.get("/daily")
async def daily(tg: dict = Depends(tg_user)):
    return gl.daily_state(_ensure_user(tg))


@router.post("/daily/claim")
async def daily_claim(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_daily(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["cookies"] = db.get_user(tg["id"])["cookies"]
    r["daily"] = gl.daily_state(db.get_user(tg["id"]))
    return r


# ---------- ежедневные задания ----------

@router.get("/quests")
async def quests(tg: dict = Depends(tg_user)):
    _ensure_user(tg)
    return {"quests": gl.quests_state(tg["id"])}


class ClaimQuest(BaseModel):
    key: str


@router.post("/quests/claim")
async def quest_claim(body: ClaimQuest, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_quest(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["cookies"] = db.get_user(tg["id"])["cookies"]
    r["quests"] = gl.quests_state(tg["id"])
    return r


# ---------- кликер ----------

class ClickBatch(BaseModel):
    clicks: int         # сколько кликов накопил клиент с прошлой отправки
    batch_id: str = ""  # уникальный id батча: повтор (ретрай) не начисляется дважды


@router.post("/click")
async def click(batch: ClickBatch, tg: dict = Depends(tg_user)):
    now = time.time()
    # ВСЁ внутри одной транзакции (BEGIN IMMEDIATE = write-lock):
    # параллельный worker дождётся и увидит уже обновлённое состояние,
    # а упавший посередине батч откатится целиком вместе со своим batch_id
    with db.tx():
        gl.refresh_energy(_ensure_user(tg))
        # доход фермы/доски капает и во время тапа: собираем каждый батч,
        # чтобы cookies в ответе не «откатывали» баланс у богатых игроков
        gl.collect_passive(db.get_user(tg["id"]))
        gl.collect_farm(db.get_user(tg["id"]))
        user = db.get_user(tg["id"])

        # дедупликация по (user_id, batch_id): id уникален для каждого батча,
        # поэтому честные батчи с другого устройства не отбрасываются
        if batch.batch_id:
            db.exec("INSERT OR IGNORE INTO click_batches (user_id, batch_id, "
                    "created_at) VALUES (?, ?, ?)",
                    (tg["id"], batch.batch_id[:64], now))
            if db.cursor.rowcount == 0:  # уже обработан — ретрай потерянного ответа
                return {"accepted": 0, "earned": 0, "duplicate": True,
                        "combo": gl.current_combo(user),
                        "energy": user["energy"], "cookies": user["cookies"],
                        "xp": user["xp"], "golden": gl.golden_state(user)}
            # TTL: чистим свои записи старше часа
            db.exec("DELETE FROM click_batches WHERE user_id = ? AND created_at < ?",
                    (tg["id"], now - 3600))

        clicks = max(0, min(batch.clicks, 200))  # защита от мусора

        # CPS-лимит: окно копит "допустимые" клики со скоростью MAX_CPS.
        # Живёт в БД — переживает рестарт и несколько worker-процессов
        last_ts, allowance = user["cps_ts"], user["cps_allowance"]
        if not last_ts:
            allowance = float(cfg.MAX_CPS)
        allowance = min(cfg.MAX_CPS * 3, allowance + (now - last_ts) * cfg.MAX_CPS)
        clicks = int(min(clicks, allowance))

        # энергия
        clicks = int(min(clicks, user["energy"] // cfg.ENERGY_PER_CLICK))
        if clicks <= 0:
            db.update_user(tg["id"], cps_ts=now, cps_allowance=allowance)
            return {"accepted": 0, "earned": 0, "combo": gl.current_combo(user),
                    "energy": user["energy"], "cookies": user["cookies"]}

        combo = gl.update_combo(user, clicks, now)
        earned = (clicks * cfg.click_power(user["click_level"])
                  * gl.click_multiplier(tg["id"]) * combo)

        # дневной счётчик кликов: после мягкого капа XP за клик режется вчетверо
        today = gl._utc_day(now)
        day_count = user["clicks_day_count"] if user["clicks_day"] == today else 0
        under_cap = max(0, min(clicks, cfg.CLICK_XP_SOFT_CAP - day_count))
        xp = under_cap * cfg.CLICK_XP_RATE + (clicks - under_cap) * cfg.CLICK_XP_RATE_CAPPED

        db.update_user(
            tg["id"],
            energy=user["energy"] - clicks * cfg.ENERGY_PER_CLICK,
            total_clicks=user["total_clicks"] + clicks,
            clicks_day=today, clicks_day_count=day_count + clicks,
            cps_ts=now, cps_allowance=allowance - clicks,
        )
        gl.add_cookies(tg["id"], earned)
        gl.add_xp(db.get_user(tg["id"]), xp)
        gl.quest_progress(tg["id"], "clicks", clicks)

    fresh = db.get_user(tg["id"])
    return {"accepted": clicks, "earned": earned, "combo": combo,
            "energy": fresh["energy"], "cookies": fresh["cookies"], "xp": fresh["xp"],
            "golden": gl.golden_state(fresh)}


@router.post("/click/upgrade")
async def upgrade_click(tg: dict = Depends(tg_user)):
    _ensure_user(tg)
    user = gl.collect_all(tg["id"])  # свежий баланс: с натикавшим доходом
    cost = cfg.click_upgrade_cost(user["click_level"])
    if user["cookies"] < cost:
        raise HTTPException(400, "err_no_cookies")
    db.update_user(tg["id"], cookies=user["cookies"] - cost,
                   click_level=user["click_level"] + 1)
    return gl.full_state(tg["id"])


# ---------- золотая печенька ----------

@router.post("/golden/claim")
async def golden_claim(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_golden(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["cookies"] = db.get_user(tg["id"])["cookies"]
    return r


# ---------- престиж ----------

@router.get("/prestige")
async def prestige(tg: dict = Depends(tg_user)):
    return gl.prestige_state(_ensure_user(tg))


@router.post("/prestige")
async def prestige_do(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.do_prestige(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    state = gl.full_state(tg["id"])
    state["prestige_result"] = r
    return state


# ---------- merge ----------

class MergeMove(BaseModel):
    from_cell: int
    to_cell: int


def _board_map(user_id: int) -> dict[int, int]:
    return {r["cell"]: r["item_level"]
            for r in db.q("SELECT cell, item_level FROM board WHERE user_id = ?", (user_id,))}


class SpawnIn(BaseModel):
    level: int = 1  # прямая покупка печеньки уровня N (дорого, экономит слияния)


@router.post("/merge/spawn")
async def spawn(body: SpawnIn = SpawnIn(), tg: dict = Depends(tg_user)):
    _ensure_user(tg)
    user = gl.collect_all(tg["id"])  # свежий баланс: с натикавшим доходом
    board = _board_map(tg["id"])
    if len(board) >= cfg.BOARD_SIZE:
        raise HTTPException(400, "err_board_full")

    level = max(1, body.level)
    # прямой спавн ограничен: топ-тиры только слиянием
    max_unlocked = max((l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                        if cfg.item_unlock_level(l) <= user["level"]), default=1)
    max_direct = max(1, max_unlocked - cfg.SPAWN_DIRECT_GAP)
    if level > max_direct:
        raise HTTPException(400, f"err_direct_cap|{max_direct}")

    cost = cfg.direct_spawn_cost(level, len(board))
    if user["cookies"] < cost:
        raise HTTPException(400, "err_no_cookies")
    free_cells = [c for c in range(cfg.BOARD_SIZE) if c not in board]
    cell = free_cells[0]
    with db.tx():  # списание и печенька на доске — одним куском
        db.update_user(tg["id"], cookies=user["cookies"] - cost)
        db.exec("INSERT INTO board (user_id, cell, item_level) VALUES (?, ?, ?)",
                (tg["id"], cell, level))
        gl.quest_progress(tg["id"], "spawns", 1)
    return gl.full_state(tg["id"])


@router.post("/merge/move")
async def move(mv: MergeMove, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    if not (0 <= mv.from_cell < cfg.BOARD_SIZE and 0 <= mv.to_cell < cfg.BOARD_SIZE) \
            or mv.from_cell == mv.to_cell:
        raise HTTPException(400, "err_bad_move")
    board = _board_map(tg["id"])
    if mv.from_cell not in board:
        raise HTTPException(400, "err_empty_cell")

    src = board[mv.from_cell]
    if mv.to_cell not in board:
        # просто перенос
        db.exec("UPDATE board SET cell = ? WHERE user_id = ? AND cell = ?",
                (mv.to_cell, tg["id"], mv.from_cell))
        return gl.full_state(tg["id"])

    dst = board[mv.to_cell]
    if src != dst:
        # свап — три шага через временную клетку, строго одной транзакцией
        with db.tx():
            db.exec("UPDATE board SET cell = -1 WHERE user_id = ? AND cell = ?", (tg["id"], mv.from_cell))
            db.exec("UPDATE board SET cell = ? WHERE user_id = ? AND cell = ?",
                    (mv.from_cell, tg["id"], mv.to_cell))
            db.exec("UPDATE board SET cell = ? WHERE user_id = ? AND cell = -1", (mv.to_cell, tg["id"]))
        return gl.full_state(tg["id"])

    # merge!
    new_level = src + 1
    if new_level > cfg.MAX_ITEM_LEVEL:
        raise HTTPException(400, "err_max_item")
    if cfg.item_unlock_level(new_level) > user["level"]:
        raise HTTPException(400, f"err_item_locked|{cfg.item_unlock_level(new_level)}")
    with db.tx():  # удаление + апгрейд + счётчики — одним куском
        db.exec("DELETE FROM board WHERE user_id = ? AND cell = ?", (tg["id"], mv.from_cell))
        db.exec("UPDATE board SET item_level = ? WHERE user_id = ? AND cell = ?",
                (new_level, tg["id"], mv.to_cell))
        db.update_user(tg["id"], total_merges=user["total_merges"] + 1)
        gl.add_xp(db.get_user(tg["id"]), cfg.merge_reward_xp(new_level))
        gl.quest_progress(tg["id"], "merges", 1)

    state = gl.full_state(tg["id"])
    state["merged_level"] = new_level
    return state


# ---------- уровни ----------

@router.get("/levels")
async def levels(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    path = []
    for lvl in range(1, cfg.MAX_LEVEL + 1):
        unlocks = [i for i in range(1, cfg.MAX_ITEM_LEVEL + 1) if cfg.item_unlock_level(i) == lvl]
        path.append({
            "level": lvl,
            "xp_required": cfg.xp_for_level(lvl),
            "reward": cfg.level_reward(lvl),
            "unlocks_items": unlocks,
            "reached": user["level"] >= lvl,
        })
    return {"path": path, "current": user["level"], "xp": user["xp"],
            "claimable": gl.claimable_level(user)}


@router.post("/levels/claim")
async def claim_level(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    nxt = gl.claimable_level(user)
    if not nxt:
        raise HTTPException(400, "err_no_xp")
    reward = cfg.level_reward(nxt)
    with db.tx():  # уровень + награда + refill — одним куском
        db.update_user(tg["id"], level=nxt)
        gl.add_cookies(tg["id"], reward["cookies"], count_earned=False)
        if reward.get("full_refill"):
            fresh = db.get_user(tg["id"])
            db.update_user(tg["id"], energy=gl.energy_cap(fresh),
                           energy_updated_at=time.time())
    state = gl.full_state(tg["id"])
    state["level_up"] = {"level": nxt, "reward": reward}
    return state


# ---------- достижения ----------

@router.get("/achievements")
async def achievements(tg: dict = Depends(tg_user)):
    return {"achievements": gl.achievements_state(_ensure_user(tg), tg["lang"])}


class ClaimAch(BaseModel):
    key: str


@router.post("/achievements/claim")
async def claim_ach(body: ClaimAch, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        reward = gl.claim_achievement(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"reward": reward, "cookies": db.get_user(tg["id"])["cookies"]}
