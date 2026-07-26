"""Основной геймплей: state, кликер, merge-доска, уровни, достижения."""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

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
    # самая тяжёлая ручка: 40+ обращений к БД, а SQLite синхронный и держит
    # весь процесс вместе с поллингом бота. Клиент поллит раз в 30 сек
    gl.check_rate_limit(tg["id"], "state", cfg.STATE_PER_MINUTE, 60)
    user = _ensure_user(tg)
    gl.ensure_user_season(tg["id"])
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
    user = _ensure_user(tg)
    return {"quests": gl.quests_state(tg["id"]),
            "reroll_available": user["quest_reroll_day"] != gl._utc_day(time.time())}


class RerollQuest(BaseModel):
    key: str = Field(max_length=64)


@router.post("/quests/reroll")
async def quest_reroll(body: RerollQuest, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        new_key = gl.reroll_quest(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"new_key": new_key, "quests": gl.quests_state(tg["id"]),
            "reroll_available": False}


class ClaimQuest(BaseModel):
    key: str = Field(max_length=64)


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
    # Field-границы обязательны: без max_length строка материализуется целиком
    # ещё до среза, и запрос с batch_id на 200 МБ клал процесс по памяти —
    # вместе с ботом, он в том же процессе
    clicks: int = Field(ge=0, le=200)
    # batch_id ОБЯЗАТЕЛЕН: раньше он был необязательным, и читерский клиент
    # просто не слал его, полностью отключая защиту от повторной отправки
    batch_id: str = Field(min_length=6, max_length=64)


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
            fresh = db.exec("INSERT OR IGNORE INTO click_batches (user_id, batch_id, "
                            "created_at) VALUES (?, ?, ?)",
                            (tg["id"], batch.batch_id[:64], now))
            if fresh == 0:  # уже обработан — ретрай потерянного ответа
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
        earned = (clicks * cfg.click_power(user["click_level"], gl.income_base(tg["id"]))
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
        gl.order_progress(tg["id"], "clicks", clicks)

    fresh = db.get_user(tg["id"])
    return {"accepted": clicks, "earned": earned, "combo": combo,
            "energy": fresh["energy"], "cookies": fresh["cookies"], "xp": fresh["xp"],
            "golden": gl.golden_state(fresh)}


@router.post("/click/upgrade")
async def upgrade_click(tg: dict = Depends(tg_user)):
    _ensure_user(tg)
    # сбор дохода + проверка цены + списание — одна транзакция: параллельная
    # покупка не спишет один и тот же баланс дважды
    with db.tx():
        user = gl.collect_all(tg["id"])
        # потолок по уровню игрока — тот же принцип, что req_level у зданий:
        # одних денег мало, иначе ветка клика разгоняет инфляцию без предела
        if user["click_level"] >= cfg.click_max_level(user["level"]):
            raise HTTPException(400, "err_click_max")
        cost = cfg.click_upgrade_cost(user["click_level"], gl.income_base(tg["id"]))
        if user["cookies"] < cost:
            raise HTTPException(400, "err_no_cookies")
        try:
            gl.buy_click_upgrade(tg["id"], cost, user["click_level"])
        except gl.NoFunds:
            raise HTTPException(400, "err_no_cookies")
    return gl.full_state(tg["id"])


# ---------- золотая печенька ----------

@router.post("/golden/claim")
async def golden_claim(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_golden(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # ВАЖНО: бонус живёт в "bonus", а "cookies" — это баланс после начисления.
    # Раньше баланс перезатирал бонус, и тост показывал «+весь твой баланс»
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
    from_cell: int = Field(ge=0, le=cfg.BOARD_SIZE - 1)
    to_cell: int = Field(ge=0, le=cfg.BOARD_SIZE - 1)


def _board_map(user_id: int) -> dict[int, int]:
    return {r["cell"]: r["item_level"]
            for r in db.q("SELECT cell, item_level FROM board WHERE user_id = ?", (user_id,))}


def _best_free_cell(free_cells: list[int], board: dict[int, int]) -> int:
    """Свободная клетка с максимумом занятых соседей: новая печенька ложится
    рядом с остальными, а не в дальний угол противня (сетка 5 в ряд)."""
    def neighbours(cell: int) -> int:
        row, col = divmod(cell, 5)
        around = []
        if col > 0:
            around.append(cell - 1)
        if col < 4:
            around.append(cell + 1)
        if row > 0:
            around.append(cell - 5)
        if row < 4:
            around.append(cell + 5)
        return sum(1 for c in around if c in board)

    return max(free_cells, key=lambda c: (neighbours(c), -c))


class SpawnIn(BaseModel):
    level: int = Field(default=1, ge=1, le=cfg.MAX_ITEM_LEVEL)


@router.post("/merge/spawn")
async def spawn(body: SpawnIn = SpawnIn(), tg: dict = Depends(tg_user)):
    _ensure_user(tg)
    # сбор дохода + все проверки + списание — одна транзакция
    with db.tx():
        user = gl.collect_all(tg["id"])
        # спавн только в ОТКРЫТЫЕ клетки; печеньки в закрытых (legacy) не мешают.
        # ВАЖНО: карту доски читаем ПОСЛЕ merge_cells_unlocked_for — внутри него
        # compact_board может перенумеровать клетки легаси-доски, и по старой
        # карте свободная клетка оказывалась занятой (IntegrityError -> 500)
        cells_open = gl.merge_cells_unlocked_for(user)
        board = _board_map(tg["id"])
        free_cells = [c for c in range(cells_open) if c not in board]
        if not free_cells:
            raise HTTPException(400, "err_board_full")

        level = max(1, body.level)
        # прямой спавн ограничен: топ-тиры только слиянием
        max_direct = gl._direct_max_level(user)
        if level > max_direct:
            raise HTTPException(400, f"err_direct_cap|{max_direct}")

        cost = cfg.direct_spawn_cost(level, len(board), gl.board_base_income(tg["id"]))
        if user["cookies"] < cost:
            raise HTTPException(400, "err_no_cookies")
        cell = _best_free_cell(free_cells, board)
        try:
            gl.spend_cookies(tg["id"], cost, "board_spawn",
                             ref_type="item_level", ref_id=str(level))
        except gl.NoFunds:
            raise HTTPException(400, "err_no_cookies")
        # paid — фактически вложенное; от него считается возврат при переплавке
        db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, ?, ?, ?)",
                (tg["id"], cell, level, cost))
        gl.quest_progress(tg["id"], "spawns", 1)
        gl.order_progress(tg["id"], "spawns", 1)
        # прямая покупка тоже бьёт рекорд: иначе игрок, купивший тир напрямую,
        # получал бы за него XP только после того, как соберёт его слиянием
        record = gl.claim_item_record(db.get_user(tg["id"]), level)
    state = gl.full_state(tg["id"])
    state["record"] = record
    return state


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
        # перенос в пустую клетку — только в открытую (из закрытой выйти можно)
        if mv.to_cell >= gl.merge_cells_unlocked_for(user):
            raise HTTPException(400, "err_cell_locked")
        db.exec("UPDATE board SET cell = ? WHERE user_id = ? AND cell = ?",
                (mv.to_cell, tg["id"], mv.from_cell))
        return gl.full_state(tg["id"])

    dst = board[mv.to_cell]
    if src != dst:
        # свап тоже только в открытую зону: иначе закрытой клеткой можно было
        # пользоваться, обменивая её содержимое (легаси-доски)
        if mv.to_cell >= gl.merge_cells_unlocked_for(user):
            raise HTTPException(400, "err_cell_locked")
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
        # вложенное складываем: слияние двух печенек стоило суммы их цен,
        # от этой суммы потом считается возврат при переплавке
        paid = db.q1("SELECT COALESCE(SUM(paid), 0) p FROM board "
                     "WHERE user_id = ? AND cell IN (?, ?)",
                     (tg["id"], mv.from_cell, mv.to_cell))["p"]
        db.exec("DELETE FROM board WHERE user_id = ? AND cell = ?", (tg["id"], mv.from_cell))
        db.exec("UPDATE board SET item_level = ?, paid = ? WHERE user_id = ? AND cell = ?",
                (new_level, paid, tg["id"], mv.to_cell))
        db.update_user(tg["id"], total_merges=user["total_merges"] + 1)
        gl.add_xp(db.get_user(tg["id"]), cfg.merge_reward_xp(new_level),
                  cfg.merge_reward_bp_xp(new_level))
        gl.quest_progress(tg["id"], "merges", 1)
        gl.order_progress(tg["id"], "merges", 1)
        gl.order_progress(tg["id"], "make_item", new_level)
        # рекорд тира — основной XP игры, начисляется один раз за тир
        record = gl.claim_item_record(db.get_user(tg["id"]), new_level)
        shiny_level = gl.roll_shiny(db.get_user(tg["id"]), new_level)
    if user["total_merges"] == 0:
        gl.track(tg["id"], "first_merge")

    state = gl.full_state(tg["id"])
    state["merged_level"] = new_level
    state["shiny"] = shiny_level is not None
    state["shiny_level"] = shiny_level
    state["record"] = record
    return state


class TrashIn(BaseModel):
    cell: int = Field(ge=0, le=cfg.BOARD_SIZE - 1)


@router.post("/merge/trash")
async def trash(body: TrashIn, tg: dict = Depends(tg_user)):
    """Печенька в мусорку/печь: клетка освобождается, возвращается TRASH_REFUND
    от ФАКТИЧЕСКИ вложенного (board.paid). По текущей цене считать нельзя:
    доска, собранная в бедности, переплавлялась бы по ценам богатого игрока."""
    _ensure_user(tg)
    if not (0 <= body.cell < cfg.BOARD_SIZE):
        raise HTTPException(400, "err_bad_move")
    with db.tx():
        row = db.q1("SELECT item_level, paid FROM board WHERE user_id = ? AND cell = ?",
                    (tg["id"], body.cell))
        if not row:
            raise HTTPException(400, "err_empty_cell")
        level = row["item_level"]
        db.exec("DELETE FROM board WHERE user_id = ? AND cell = ?", (tg["id"], body.cell))
        # строки, созданные до появления paid, оцениваем по минимальной цене
        invested = row["paid"] or cfg.legacy_item_value(level)
        refund = invested * cfg.TRASH_REFUND
        gl.add_cookies(tg["id"], refund, count_earned=False)
    gl.track(tg["id"], "trash_item", level)
    state = gl.full_state(tg["id"])
    state["trash_refund"] = refund
    return state


# ---------- уровни ----------

@router.get("/levels")
async def levels(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    income = gl.hourly_income(tg["id"])   # награда уровня не обесценивается
    path = []
    for lvl in range(1, cfg.MAX_LEVEL + 1):
        unlocks = [i for i in range(1, cfg.MAX_ITEM_LEVEL + 1) if cfg.item_unlock_level(i) == lvl]
        path.append({
            "level": lvl,
            "xp_required": cfg.xp_for_level(lvl),
            "reward": gl.level_reward_scaled(lvl, income),
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
    reward = gl.level_reward_scaled(nxt, gl.hourly_income(tg["id"]))
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
    key: str = Field(max_length=64)


@router.post("/achievements/claim")
async def claim_ach(body: ClaimAch, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        reward = gl.claim_achievement(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"reward": reward, "cookies": db.get_user(tg["id"])["cookies"]}


# ---------- заказы пекарни ----------

@router.get("/orders")
async def orders(tg: dict = Depends(tg_user)):
    return gl.orders_state(_ensure_user(tg))


class TakeOrder(BaseModel):
    slot: int = Field(ge=1, le=3)


@router.post("/orders/take")
async def order_take(body: TakeOrder, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        active = gl.take_order(user, body.slot)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"active": active}


@router.post("/orders/abandon")
async def order_abandon(tg: dict = Depends(tg_user)):
    """Отказ от заказа: тратит одну попытку из дневного лимита.
    Мёртвые заказы (цель недостижима после престижа) снимаются сами и бесплатно."""
    user = _ensure_user(tg)
    try:
        orders = gl.abandon_order(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"orders": orders}


@router.post("/orders/claim")
async def order_claim(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_order(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["cookies"] = db.get_user(tg["id"])["cookies"]
    r["orders"] = gl.orders_state(db.get_user(tg["id"]))
    return r


# ---------- стартовый чеклист ----------

@router.post("/tutorial/claim")
async def tutorial_claim(tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        r = gl.claim_tutorial(user)
    except ValueError as e:
        raise HTTPException(400, str(e))
    r["cookies"] = db.get_user(tg["id"])["cookies"]
    return r


# ---------- коллекция блестящих печенек ----------

@router.get("/collection")
async def collection(tg: dict = Depends(tg_user)):
    return gl.collection_state(_ensure_user(tg))


# ---------- оффлайн-рецепты ----------

class RecipeIn(BaseModel):
    key: str = Field(max_length=32)


@router.get("/recipes")
async def recipes(tg: dict = Depends(tg_user)):
    """Закваска перед выходом: оффлайн-кап превращается из штрафа в механику."""
    user = _ensure_user(tg)
    return {"recipes": gl.recipes_available(user),
            "active": gl.recipe_status(user)}


@router.post("/recipes/set")
async def recipe_set(body: RecipeIn, tg: dict = Depends(tg_user)):
    user = _ensure_user(tg)
    try:
        active = gl.set_recipe(user, body.key)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"active": active, "recipes": gl.recipes_available(db.get_user(tg["id"]))}


# ---------- дуэли ----------

@router.get("/duel")
async def duel_state(tg: dict = Depends(tg_user)):
    """Асинхронный забег 1x1 на сутки: конкретный соперник вместо витрины."""
    from server import duels
    return duels.state(_ensure_user(tg))


@router.post("/duel/find")
async def duel_find(tg: dict = Depends(tg_user)):
    from server import duels
    gl.check_rate_limit(tg["id"], "duel", cfg.DUEL_PER_MINUTE, 60)
    try:
        return duels.find(_ensure_user(tg))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/duel/cancel")
async def duel_cancel(tg: dict = Depends(tg_user)):
    from server import duels
    return duels.cancel(_ensure_user(tg))


@router.post("/duel/claim")
async def duel_claim(tg: dict = Depends(tg_user)):
    from server import duels
    try:
        return duels.claim(_ensure_user(tg))
    except ValueError as e:
        raise HTTPException(400, str(e))
