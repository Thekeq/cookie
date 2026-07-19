"""Общая игровая логика поверх БД. Сервер — единственный источник правды."""
import datetime
import json
import random
import time

from db import DataBase
from server import game_config as cfg

db = DataBase("data.db")


# ---------- сезоны ----------

def current_season(now: float | None = None) -> int:
    now = now or time.time()
    return max(0, int((now - cfg.SEASON_EPOCH) // (cfg.SEASON_LENGTH_DAYS * 86400)))


def season_end_ts(season: int) -> float:
    return cfg.SEASON_EPOCH + (season + 1) * cfg.SEASON_LENGTH_DAYS * 86400


def finalize_seasons():
    """Ленивый ролловер: если есть юзеры из прошлых сезонов — снапшотим их топ,
    раздаём награды и сбрасываем сезонный прогресс. Вызывается из auth/state."""
    cur = current_season()
    stale = db.q("SELECT DISTINCT season_id s FROM users WHERE season_id < ?", (cur,))
    for row in stale:
        season = row["s"]
        top = db.q(
            "SELECT user_id, season_earned FROM users "
            "WHERE season_id = ? AND season_earned > 0 "
            "ORDER BY season_earned DESC LIMIT 10", (season,))
        now = time.time()
        for i, u in enumerate(top):
            reward = cfg.season_reward(i + 1, u["season_earned"])
            db.exec(
                "INSERT INTO season_results (season_id, user_id, rank, earned, "
                "reward_cookies, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (season, u["user_id"], i + 1, u["season_earned"], reward, now))
        # сброс сезонного прогресса всем игрокам этого сезона
        db.exec(
            "UPDATE users SET season_id = ?, season_earned = 0, bp_xp = 0, "
            "bp_premium = 0, bp_claimed_free = '[]', bp_claimed_premium = '[]' "
            "WHERE season_id = ?", (cur, season))
        # награды топам — уже после сброса, чтобы не попали в новый сезон
        for i, u in enumerate(top):
            reward = cfg.season_reward(i + 1, u["season_earned"])
            if reward:
                add_cookies(u["user_id"], reward, count_earned=False)


def my_last_season_result(user_id: int) -> dict | None:
    return db.q1(
        "SELECT season_id, rank, earned, reward_cookies FROM season_results "
        "WHERE user_id = ? ORDER BY season_id DESC LIMIT 1", (user_id,))


# ---------- ежедневная награда (стрик) ----------

def _utc_day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def daily_state(user: dict) -> dict:
    now = time.time()
    today = _utc_day(now)
    last_day = _utc_day(user["daily_claimed_at"]) if user["daily_claimed_at"] else ""
    yesterday = _utc_day(now - 86400)
    can_claim = last_day != today
    # если пропустил день — стрик сгорел и следующий клейм начнёт с 1;
    # если уже забрал сегодня — «следующий» это завтрашний (стрик+1)
    if can_claim:
        next_streak = user["daily_streak"] + 1 if last_day == yesterday else 1
    else:
        next_streak = user["daily_streak"] + 1
    return {
        "can_claim": can_claim,
        "streak": user["daily_streak"],
        "next_streak": next_streak,
        "next_reward": cfg.daily_reward(next_streak),
        "rewards": [{"day": d, "cookies": c} for d, c in sorted(cfg.DAILY_REWARDS.items())],
    }


def claim_daily(user: dict) -> dict:
    """Возвращает {streak, reward} или кидает ValueError."""
    now = time.time()
    today = _utc_day(now)
    last_day = _utc_day(user["daily_claimed_at"]) if user["daily_claimed_at"] else ""
    if last_day == today:
        raise ValueError("err_already_today")
    yesterday = _utc_day(now - 86400)
    streak = user["daily_streak"] + 1 if last_day == yesterday else 1
    reward = cfg.daily_reward(streak)
    db.update_user(user["user_id"], daily_streak=streak, daily_claimed_at=now)
    add_cookies(user["user_id"], reward, count_earned=False)
    return {"streak": streak, "reward": reward}


# ---------- ежедневные задания ----------

def todays_quest_keys(day: str | None = None) -> list[str]:
    """Детерминированный выбор заданий дня — одинаковый для всех игроков."""
    day = day or _utc_day(time.time())
    rnd = random.Random(day)
    return rnd.sample(sorted(cfg.DAILY_QUEST_POOL), cfg.DAILY_QUESTS_PER_DAY)


def _ensure_quest_rows(user_id: int, day: str, keys: list[str]):
    for key in keys:
        db.exec("INSERT OR IGNORE INTO daily_quests (user_id, day, quest_key) "
                "VALUES (?, ?, ?)", (user_id, day, key))


def quest_reward_cookies(user_id: int, base: float) -> float:
    """Награда квеста: базовая сумма ИЛИ полчаса дохода игрока — что больше.
    Так квесты не превращаются в мусор для прокачанных игроков."""
    return max(base, hourly_income(user_id) * 0.5)


def quests_state(user_id: int) -> list[dict]:
    day = _utc_day(time.time())
    keys = todays_quest_keys(day)
    _ensure_quest_rows(user_id, day, keys)
    rows = {r["quest_key"]: r for r in db.q(
        "SELECT quest_key, progress, claimed FROM daily_quests "
        "WHERE user_id = ? AND day = ?", (user_id, day))}
    out = []
    for key in keys:
        q = cfg.DAILY_QUEST_POOL[key]
        r = rows.get(key, {"progress": 0, "claimed": 0})
        out.append({
            "key": key, "metric": q["metric"], "goal": q["goal"],
            "reward_cookies": quest_reward_cookies(user_id, q["reward_cookies"]),
            "reward_bp_xp": q["reward_bp_xp"],
            "progress": min(r["progress"], q["goal"]),
            "done": r["progress"] >= q["goal"], "claimed": bool(r["claimed"]),
        })
    return out


def quest_progress(user_id: int, metric: str, amount: float):
    """Инкремент прогресса заданий дня с данной метрикой. Дёшево: max 1-2 UPDATE."""
    if amount <= 0:
        return
    day = _utc_day(time.time())
    keys = [k for k in todays_quest_keys(day)
            if cfg.DAILY_QUEST_POOL[k]["metric"] == metric]
    if not keys:
        return
    _ensure_quest_rows(user_id, day, keys)
    for key in keys:
        db.exec("UPDATE daily_quests SET progress = progress + ? "
                "WHERE user_id = ? AND day = ? AND quest_key = ? AND claimed = 0",
                (amount, user_id, day, key))


def claim_quest(user: dict, key: str) -> dict:
    """Возвращает награду задания или кидает ValueError."""
    day = _utc_day(time.time())
    if key not in todays_quest_keys(day):
        raise ValueError("err_no_quest")
    q = cfg.DAILY_QUEST_POOL[key]
    row = db.q1("SELECT * FROM daily_quests WHERE user_id = ? AND day = ? AND quest_key = ?",
                (user["user_id"], day, key))
    if not row or row["progress"] < q["goal"]:
        raise ValueError("err_not_done")
    if row["claimed"]:
        raise ValueError("err_claimed")
    db.exec("UPDATE daily_quests SET claimed = 1 WHERE id = ?", (row["id"],))
    reward = quest_reward_cookies(user["user_id"], q["reward_cookies"])
    add_cookies(user["user_id"], reward, count_earned=False)
    db.update_user(user["user_id"], bp_xp=db.get_user(user["user_id"])["bp_xp"] + q["reward_bp_xp"])
    return {"reward_cookies": reward, "reward_bp_xp": q["reward_bp_xp"]}


def claimable_quests_count(user_id: int) -> int:
    return sum(1 for q in quests_state(user_id) if q["done"] and not q["claimed"])


# ---------- milestone-награды рефералки ----------

def ref_milestones_state(user_id: int) -> list[dict]:
    refs = db.q1("SELECT COUNT(*) c FROM referrals WHERE referrer_id = ?", (user_id,))["c"]
    claimed = {r["milestone_key"] for r in
               db.q("SELECT milestone_key FROM ref_claims WHERE user_id = ?", (user_id,))}
    out = []
    for key, ms in cfg.REF_MILESTONES.items():
        out.append({
            "key": key, "count": ms["count"], "type": ms["type"],
            "progress": min(refs, ms["count"]),
            "done": refs >= ms["count"], "claimed": key in claimed,
        })
    return out


def claim_ref_milestone(user: dict, key: str) -> dict:
    ms = cfg.REF_MILESTONES.get(key)
    if not ms:
        raise ValueError("err_no_item")
    state = {m["key"]: m for m in ref_milestones_state(user["user_id"])}[key]
    if not state["done"]:
        raise ValueError("err_not_done")
    if state["claimed"]:
        raise ValueError("err_claimed")
    db.exec("INSERT INTO ref_claims (user_id, milestone_key, claimed_at) VALUES (?, ?, ?)",
            (user["user_id"], key, time.time()))
    if ms["type"] == "boost":
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                (user["user_id"], "click_x2", time.time() + ms["hours"] * 3600))
    elif ms["type"] == "skin":
        db.exec("INSERT INTO skins (user_id, skin_key) VALUES (?, ?)",
                (user["user_id"], ms["skin"]))
    elif ms["type"] == "bp_premium":
        db.update_user(user["user_id"], bp_premium=1)
    return {"type": ms["type"]}


# ---------- скины (магазин + эксклюзивы) ----------

def skin_emoji(key: str) -> str:
    if key in cfg.COOKIE_SKINS_SHOP:
        return cfg.COOKIE_SKINS_SHOP[key]["emoji"]
    if key in cfg.REF_EXCLUSIVE_SKIN:
        return cfg.REF_EXCLUSIVE_SKIN[key]["emoji"]
    return cfg.COOKIE_SKINS_SHOP["classic"]["emoji"]


# ---------- апгрейды за печеньки ----------

def user_upgrades(user_id: int) -> set[str]:
    return {r["upgrade_key"] for r in
            db.q("SELECT upgrade_key FROM upgrades WHERE user_id = ?", (user_id,))}


def upgrade_effects(user_id: int) -> dict:
    """Суммарные эффекты купленных апгрейдов."""
    eff = {"click_mult": 1.0, "farm_mult": 1.0, "energy_cap": 0,
           "energy_regen": 0.0, "passive_mult": 1.0}
    for key in user_upgrades(user_id):
        u = cfg.COOKIE_UPGRADES.get(key)
        if not u:
            continue
        if u["effect"] in ("click_mult", "farm_mult", "passive_mult"):
            eff[u["effect"]] *= u["value"]
        else:
            eff[u["effect"]] += u["value"]
    return eff


# ---------- энергия ----------

def energy_cap(user: dict, eff: dict | None = None) -> int:
    eff = eff or upgrade_effects(user["user_id"])
    return cfg.max_energy(user["level"]) + int(eff["energy_cap"])


def refresh_energy(user: dict) -> dict:
    """Доначисляет энергию по прошедшему времени. Возвращает свежего юзера."""
    now = time.time()
    eff = upgrade_effects(user["user_id"])
    cap = energy_cap(user, eff)
    regen = cfg.ENERGY_REGEN_PER_SEC + eff["energy_regen"]
    elapsed = max(0, now - (user["energy_updated_at"] or now))
    energy = min(cap, user["energy"] + elapsed * regen)
    db.update_user(user["user_id"], energy=energy, energy_updated_at=now)
    user = dict(user, energy=energy, energy_updated_at=now)
    return user


# ---------- бусты ----------

def active_boosts(user_id: int) -> list[str]:
    now = time.time()
    rows = db.q("SELECT boost_key FROM boosts WHERE user_id = ? AND expires_at > ?", (user_id, now))
    return [r["boost_key"] for r in rows]


def click_multiplier(user_id: int) -> float:
    mult = upgrade_effects(user_id)["click_mult"]
    boosts = active_boosts(user_id)
    if "click_x2" in boosts:
        mult *= cfg.BOOST_CLICK_X2_MULT
    if "golden_frenzy" in boosts:
        mult *= cfg.GOLDEN_EFFECTS["frenzy"]["mult"]
    user = db.get_user(user_id)
    return mult * cfg.prestige_multiplier(user["prestige_points"] if user else 0)


# ---------- золотая печенька ----------

def golden_state(user: dict) -> dict:
    """Планирует/активирует золотую печеньку. Вся логика времени — на сервере."""
    import random as _r
    now = time.time()
    fields = {}
    # первая инициализация расписания
    if not user["golden_next_at"]:
        fields["golden_next_at"] = now + _r.uniform(
            cfg.GOLDEN_MIN_INTERVAL, cfg.GOLDEN_MAX_INTERVAL)
    # пора появиться (и предыдущая не активна)
    elif now >= user["golden_next_at"] and now >= user["golden_expires_at"]:
        keys = list(cfg.GOLDEN_EFFECTS)
        weights = [cfg.GOLDEN_EFFECTS[k]["weight"] for k in keys]
        fields["golden_effect"] = _r.choices(keys, weights=weights)[0]
        fields["golden_expires_at"] = now + cfg.GOLDEN_LIFETIME
        fields["golden_next_at"] = now + _r.uniform(
            cfg.GOLDEN_MIN_INTERVAL, cfg.GOLDEN_MAX_INTERVAL)
    if fields:
        db.update_user(user["user_id"], **fields)
        user = dict(user, **fields)
    active = now < user["golden_expires_at"]
    return {
        "active": active,
        "effect": user["golden_effect"] if active else None,
        "expires_at": user["golden_expires_at"] if active else 0,
    }


def claim_golden(user: dict) -> dict:
    """Тап по золотой печеньке. Возвращает применённый эффект или ValueError."""
    now = time.time()
    if now >= user["golden_expires_at"]:
        raise ValueError("err_golden_gone")
    effect = user["golden_effect"] or "chain"
    db.update_user(user["user_id"], golden_expires_at=0)
    if effect == "frenzy":
        e = cfg.GOLDEN_EFFECTS["frenzy"]
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                (user["user_id"], "golden_frenzy", now + e["seconds"]))
        return {"effect": "frenzy", "mult": e["mult"], "seconds": e["seconds"]}
    e = cfg.GOLDEN_EFFECTS["chain"]
    bonus = max(passive_per_hour(user["user_id"]) * e["passive_hours"],
                e["min_per_level"] * user["level"])
    add_cookies(user["user_id"], bonus)
    return {"effect": "chain", "cookies": bonus}


# ---------- комбо ----------

def current_combo(user: dict, now: float | None = None) -> float:
    """Актуальный множитель комбо: если окно истекло — уже 1 (даже до записи)."""
    now = now or time.time()
    if now - (user["combo_last_at"] or 0) > cfg.COMBO_WINDOW:
        return 1.0
    return user["combo_mult"] or 1.0


def update_combo(user: dict, clicks: int, now: float) -> float:
    """Комбо растёт, пока батчи кликов идут без пауз в хорошем темпе."""
    elapsed = now - (user["combo_last_at"] or 0)
    if elapsed <= cfg.COMBO_WINDOW and clicks / max(elapsed, 0.5) >= cfg.COMBO_MIN_CPS:
        mult = min(cfg.COMBO_MAX_MULT, current_combo(user, now) + cfg.COMBO_STEP)
    else:
        mult = 1.0
    db.update_user(user["user_id"], combo_mult=mult, combo_last_at=now)
    return mult


# ---------- престиж ----------

def prestige_state(user: dict) -> dict:
    total_pts = cfg.prestige_points(user["total_earned"])
    gain = max(0, total_pts - int(user["prestige_points"]))
    threshold = cfg.prestige_threshold(user["prestige_count"])
    return {
        "points": int(user["prestige_points"]),
        "count": user["prestige_count"],
        "multiplier": cfg.prestige_multiplier(user["prestige_points"]),
        "gain_available": gain,
        "min_earned": threshold,
        "can_prestige": gain >= 1 and user["total_earned"] >= threshold,
        "mult_per_point": cfg.PRESTIGE_MULT_PER_POINT,
    }


def do_prestige(user: dict) -> dict:
    """Сбрасывает прогресс за постоянный множитель. Возвращает {gained, points, multiplier}."""
    st = prestige_state(user)
    if not st["can_prestige"]:
        raise ValueError("err_prestige_early")
    new_points = user["prestige_points"] + st["gain_available"]
    uid = user["user_id"]
    # сохраняем: скины, ачивки, рефералов, стрик, БП сезона, покупки Stars, бусты
    db.exec("DELETE FROM board WHERE user_id = ?", (uid,))
    db.exec("DELETE FROM farm WHERE user_id = ?", (uid,))
    db.exec("DELETE FROM upgrades WHERE user_id = ?", (uid,))
    db.update_user(
        uid,
        cookies=0, click_level=1, level=1, xp=0,
        energy=cfg.max_energy(1), energy_updated_at=time.time(),
        passive_collected_at=time.time(), farm_collected_at=time.time(),
        combo_mult=1,
        prestige_points=new_points,
        prestige_count=user["prestige_count"] + 1,
    )
    return {"gained": st["gain_available"], "points": int(new_points),
            "multiplier": cfg.prestige_multiplier(new_points)}


# ---------- ферма (автофарм) ----------

def farm_counts(user_id: int) -> dict[str, int]:
    return {r["building_key"]: r["count"] for r in
            db.q("SELECT building_key, count FROM farm WHERE user_id = ?", (user_id,))}


def farm_cps(user_id: int, eff: dict | None = None) -> float:
    """Суммарный доход фермы, cookies/сек (с учётом апгрейдов и престижа)."""
    eff = eff or upgrade_effects(user_id)
    counts = farm_counts(user_id)
    base = sum(cfg.FARM_BUILDINGS[k]["cps"] * c for k, c in counts.items()
               if k in cfg.FARM_BUILDINGS)
    user = db.get_user(user_id)
    prestige = cfg.prestige_multiplier(user["prestige_points"] if user else 0)
    return base * eff["farm_mult"] * prestige


def collect_farm(user: dict) -> float:
    """Начисляет накопленный доход фермы, возвращает сколько упало."""
    now = time.time()
    seconds = min(cfg.FARM_OFFLINE_CAP_HOURS * 3600,
                  now - (user["farm_collected_at"] or now))
    db.update_user(user["user_id"], farm_collected_at=now)
    if seconds <= 0:
        return 0
    income = farm_cps(user["user_id"]) * seconds
    if income > 0:
        add_cookies(user["user_id"], income)
    return income


# ---------- XP и уровни ----------

def add_xp(user: dict, xp: float) -> dict:
    """Начисляет XP; level-up происходит на вкладке уровней (claim), тут только копим."""
    db.update_user(user["user_id"], xp=user["xp"] + xp, bp_xp=user["bp_xp"] + xp)
    return dict(user, xp=user["xp"] + xp, bp_xp=user["bp_xp"] + xp)


def claimable_level(user: dict) -> int | None:
    """Следующий уровень, если XP уже хватает."""
    nxt = user["level"] + 1
    if nxt <= cfg.MAX_LEVEL and user["xp"] >= cfg.xp_for_level(nxt):
        return nxt
    return None


# ---------- деньги ----------

def add_cookies(user_id: int, amount: float, count_earned: bool = True):
    user = db.get_user(user_id)
    fields = {"cookies": user["cookies"] + amount}
    if count_earned and amount > 0:
        fields["total_earned"] = user["total_earned"] + amount
        fields["season_earned"] = user["season_earned"] + amount
        # честный заработок кормит и дневное задание "заработай N"
        quest_progress(user_id, "earned", amount)
    db.update_user(user_id, **fields)


# ---------- пассивный доход с merge-доски ----------

def collect_passive(user: dict) -> float:
    """Начисляет накопленный пассивный доход, возвращает сколько упало."""
    now = time.time()
    hours = min(cfg.PASSIVE_CAP_HOURS, (now - (user["passive_collected_at"] or now)) / 3600)
    if hours <= 0:
        return 0
    income = passive_per_hour(user["user_id"]) * hours
    db.update_user(user["user_id"], passive_collected_at=now)
    if income > 0:
        add_cookies(user["user_id"], income)
    return income


def hourly_income(user_id: int) -> float:
    """Оценка часового дохода игрока для масштабируемых наград:
    ферма + пассивка доски + скромная оценка кликов (5 мин активного тапа)."""
    user = db.get_user(user_id)
    clicks_estimate = (cfg.click_power(user["click_level"])
                       * click_multiplier(user_id) * 5 * 60)
    return farm_cps(user_id) * 3600 + passive_per_hour(user_id) + clicks_estimate


def passive_per_hour(user_id: int) -> float:
    rows = db.q("SELECT item_level FROM board WHERE user_id = ?", (user_id,))
    base = sum(cfg.passive_income_per_hour(r["item_level"]) for r in rows)
    user = db.get_user(user_id)
    prestige = cfg.prestige_multiplier(user["prestige_points"] if user else 0)
    return base * upgrade_effects(user_id)["passive_mult"] * prestige


# ---------- достижения ----------

def achievements_state(user: dict, lang: str = "en") -> list[dict]:
    from server.i18n import tr
    user_id = user["user_id"]
    refs = db.q1("SELECT COUNT(*) c FROM referrals WHERE referrer_id = ?", (user_id,))["c"]
    claimed = {r["key"] for r in db.q(
        "SELECT key FROM achievements WHERE user_id = ? AND claimed = 1", (user_id,))}
    out = []
    for key, (_title, _desc, field, goal, reward) in cfg.ACHIEVEMENTS.items():
        progress = refs if field == "_refs" else user.get(field, 0)
        out.append({
            "key": key,
            "title": tr(lang, f"ach_{key}_t"),
            "desc": tr(lang, f"ach_{key}_d"),
            "progress": min(progress, goal), "goal": goal, "reward": reward,
            "done": progress >= goal, "claimed": key in claimed,
        })
    return out


def claim_achievement(user: dict, key: str) -> float:
    """Возвращает награду или кидает ValueError."""
    for a in achievements_state(user):
        if a["key"] == key:
            if not a["done"]:
                raise ValueError("err_not_done")
            if a["claimed"]:
                raise ValueError("err_claimed")
            db.exec("INSERT INTO achievements (user_id, key, claimed) VALUES (?, ?, 1)",
                    (user["user_id"], key))
            add_cookies(user["user_id"], a["reward"], count_earned=False)
            return a["reward"]
    raise ValueError("err_no_item")


# ---------- профиль целиком (для фронта) ----------

def full_state(user_id: int) -> dict:
    user = db.get_user(user_id)
    user = refresh_energy(user)
    db.update_user(user_id, last_seen_at=time.time())
    board = db.q("SELECT cell, item_level FROM board WHERE user_id = ? ORDER BY cell", (user_id,))
    items_count = len(board)
    nxt = user["level"] + 1
    eff = upgrade_effects(user_id)
    owned_skins = {r["skin_key"] for r in
                   db.q("SELECT skin_key FROM skins WHERE user_id = ?", (user_id,))}
    owned_skins.add("classic")
    return {
        "user": {
            "user_id": user["user_id"],
            "username": user["username"],
            "first_name": user["first_name"],
            "cookies": user["cookies"],
            "level": user["level"],
            "xp": user["xp"],
            "xp_next": cfg.xp_for_level(nxt) if nxt <= cfg.MAX_LEVEL else None,
            "energy": user["energy"],
            "max_energy": energy_cap(user, eff),
            "click_level": user["click_level"],
            "click_power": cfg.click_power(user["click_level"]) * click_multiplier(user_id),
            "click_upgrade_cost": cfg.click_upgrade_cost(user["click_level"]),
            "total_clicks": user["total_clicks"],
            "total_merges": user["total_merges"],
            "bp_xp": user["bp_xp"],
            "bp_premium": bool(user["bp_premium"]),
            "active_skin": user["active_skin"] or "classic",
            "skin_emoji": skin_emoji(user["active_skin"] or "classic"),
        },
        "season": {
            "id": current_season(),
            "ends_at": season_end_ts(current_season()),
        },
        "daily": daily_state(user),
        "quests_claimable": claimable_quests_count(user_id),
        "golden": golden_state(user),
        "combo": {"mult": current_combo(user),
                  "max_mult": cfg.COMBO_MAX_MULT},
        "prestige": prestige_state(user),
        "farm": {
            "buildings": farm_counts(user_id),
            "cps": farm_cps(user_id, eff),
        },
        "upgrades_owned": sorted(user_upgrades(user_id)),
        "skins_owned": sorted(owned_skins),
        "board": board,
        "spawn_cost": cfg.spawn_cost(items_count),
        # прямая покупка печенек выше 1 lvl: доступные уровни и цены
        "spawn_direct": {
            "max_level": max(1, max(
                (l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                 if cfg.item_unlock_level(l) <= user["level"]), default=1)
                - cfg.SPAWN_DIRECT_GAP),
            "costs": {str(l): cfg.direct_spawn_cost(l, items_count)
                      for l in range(1, cfg.MAX_ITEM_LEVEL + 1)},
        },
        "passive_per_hour": passive_per_hour(user_id),
        "boosts": [
            {"key": r["boost_key"], "expires_at": r["expires_at"]}
            for r in db.q("SELECT boost_key, expires_at FROM boosts "
                          "WHERE user_id = ? AND expires_at > ?", (user_id, time.time()))
        ],
        "claimable_level": claimable_level(user),
        "max_item_unlocked": max(
            (lvl for lvl in range(1, cfg.MAX_ITEM_LEVEL + 1)
             if cfg.item_unlock_level(lvl) <= user["level"]), default=1),
    }
