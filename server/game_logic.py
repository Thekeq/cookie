"""Общая игровая логика поверх БД. Сервер — единственный источник правды."""
import datetime
import json
import random
import time

from server import economy
from server import game_config as cfg

# один экземпляр базы на процесс: книга операций обязана писаться в ТОЙ ЖЕ
# транзакции, что и само движение денег, а значит и через то же соединение
db = economy.db


# ---------- лимитер запросов ----------
# Живёт в памяти процесса: игра работает одним процессом (бот + API + notifier
# в общем event loop), внешнего Redis нет и заводить его ради этого не стоит.
# Смысл — не пустить перебор промокодов и не дать одному игроку выжечь тяжёлые
# ручки: /api/state делает под сотню SQL-запросов, а SQLite синхронный и
# блокирует весь процесс, включая поллинг бота.
_rate_buckets: dict[tuple[int, str], list[float]] = {}
_RATE_GC_EVERY = 500
_rate_calls = 0


def check_rate_limit(user_id: int, bucket: str, limit: int, window: float):
    """Кидает HTTP 429, если за window секунд было больше limit обращений."""
    global _rate_calls
    from fastapi import HTTPException
    now = time.time()
    key = (user_id, bucket)
    hits = [t for t in _rate_buckets.get(key, ()) if now - t < window]
    if len(hits) >= limit:
        hits.append(now)
        _rate_buckets[key] = hits
        raise HTTPException(429, "err_too_fast")
    hits.append(now)
    _rate_buckets[key] = hits

    # периодическая уборка: без неё словарь растёт по одному ключу на игрока
    _rate_calls += 1
    if _rate_calls % _RATE_GC_EVERY == 0:
        for k, ts in list(_rate_buckets.items()):
            if not ts or now - ts[-1] > 3600:
                _rate_buckets.pop(k, None)


# ---------- аналитика ----------

def track(user_id: int, event: str, value: float = 0):
    """Пишет событие аналитики. Одна вставка, никогда не роняет игровой код."""
    try:
        db.exec("INSERT INTO events (user_id, event, value, created_at) "
                "VALUES (?, ?, ?, ?)", (user_id, event, value, time.time()))
    except Exception:
        pass


# ---------- сезоны ----------

def current_season(now: float | None = None) -> int:
    now = now or time.time()
    return max(0, int((now - cfg.SEASON_EPOCH) // (cfg.SEASON_LENGTH_DAYS * 86400)))


def season_end_ts(season: int) -> float:
    return cfg.SEASON_EPOCH + (season + 1) * cfg.SEASON_LENGTH_DAYS * 86400


def league_brackets() -> list[tuple[str, int, int | None]]:
    """[(ключ, мин. уровень, макс. уровень или None)] по конфигу LEAGUES."""
    out = []
    for i, (key, lo) in enumerate(cfg.LEAGUES):
        hi = cfg.LEAGUES[i + 1][1] - 1 if i + 1 < len(cfg.LEAGUES) else None
        out.append((key, lo, hi))
    return out


SEASON_RESET_CHUNK = 500        # юзеров за один проход ролловера


_SEASON_RESET_SQL = (
    "UPDATE users SET season_id = ?, season_earned = 0, bp_xp = 0, "
    "bp_premium = bp_premium_next, bp_premium_next = 0, "
    "bp_claimed_free = '[]', bp_claimed_premium = '[]' ")


def _ensure_season_snapshot(season: int):
    """Снапшот топа сезона и выплата призов — ровно один раз.

    Маркер — наличие строк season_results: без него после частичного сброса
    победители пересчитались бы по остатку. add_cookies(count_earned=False)
    не трогает season_earned, поэтому платить можно до сброса."""
    if db.q1("SELECT 1 x FROM season_results WHERE season_id = ? LIMIT 1", (season,)):
        return
    with db.tx():
        now = time.time()
        for uid, rank, earned in _season_winners(season):
            reward = cfg.season_reward(rank, earned)
            db.exec(
                "INSERT OR IGNORE INTO season_results (season_id, user_id, "
                "rank, earned, reward_cookies, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (season, uid, rank, earned, reward, now))
            if reward:
                add_cookies(uid, reward, count_earned=False)


def ensure_user_season(user_id: int) -> dict | None:
    """Приводит сезонные поля ОДНОГО игрока к текущему сезону.

    Это дешёвая замена finalize_seasons в горячем пути: тот делал
    SELECT DISTINCT season_id по всей таблице users на каждый /api/state,
    /api/auth, /api/battlepass и /api/leaderboard, а в момент смены сезона
    ещё и запускал пакетный UPDATE на 500 строк с каждого запроса. Массовый
    сброс теперь идёт по таймеру в notifier; здесь — только своя строка."""
    cur = current_season()
    user = db.get_user(user_id)
    if not user or user["season_id"] >= cur:
        return user
    _ensure_season_snapshot(user["season_id"])
    with db.tx():
        db.exec(_SEASON_RESET_SQL + "WHERE user_id = ? AND season_id < ?",
                (cur, user_id, cur))
    return db.get_user(user_id)


def finalize_seasons():
    """Массовый ролловер: снапшот топа прошлого сезона, призы и сброс
    сезонного прогресса порциями. Зовётся по таймеру из notifier — один
    UPDATE на всех держал бы write-lock на большой базе.

    Премиум переезжает в новый сезон только через bp_premium_next (см.
    grant_bp_premium: он ставит флаг и для покупок, совершённых уже ПОСЛЕ
    смены сезона, пока сброс не дошёл до этой строки)."""
    cur = current_season()
    stale = db.q("SELECT DISTINCT season_id s FROM users WHERE season_id < ?", (cur,))
    for row in stale:
        season = row["s"]
        _ensure_season_snapshot(season)
        with db.tx():
            db.exec(
                _SEASON_RESET_SQL +
                "WHERE user_id IN (SELECT user_id FROM users "
                "                  WHERE season_id = ? LIMIT ?)",
                (cur, season, SEASON_RESET_CHUNK))


def _season_winners(season: int) -> list[tuple[int, int, float]]:
    """[(user_id, место, заработано)] — топ-10 в КАЖДОЙ лиге: новички
    соревнуются с новичками. Место — по заработку ЗА СЕЗОН (он и сбрасывается),
    уровень только тай-брейк: раньше сортировка по уровню делала таблицу
    статичной, ведь уровень не обнуляется вместе с сезоном."""
    winners = []
    for _key, lo, hi in league_brackets():
        cond = "level >= ?" + (" AND level <= ?" if hi is not None else "")
        params = [season, lo] + ([hi] if hi is not None else [])
        top = db.q(
            f"SELECT user_id, season_earned FROM users "
            f"WHERE season_id = ? AND season_earned > 0 AND {cond} "
            f"ORDER BY season_earned DESC, level DESC LIMIT 10", params)
        winners += [(u["user_id"], i + 1, u["season_earned"])
                    for i, u in enumerate(top)]
    return winners


def grant_bp_premium(user_id: int, now: float | None = None):
    """Выдаёт premium-пасс за 100⭐. Флаг переноса на следующий сезон ставится
    в двух случаях, и оба — про то, чтобы покупка не сгорела:

    1) до конца сезона осталось меньше BP_PREMIUM_GRACE_DAYS — иначе покупка
       накануне ролловера обнулялась бы через несколько часов;
    2) сезон УЖЕ сменился, но пакетный сброс (порциями по 500) ещё не дошёл
       до этой строки — без флага ближайший чужой запрос прогнал бы её чанк
       и стёр только что оплаченный товар. Довыдачи в этом случае не будет:
       покупка уже помечена 'fulfilled'."""
    now = now or time.time()
    fields = {"bp_premium": 1}
    user = db.get_user(user_id)
    rollover_pending = bool(user) and user["season_id"] < current_season()
    if (season_end_ts(current_season()) - now <= cfg.BP_PREMIUM_GRACE_DAYS * 86400
            or rollover_pending):
        fields["bp_premium_next"] = 1
    db.update_user(user_id, **fields)


def my_last_season_result(user_id: int) -> dict | None:
    return db.q1(
        "SELECT season_id, rank, earned, reward_cookies FROM season_results "
        "WHERE user_id = ? ORDER BY season_id DESC LIMIT 1", (user_id,))


# ---------- ежедневная награда (стрик) ----------

def _utc_day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _iso_week(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%G-W%V")


def _freeze_available(user: dict, now: float) -> bool:
    """Заморозка стрика: раз в неделю пропуск ровно одного дня не сжигает стрик."""
    return user["daily_streak"] > 0 and user.get("streak_freeze_week") != _iso_week(now)


def daily_state(user: dict) -> dict:
    now = time.time()
    today = _utc_day(now)
    last_day = _utc_day(user["daily_claimed_at"]) if user["daily_claimed_at"] else ""
    yesterday = _utc_day(now - 86400)
    day_before = _utc_day(now - 2 * 86400)
    can_claim = last_day != today
    # если пропустил день — стрик сгорел и следующий клейм начнёт с 1;
    # НО раз в неделю пропуск ровно одного дня прощается (заморозка);
    # если уже забрал сегодня — «следующий» это завтрашний (стрик+1)
    if can_claim:
        if last_day == yesterday or (last_day == day_before and _freeze_available(user, now)):
            next_streak = user["daily_streak"] + 1
        else:
            next_streak = 1
    else:
        next_streak = user["daily_streak"] + 1
    return {
        "can_claim": can_claim,
        "streak": user["daily_streak"],
        "next_streak": next_streak,
        "next_reward": cfg.scaled_reward(cfg.daily_reward(next_streak), "daily",
                                         hourly_income(user["user_id"])),
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
    day_before = _utc_day(now - 2 * 86400)
    freeze_used = False
    if last_day == yesterday:
        streak = user["daily_streak"] + 1
    elif last_day == day_before and _freeze_available(user, now):
        # заморозка: пропущен ровно один день — стрик выживает (раз в неделю)
        streak = user["daily_streak"] + 1
        freeze_used = True
    else:
        streak = 1
    reward = cfg.scaled_reward(cfg.daily_reward(streak), "daily",
                               hourly_income(user["user_id"]))
    extra = {"streak_freeze_week": _iso_week(now)} if freeze_used else {}
    with db.tx():  # отметка о получении и деньги — одним куском
        db.update_user(user["user_id"], daily_streak=streak, daily_claimed_at=now, **extra)
        add_cookies(user["user_id"], reward, count_earned=False)
    return {"streak": streak, "reward": reward, "freeze_used": freeze_used}


# ---------- ежедневные задания ----------

_quest_keys_cache: dict[str, list[str]] = {}
# (user_id, day), для которых строки заданий уже созданы в этом процессе —
# quest_progress вызывается на каждое начисление печенек, а INSERT OR IGNORE
# на три задания в горячем пути стоил дороже самого начисления
_quest_rows_ready: set[tuple[int, str]] = set()


def todays_quest_keys(day: str | None = None) -> list[str]:
    """Детерминированный выбор заданий дня — одинаковый для всех игроков."""
    day = day or _utc_day(time.time())
    if day not in _quest_keys_cache:
        _quest_keys_cache.clear()  # держим только сегодняшний день
        _quest_keys_cache[day] = random.Random(day).sample(
            sorted(cfg.DAILY_QUEST_POOL), cfg.DAILY_QUESTS_PER_DAY)
    return _quest_keys_cache[day]


def _ensure_quest_rows(user_id: int, day: str, keys: list[str]):
    for key in keys:
        db.exec("INSERT OR IGNORE INTO daily_quests (user_id, day, quest_key) "
                "VALUES (?, ?, ?)", (user_id, day, key))
    if len(_quest_rows_ready) > 20_000:
        _quest_rows_ready.clear()
    # Кеш ставим ТОЛЬКО если вставка уже зафиксирована. Внутри чужой открытой
    # транзакции откат удалил бы строки, а кеш продолжал бы врать, что они
    # есть, и quest_progress молча обновлял бы ноль строк.
    if not db._tx_depth:
        _quest_rows_ready.add((user_id, day))


def quest_reward_cookies(user_id: int, quest_key: str,
                         income: float | None = None) -> float:
    """Награда квеста: базовая сумма ИЛИ доля дохода игрока — что больше.
    Доля своя у каждого задания: спавн- и мердж-квесты тратят печеньки и
    обязаны отбивать затраты, кликовые бесплатны и платят меньше."""
    q = cfg.DAILY_QUEST_POOL.get(quest_key)
    if not q:
        return 0.0
    income = hourly_income(user_id) if income is None else income
    return max(q["reward_cookies"], income * q.get("reward_hours", 0.5))


def _user_quest_rows(user_id: int, day: str) -> list[dict]:
    """Строки заданий юзера за день (после реролла могут отличаться от глобальных)."""
    _ensure_quest_rows(user_id, day, todays_quest_keys(day))
    return db.q("SELECT id, quest_key, progress, claimed FROM daily_quests "
                "WHERE user_id = ? AND day = ? ORDER BY id", (user_id, day))


def quests_state(user_id: int) -> list[dict]:
    day = _utc_day(time.time())
    # доход считаем ОДИН раз на все задания: раньше quest_reward_cookies
    # дёргал hourly_income на каждое из трёх (12 запросов x 3) и это была
    # треть от 79 SQL-запросов, которые делал один /api/state
    income = hourly_income(user_id)
    out = []
    for r in _user_quest_rows(user_id, day):
        q = cfg.DAILY_QUEST_POOL.get(r["quest_key"])
        if not q:
            continue
        goal = cfg.quest_goal(r["quest_key"], income)
        out.append({
            "key": r["quest_key"], "metric": q["metric"], "goal": goal,
            "reward_cookies": quest_reward_cookies(user_id, r["quest_key"], income),
            "reward_bp_xp": q["reward_bp_xp"],
            "progress": min(r["progress"], goal),
            "done": r["progress"] >= goal, "claimed": bool(r["claimed"]),
        })
    return out


def quest_progress(user_id: int, metric: str, amount: float):
    """Инкремент прогресса заданий дня с данной метрикой.

    Горячий путь (зовётся из add_cookies на каждое начисление) — один UPDATE:
    строки заданий создаём максимум раз за день на процесс, а не каждый раз."""
    if amount <= 0:
        return
    day = _utc_day(time.time())
    keys = [k for k in todays_quest_keys(day)
            if cfg.DAILY_QUEST_POOL[k]["metric"] == metric]
    if not keys:
        return
    if (user_id, day) not in _quest_rows_ready:
        _ensure_quest_rows(user_id, day, todays_quest_keys(day))
    holes = ", ".join("?" * len(keys))
    db.exec(f"UPDATE daily_quests SET progress = progress + ? "
            f"WHERE user_id = ? AND day = ? AND claimed = 0 AND quest_key IN ({holes})",
            (amount, user_id, day, *keys))


def reroll_quest(user: dict, key: str) -> str:
    """Бесплатный реролл одного (не выполненного) задания раз в день.
    Возвращает ключ нового задания."""
    now = time.time()
    day = _utc_day(now)
    if user["quest_reroll_day"] == day:
        raise ValueError("err_no_reroll")
    rows = _user_quest_rows(user["user_id"], day)
    row = next((r for r in rows if r["quest_key"] == key), None)
    if not row:
        raise ValueError("err_no_quest")
    if row["claimed"]:
        raise ValueError("err_claimed")
    current = {r["quest_key"] for r in rows}
    candidates = sorted(k for k in cfg.DAILY_QUEST_POOL if k not in current)
    if not candidates:
        raise ValueError("err_no_reroll")
    new_key = random.choice(candidates)
    with db.tx():
        db.exec("DELETE FROM daily_quests WHERE id = ?", (row["id"],))
        db.exec("INSERT OR IGNORE INTO daily_quests (user_id, day, quest_key) "
                "VALUES (?, ?, ?)", (user["user_id"], day, new_key))
        db.update_user(user["user_id"], quest_reroll_day=day)
    track(user["user_id"], "quest_reroll")
    return new_key


def bp_catchup_mult(user: dict, now: float | None = None) -> float:
    """x2 BP XP, если игрок отстаёт от темпа сезона: обычный игрок должен
    успевать закрыть пасс, иначе он перестаёт пытаться."""
    now = now or time.time()
    total = cfg.SEASON_LENGTH_DAYS * 86400
    elapsed = 1 - max(0.0, (season_end_ts(current_season()) - now)) / total
    expected = cfg.BP_MAX_LEVEL * elapsed
    if cfg.bp_level_for_xp(user["bp_xp"]) < expected - cfg.BP_CATCHUP_LAG:
        return cfg.BP_CATCHUP_MULT
    return 1.0


def claim_quest(user: dict, key: str) -> dict:
    """Возвращает награду задания или кидает ValueError."""
    day = _utc_day(time.time())
    q = cfg.DAILY_QUEST_POOL.get(key)
    if not q:
        raise ValueError("err_no_quest")
    row = db.q1("SELECT * FROM daily_quests WHERE user_id = ? AND day = ? AND quest_key = ?",
                (user["user_id"], day, key))
    income = hourly_income(user["user_id"])
    if not row or row["progress"] < cfg.quest_goal(key, income):
        raise ValueError("err_not_done")
    if row["claimed"]:
        raise ValueError("err_claimed")
    reward = quest_reward_cookies(user["user_id"], key, income)
    bp_xp = int(q["reward_bp_xp"] * bp_catchup_mult(user))
    with db.tx():  # отметка + печеньки + BP XP — одним куском
        # условный UPDATE: параллельный клейм не выдаст награду дважды
        if db.exec("UPDATE daily_quests SET claimed = 1 WHERE id = ? AND claimed = 0",
                   (row["id"],)) == 0:
            raise ValueError("err_claimed")
        add_cookies(user["user_id"], reward, count_earned=False)
        add_xp(user["user_id"], 0, bp_xp)
    return {"reward_cookies": reward, "reward_bp_xp": bp_xp}


def claimable_quests_count(user_id: int) -> int:
    return sum(1 for q in quests_state(user_id) if q["done"] and not q["claimed"])


# ---------- milestone-награды рефералки ----------

def ref_milestones_state(user_id: int) -> list[dict]:
    # считаем только «живых» рефералов: майлстоуны раздают буст, эксклюзивный
    # скин и Premium Пасс — за пустые аккаунты это прямой денежный эквивалент
    refs = ref_count(user_id)
    claimed = {r["milestone_key"] for r in
               db.q("SELECT milestone_key FROM ref_claims WHERE user_id = ?", (user_id,))}
    out = []
    for key, ms in cfg.REF_MILESTONES.items():
        out.append({
            "key": key, "count": ms["count"], "type": ms["type"],
            "progress": min(refs, ms["count"]),
            "done": refs >= ms["count"], "claimed": key in claimed,
            "qualify_level": cfg.REF_QUALIFY_LEVEL,
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
    with db.tx():  # отметка и награда — одним куском
        # INSERT OR IGNORE + rowcount: проверка claimed выше живёт вне
        # транзакции, и два параллельных клейма прошли бы её оба
        if db.exec("INSERT OR IGNORE INTO ref_claims (user_id, milestone_key, claimed_at) "
                   "VALUES (?, ?, ?)", (user["user_id"], key, time.time())) == 0:
            raise ValueError("err_claimed")
        if ms["type"] == "boost":
            db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                    (user["user_id"], "click_x2", time.time() + ms["hours"] * 3600))
        elif ms["type"] == "skin":
            db.exec("INSERT OR IGNORE INTO skins (user_id, skin_key) VALUES (?, ?)",
                    (user["user_id"], ms["skin"]))
        elif ms["type"] == "bp_premium":
            grant_bp_premium(user["user_id"])
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


def permanent_click_multiplier(user_id: int) -> float:
    """Множитель клика БЕЗ временных бустов: апгрейды, престиж, коллекция.
    Используется для оценки дохода (цены, награды) — иначе френзи x7 на 25 сек
    раздувал бы награду заказа и цену спавна на всю сессию."""
    user = db.get_user(user_id)
    return (upgrade_effects(user_id)["click_mult"]
            * cfg.prestige_multiplier(user["prestige_points"] if user else 0)
            * collection_multiplier(user_id))


def click_multiplier(user_id: int) -> float:
    mult = permanent_click_multiplier(user_id)
    boosts = active_boosts(user_id)
    if "click_x2" in boosts:
        mult *= cfg.BOOST_CLICK_X2_MULT
    if "golden_frenzy" in boosts:
        mult *= cfg.GOLDEN_EFFECTS["frenzy"]["mult"]
    # Ивент выходных умножает активный доход. Сознательно НЕ попадает в
    # permanent_click_multiplier: тот кормит hourly_income, а через него —
    # награды заказов и цены доски. Ровно так золотая печенька когда-то
    # раздувала стоимость сундуков в семь раз.
    return mult * event_multiplier()


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
    if effect == "frenzy":
        e = cfg.GOLDEN_EFFECTS["frenzy"]
        with db.tx():
            db.update_user(user["user_id"], golden_expires_at=0)
            db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                    (user["user_id"], "golden_frenzy", now + e["seconds"]))
        return {"effect": "frenzy", "mult": e["mult"], "seconds": e["seconds"]}
    e = cfg.GOLDEN_EFFECTS["chain"]
    bonus = max(passive_per_hour(user["user_id"]) * e["passive_hours"],
                e["min_per_level"] * user["level"])
    with db.tx():
        db.update_user(user["user_id"], golden_expires_at=0)
        add_cookies(user["user_id"], bonus)
    # ключ "bonus", а не "cookies": роутер добавляет к ответу баланс под
    # ключом "cookies" и раньше затирал им сам бонус
    return {"effect": "chain", "bonus": bonus}


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
        # что останется после перерождения — видно ДО нажатия кнопки
        "kept_level": prestige_kept_level(user["level"]),
        "next_multiplier": cfg.prestige_multiplier(
            user["prestige_points"] + gain),
    }


def prestige_kept_level(level: int) -> int:
    """Какой уровень остаётся после престижа (не ниже 1)."""
    return max(1, int(level * cfg.PRESTIGE_KEEP_LEVEL_SHARE))


def do_prestige(user: dict) -> dict:
    """Сбрасывает прогресс за постоянный множитель. Возвращает {gained, points, multiplier}."""
    st = prestige_state(user)
    if not st["can_prestige"]:
        raise ValueError("err_prestige_early")
    new_points = user["prestige_points"] + st["gain_available"]
    uid = user["user_id"]
    # Уровень сохраняется частично. Полный откат на 1-й означал заново
    # проходить все req_level зданий и предметов, а множитель престижа этого
    # не ускорял — перерождаться было невыгодно ни в какой момент.
    kept_level = prestige_kept_level(user["level"])
    # сохраняем: скины, ачивки, рефералов, стрик, БП сезона, покупки Stars, бусты.
    # Сброс и начисление очков — одна транзакция: полустёртого профиля не бывает
    with db.tx():
        db.exec("DELETE FROM board WHERE user_id = ?", (uid,))
        db.exec("DELETE FROM farm WHERE user_id = ?", (uid,))
        db.exec("DELETE FROM upgrades WHERE user_id = ?", (uid,))
        # незавершённые заказы выписаны под старый доход: цель «заработай 60M»
        # недостижима на 1 уровне, а награда по ней была бы читом
        db.exec("DELETE FROM orders WHERE user_id = ? AND status != 'done'", (uid,))
        db.update_user(
            uid,
            cookies=0, click_level=1,
            level=kept_level, xp=cfg.xp_for_level(kept_level),
            energy=cfg.max_energy(kept_level), energy_updated_at=time.time(),
            passive_collected_at=time.time(), farm_collected_at=time.time(),
            combo_mult=1,
            prestige_points=new_points,
            prestige_count=user["prestige_count"] + 1,
        )
    return {"gained": st["gain_available"], "points": int(new_points),
            "kept_level": kept_level,
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
    return base * eff["farm_mult"] * prestige * collection_multiplier(user_id)


def collect_all(user_id: int) -> dict:
    """Собирает ферму + пассивку доски и возвращает СВЕЖЕГО юзера.
    Обязателен перед любой проверкой «хватает ли печенек»: иначе сервер
    сравнивает цену со вчерашним балансом, а игрок видит уже натикавший —
    «деньги есть, а купить не даёт»."""
    u = db.get_user(user_id)
    collect_passive(u)
    collect_farm(db.get_user(user_id))
    return db.get_user(user_id)


def offline_bonus_hours(user: dict) -> float:
    """Постоянная добавка к оффлайн-капу из Stars-покупки offline_cap_*."""
    return user.get("offline_bonus_hours") or 0


# ---------- оффлайн-рецепты ----------

def recipe_status(user: dict, now: float | None = None) -> dict:
    """Состояние поставленной закваски: сколько прошло, готово ли, множитель.

    Оффлайн-кап был чистым штрафом за то, что игрок не в игре. Рецепт делает
    из возвращения событие: у теста есть своё время готовности и окно, в
    которое надо успеть."""
    key = user.get("recipe_key") or ""
    if key not in cfg.RECIPES:
        return {"key": None, "state": "none", "mult": 1.0}
    now = now or time.time()
    started = user.get("recipe_started_at") or now
    elapsed_h = max(0.0, (now - started) / 3600)
    state, mult = cfg.recipe_state(key, elapsed_h)
    r = cfg.RECIPES[key]
    return {
        "key": key,
        "state": state,
        "mult": mult,
        "elapsed_h": elapsed_h,
        "ready_at": started + r["hours"] * 3600,
        "spoils_at": started + r["hours"] * r["window"] * 3600,
    }


def recipes_available(user: dict) -> list[dict]:
    out = []
    for key, r in cfg.RECIPES.items():
        need = cfg.RECIPE_REQ_LEVEL.get(key, 1)
        out.append({"key": key, "hours": r["hours"], "mult": r["mult"],
                    # округляем: 6.0 * 1.6 в double даёт 9.600000000000001,
                    # и это число уходило прямо в интерфейс
                    "window_h": round(r["hours"] * r["window"], 1),
                    "req_level": need, "unlocked": user["level"] >= need})
    return out


def set_recipe(user: dict, key: str) -> dict:
    """Поставить закваску. Смена рецепта сбрасывает таймер — иначе можно было
    бы дождаться готовности дешёвого и «переобуться» в дорогой."""
    if key not in cfg.RECIPES:
        raise ValueError("err_no_item")
    if user["level"] < cfg.RECIPE_REQ_LEVEL.get(key, 1):
        raise ValueError(f"err_req_level|{cfg.RECIPE_REQ_LEVEL[key]}")
    db.update_user(user["user_id"], recipe_key=key, recipe_started_at=time.time())
    return recipe_status(db.get_user(user["user_id"]))


def _consume_recipe(user: dict) -> float:
    """Забирает множитель закваски и снимает её (одноразовая).
    Возвращает множитель: 1.0, если рано или подгорело."""
    st = recipe_status(user)
    if st["state"] == "none":
        return 1.0
    if st["state"] == "rising":
        return 1.0          # рано вернулся — тесто ещё стоит, не тратим
    db.update_user(user["user_id"], recipe_key=None, recipe_started_at=0)
    return st["mult"]


# ---------- выходные-ивенты ----------

def active_event(now: float | None = None) -> dict | None:
    """Ивент выходных. Детерминирован от календаря — как и номер сезона,
    поэтому не нужны ни таблица, ни фоновая задача, ни ручное включение."""
    now = now or time.time()
    dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    if dt.weekday() not in cfg.EVENT_WEEKDAYS:
        return None
    keys = sorted(cfg.EVENTS)
    # неделя года выбирает ивент: подряд идущие выходные не повторяются
    key = keys[dt.isocalendar()[1] % len(keys)]
    ev = cfg.EVENTS[key]
    # окно = все дни EVENT_WEEKDAYS этой недели, до конца последнего
    start = dt - datetime.timedelta(days=dt.weekday() - min(cfg.EVENT_WEEKDAYS))
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + datetime.timedelta(days=len(cfg.EVENT_WEEKDAYS))
    return {"key": key, "mult": ev["mult"], "title_key": ev["title_key"],
            "started_at": start.timestamp(), "ends_at": end.timestamp()}


def event_multiplier(now: float | None = None) -> float:
    ev = active_event(now)
    return ev["mult"] if ev else 1.0


def farm_offline_cap_hours(user: dict) -> float:
    return cfg.FARM_OFFLINE_CAP_HOURS + offline_bonus_hours(user)


def passive_offline_cap_hours(user: dict) -> float:
    return cfg.PASSIVE_CAP_HOURS + offline_bonus_hours(user)


def collect_farm(user: dict) -> float:
    """Начисляет накопленный доход фермы, возвращает сколько упало.
    Таймер и деньги — одна транзакция: сбой не может «продвинуть» таймер,
    потеряв недоначисленный доход."""
    now = time.time()
    seconds = min(farm_offline_cap_hours(user) * 3600,
                  now - (user["farm_collected_at"] or now))
    if seconds <= 0:
        db.update_user(user["user_id"], farm_collected_at=now)
        return 0
    income = farm_cps(user["user_id"]) * seconds
    # закваска умножает ТОЛЬКО оффлайн-доход и только если игрок вернулся в
    # окно готовности; съедается один раз — на ферме, а не на каждом сборе
    if seconds > 60:
        income *= _consume_recipe(user)
    with db.tx():
        db.update_user(user["user_id"], farm_collected_at=now)
        if income > 0:
            add_cookies(user["user_id"], income)
    return income


# ---------- XP и уровни ----------

def add_xp(user_id: int, xp: float, bp_xp: float | None = None) -> dict:
    """Начисляет XP; level-up происходит на вкладке уровней (claim), тут только
    копим. bp_xp можно задать отдельно: у мерджа он ограничен капом, иначе одно
    топ-слияние закрывало бы весь сезонный пасс.

    И сложение, и развилка «лить XP или переливать в пасс» выполняются в базе.
    Раньше обе считались из прочитанного заранее словаря: два мерджа подряд —
    а на активной игре они идут пачками — читали одно и то же значение и второй
    затирал первый. Пропадал не мусор, а основной XP игры."""
    xp = economy._sane(xp, "add_xp.xp")
    bp_xp = xp if bp_xp is None else economy._sane(bp_xp, "add_xp.bp_xp")
    if not xp and not bp_xp:
        return db.get_user(user_id)
    op = economy.auto_op(user_id, "xp_gain")
    with db.tx():
        # на потолке уровней XP копился в пустоту (мердж 24 lvl давал 58 208 XP
        # в никуда) — переливаем его в батл-пасс, там прогресс продолжается
        row = db.q1w(
            "UPDATE users SET "
            "xp = xp + CASE WHEN level >= ? AND ? > 0 THEN 0 ELSE ? END, "
            "bp_xp = bp_xp + ? + CASE WHEN level >= ? AND ? > 0 "
            f"THEN {db.LEAST}(? * ?, ?) ELSE 0 END, "
            "user_revision = user_revision + 1 "
            "WHERE user_id = ? RETURNING xp, bp_xp, level, season_id",
            (cfg.MAX_LEVEL, xp, xp, bp_xp, cfg.MAX_LEVEL, xp,
             xp, cfg.MAXLEVEL_XP_TO_BP, cfg.MERGE_BP_XP_CAP, user_id))
        if row is None:
            raise ValueError("err_no_user")
        # ту же развилку повторяем для книги — но не отдельным чтением, а по
        # level из этого же RETURNING: сам statement его не трогает, значит это
        # ровно то значение, на котором сработал CASE
        if row["level"] >= cfg.MAX_LEVEL and xp > 0:
            moved_xp = 0.0
            moved_bp = bp_xp + min(xp * cfg.MAXLEVEL_XP_TO_BP, cfg.MERGE_BP_XP_CAP)
        else:
            moved_xp, moved_bp = xp, bp_xp
        if moved_xp:
            economy.record(user_id, "xp", moved_xp, "xp_gain", row["xp"], op,
                           season_id=row["season_id"])
        if moved_bp:
            economy.record(user_id, "bp_xp", moved_bp, "xp_gain", row["bp_xp"], op,
                           seq=1, season_id=row["season_id"])
    return row


def level_reward_scaled(level: int, income: float) -> dict:
    """Награда за уровень с поправкой на доход: константа 150*lvl^1.5 давала
    на 30-м уровне 24 647 печенек — секунды дохода на этой стадии."""
    r = dict(cfg.level_reward(level))
    r["cookies"] = cfg.scaled_reward(r["cookies"], "level", income)
    return r


def claimable_level(user: dict) -> int | None:
    """Следующий уровень, если XP уже хватает."""
    nxt = user["level"] + 1
    if nxt <= cfg.MAX_LEVEL and user["xp"] >= cfg.xp_for_level(nxt):
        return nxt
    return None


# ---------- деньги ----------

class NoFunds(ValueError):
    """Денег не хватило. Отдельный тип, потому что это не ошибка — это отказ,
    и обработчик обязан отличать его от поломки."""


def add_cookies(user_id: int, amount: float, count_earned: bool = True,
                *, operation_id: str | None = None, reason: str = "unspecified",
                ref_type: str | None = None, ref_id: str | None = None) -> float:
    """Атомарное начисление. Возвращает НОВЫЙ баланс.

    Раньше было «прочитать, сложить в питоне, записать». Пока процесс один и
    между чтением и записью нет await, это работает. Как только worker'ов
    станет двое, два таких начисления затрут друг друга: оба прочитают старый
    баланс и запишут свою сумму — одно начисление исчезнет без следа. Теперь
    сложение делает сама база (`cookies = cookies + ?`), а движение попадает в
    книгу той же транзакцией.

    Клампа в ноль тут нет и раньше не было: отрицательная сумма уводит баланс
    в минус ровно так же, как уводила. Это изменение атомарности, а не правил.

    `operation_id` делает начисление идемпотентным целиком: повтор с тем же
    токеном не двигает баланс и возвращает текущий. Без токена (auto_op)
    повтор — это законное второе начисление, и защищаться от ретрая обязан
    вызывающий."""
    amount = economy._sane(amount, "add_cookies")
    earn = amount if (count_earned and amount > 0) else 0.0
    op = operation_id or economy.auto_op(user_id, reason)
    with db.tx():
        if operation_id and economy.already_recorded(operation_id, "cookies"):
            return db.get_user(user_id)["cookies"]
        row = db.q1w(
            "UPDATE users SET cookies = cookies + ?, total_earned = total_earned + ?, "
            "season_earned = season_earned + ?, user_revision = user_revision + 1 "
            "WHERE user_id = ? RETURNING cookies, season_id",
            (amount, earn, earn, user_id))
        if row is None:
            raise ValueError("err_no_user")
        economy.record(user_id, "cookies", amount, reason, row["cookies"], op,
                       ref_type=ref_type, ref_id=ref_id,
                       counts_earned=1 if earn else 0, season_id=row["season_id"])
        if earn:
            # честный заработок кормит дневное задание "заработай N" и заказ пекарни
            quest_progress(user_id, "earned", amount)
            order_progress(user_id, "earned", amount)
    return row["cookies"]


def spend_cookies(user_id: int, cost: float, reason: str, *,
                  operation_id: str | None = None,
                  ref_type: str | None = None, ref_id: str | None = None) -> float:
    """Атомарная трата. Возвращает НОВЫЙ баланс, кидает NoFunds при нехватке.

    Условие `cookies >= ?` живёт в самом UPDATE, поэтому проверка и списание —
    один шаг. Проверка отдельным SELECT'ом (как во всех шести местах покупок)
    пробивается двумя параллельными запросами: оба видят достаточный баланс и
    оба покупают.

    С `operation_id` списание идемпотентно: повтор ничего не снимает и отдаёт
    текущий баланс — важно там, где деньги берут за уже выданный товар."""
    cost = economy._sane(cost, "spend_cookies")
    op = operation_id or economy.auto_op(user_id, reason)
    with db.tx():
        if operation_id and economy.already_recorded(operation_id, "cookies"):
            return db.get_user(user_id)["cookies"]
        row = db.q1w("UPDATE users SET cookies = cookies - ?, "
                     "user_revision = user_revision + 1 "
                     "WHERE user_id = ? AND cookies >= ? RETURNING cookies",
                     (cost, user_id, cost))
        if row is None:
            raise NoFunds("err_no_cookies")
        economy.record(user_id, "cookies", -cost, reason, row["cookies"], op,
                       ref_type=ref_type, ref_id=ref_id)
    return row["cookies"]


def buy_click_upgrade(user_id: int, cost: float, click_level: int) -> float:
    """Списание и повышение уровня клика ОДНИМ условным UPDATE.

    Отдельная функция, а не spend_cookies, потому что тут пишутся две колонки
    от одного и того же устаревшего чтения. Условие `click_level = ?` держит
    их вместе: куплен ровно тот уровень, за который посчитана цена, — иначе
    два параллельных апгрейда списали бы цену первого уровня дважды и подняли
    бы клик на две ступени."""
    cost = economy._sane(cost, "click_upgrade")
    op = economy.auto_op(user_id, "click_upgrade")
    with db.tx():
        row = db.q1w("UPDATE users SET cookies = cookies - ?, "
                     "click_level = click_level + 1, "
                     "user_revision = user_revision + 1 "
                     "WHERE user_id = ? AND cookies >= ? AND click_level = ? "
                     "RETURNING cookies, click_level",
                     (cost, user_id, cost, click_level))
        if row is None:
            raise NoFunds("err_no_cookies")
        economy.record(user_id, "cookies", -cost, "click_upgrade", row["cookies"], op)
    return row["cookies"]


# ---------- merge-доска: клетки ----------

def ref_count(user_id: int, qualified_only: bool = True) -> int:
    """Число приглашённых. По умолчанию считаются только «живые» — те, кто
    дошёл до REF_QUALIFY_LEVEL. Иначе 25 пустых аккаунтов приносили
    refs_premium, который в магазине стоит 100 Stars, плюс 4 клетки доски."""
    if not qualified_only:
        return db.q1("SELECT COUNT(*) c FROM referrals WHERE referrer_id = ?",
                     (user_id,))["c"]
    return db.q1(
        "SELECT COUNT(*) c FROM referrals r JOIN users u ON u.user_id = r.referred_id "
        "WHERE r.referrer_id = ? AND u.level >= ?",
        (user_id, cfg.REF_QUALIFY_LEVEL))["c"]


def compact_board(user_id: int, earned_cells: int) -> int:
    """Сдвигает печеньки в начало доски, если они стоят ВНЕ заслуженной зоны, и
    возвращает их количество. Нужно игрокам, набравшим 25 предметов до введения
    закрытых клеток: иначе половина доски оказывалась в закрытой зоне — двигать
    нельзя, доска считается полной, спавн запрещён.

    Обычную доску не трогаем: переставлять печеньки под игроком из-за дырки
    в середине нельзя. Переезд в два прохода через отрицательные номера —
    UNIQUE(user_id, cell) не даёт менять номера напрямую."""
    rows = db.q("SELECT id, cell FROM board WHERE user_id = ? ORDER BY cell", (user_id,))
    if not rows:
        return 0
    top = rows[-1]["cell"]
    if top >= earned_cells and top >= len(rows):  # легаси-доска и есть куда сжать
        with db.tx():
            for i, r in enumerate(rows):
                db.exec("UPDATE board SET cell = ? WHERE id = ?", (-1 - i, r["id"]))
            for i, r in enumerate(rows):
                db.exec("UPDATE board SET cell = ? WHERE id = ?", (i, r["id"]))
    return len(rows)


def merge_cells_unlocked_for(user: dict) -> int:
    """Сколько клеток доски открыто: база + уровни + друзья. Уже занятые клетки
    не отбираем (грандфазер старых досок) — по мере переплавки лишних печенек
    доска сама сходится к честному лимиту."""
    earned = cfg.merge_cells_unlocked(user["level"], ref_count(user["user_id"]))
    items = compact_board(user["user_id"], earned)
    return min(cfg.BOARD_SIZE, max(earned, items))


def board_cells_state(user: dict) -> dict:
    """Инфо для фронта: сколько открыто и как открыть следующие."""
    refs = ref_count(user["user_id"])
    return {
        "unlocked": merge_cells_unlocked_for(user),
        "earned": cfg.merge_cells_unlocked(user["level"], refs),
        "total": cfg.BOARD_SIZE,
        "next_unlock_level": min((lvl for lvl in cfg.MERGE_CELL_LEVELS
                                  if lvl > user["level"]), default=None),
        "ref_cells": [{"friends": n, "done": refs >= n} for n in cfg.MERGE_CELL_REFS],
        "refs": refs,
        "trash_refund": cfg.TRASH_REFUND,
    }


# ---------- пассивный доход с merge-доски ----------

def collect_passive(user: dict) -> float:
    """Начисляет накопленный пассивный доход, возвращает сколько упало.
    Таймер и деньги — одна транзакция (см. collect_farm)."""
    now = time.time()
    hours = min(passive_offline_cap_hours(user),
                (now - (user["passive_collected_at"] or now)) / 3600)
    if hours <= 0:
        return 0
    income = passive_per_hour(user["user_id"]) * hours
    with db.tx():
        db.update_user(user["user_id"], passive_collected_at=now)
        if income > 0:
            add_cookies(user["user_id"], income)
    return income


# Мемо часового дохода. hourly_income обходится в ~12 SQL-запросов и раньше
# звался по 3-5 раз за один full_state плюс отдельно на каждое из трёх дневных
# заданий — это была треть от 79 запросов, которые делал один /api/state.
# TTL короткий, а full_state сбрасывает кеш на входе, поэтому в ответе никогда
# не бывает устаревших чисел: мемо живёт только внутри одного запроса.
_income_memo: dict[int, tuple[float, float]] = {}
_INCOME_MEMO_TTL = 1.0


def invalidate_income(user_id: int | None = None):
    if user_id is None:
        _income_memo.clear()
    else:
        _income_memo.pop(user_id, None)


def income_base(user_id: int) -> float:
    """Доход в час БЕЗ учёта кликов: ферма + пассивка доски.

    От него считается сила клика (cfg.click_power). Брать полный hourly_income
    нельзя — тот сам включает оценку кликов, и получилась бы рекурсия."""
    return farm_cps(user_id) * 3600 + passive_per_hour(user_id)


def hourly_income(user_id: int) -> float:
    """Оценка часового дохода игрока для масштабируемых наград и цен:
    ферма + пассивка доски + скромная оценка кликов (5 мин активного тапа).
    Берём ТОЛЬКО постоянные множители: под золотой печенькой доход не должен
    подскакивать в 7 раз (это раздувало награды заказов и цены на доске)."""
    now = time.time()
    hit = _income_memo.get(user_id)
    if hit and now - hit[0] < _INCOME_MEMO_TTL:
        return hit[1]
    user = db.get_user(user_id)
    if not user:
        return 0.0
    base = income_base(user_id)
    clicks_estimate = (cfg.click_power(user["click_level"], base)
                       * permanent_click_multiplier(user_id) * 5 * 60)
    value = base + clicks_estimate
    if len(_income_memo) > 10_000:
        _income_memo.clear()
    _income_memo[user_id] = (now, value)
    return value


def board_base_income(user_id: int) -> float:
    """СЫРОЙ доход доски, без множителей престижа/коллекции/апгрейдов.

    От него считается цена спавна. Два «почему»:
    1) не от общего дохода — иначе экспоненциальный рост фермы уносит цены
       доски вверх, а доход печеньки остаётся константой, и доска умирает;
    2) без множителей — иначе престиж поднимал бы цены ровно во столько же
       раз, во сколько доход, спавнов в час было бы столько же, и престиж
       вообще не ускорял бы набор XP (то есть был бы бессмыслен)."""
    rows = db.q("SELECT item_level FROM board WHERE user_id = ?", (user_id,))
    return sum(cfg.passive_income_per_hour(r["item_level"]) for r in rows)


def passive_per_hour(user_id: int) -> float:
    base = board_base_income(user_id)
    user = db.get_user(user_id)
    prestige = cfg.prestige_multiplier(user["prestige_points"] if user else 0)
    return (base * upgrade_effects(user_id)["passive_mult"] * prestige
            * collection_multiplier(user_id))


# ---------- достижения ----------

def achievements_state(user: dict, lang: str = "en") -> list[dict]:
    from server.i18n import tr
    user_id = user["user_id"]
    refs = ref_count(user_id)   # только «живые» рефералы, как в майлстоунах
    claimed = {r["key"] for r in db.q(
        "SELECT key FROM achievements WHERE user_id = ? AND claimed = 1", (user_id,))}
    income = hourly_income(user_id)
    out = []
    for key, (_title, _desc, field, goal, base_reward) in cfg.ACHIEVEMENTS.items():
        progress = refs if field == "_refs" else user.get(field, 0)
        # награда не обесценивается: все ачивки вместе давали 69 000 печенек,
        # на 10-й день это полторы секунды дохода
        reward = cfg.scaled_reward(base_reward, "achievement", income)
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
            with db.tx():  # отметка и награда — одним куском
                # DO UPDATE ... WHERE claimed = 0 + rowcount: без условия два
                # параллельных клейма выдали бы награду дважды
                if db.exec("INSERT INTO achievements (user_id, key, claimed) VALUES (?, ?, 1) "
                           "ON CONFLICT(user_id, key) DO UPDATE SET claimed = 1 "
                           "WHERE claimed = 0", (user["user_id"], key)) == 0:
                    raise ValueError("err_claimed")
                add_cookies(user["user_id"], a["reward"], count_earned=False)
            return a["reward"]
    raise ValueError("err_no_item")


# ---------- заказы пекарни ----------
# Клей между режимами: ферма/клики/мердж дают прогресс одному активному заказу,
# награда-сундук масштабируется от дохода. offer(3) -> active(1) -> done.

def _order_difficulty(template_key: str) -> int:
    return cfg.ORDER_TEMPLATES.get(template_key, {}).get("difficulty", 1)


def order_reward(template_key: str, income: float) -> tuple[float, int]:
    """Награда заказа СЧИТАЕТСЯ ЗАНОВО от текущего дохода, а не хранится в строке.
    Иначе старый оффер, выписанный до престижа, платил бы 60M новичку 1 уровня,
    а выросший игрок добивал бы копеечные заказы ради обновления расценок.
    Часы дохода берутся по шаблону: заказ, который тратит печеньки на спавны и
    слияния, обязан отбивать затраты, иначе он чистый убыток."""
    diff = _order_difficulty(template_key)
    hours = cfg.ORDER_REWARD_HOURS.get(template_key, 0.3)
    return (max(cfg.ORDER_REWARD_MIN[diff], income * hours), cfg.ORDER_BP_XP[diff])


def _order_params(user: dict, template_key: str, income: float | None = None) -> dict:
    t = cfg.ORDER_TEMPLATES[template_key]
    income = hourly_income(user["user_id"]) if income is None else income
    goal = t["goal"]
    if t["metric"] == "earned":
        goal = max(2000, round(income))          # ~час дохода
    elif t["metric"] == "make_item":
        # печенье на уровень ниже максимально открытого — достижимо слиянием
        max_unlocked = max((l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                            if cfg.item_unlock_level(l) <= user["level"]), default=1)
        goal = max(3, max_unlocked - 1)
    reward, bp_xp = order_reward(template_key, income)
    return {"metric": t["metric"], "goal": goal, "reward_cookies": reward,
            "reward_bp_xp": bp_xp}


def _gen_order_offers(user: dict, income: float | None = None):
    """3 оффера: по одному каждой сложности, шаблон случайный."""
    uid = user["user_id"]
    income = hourly_income(uid) if income is None else income
    rnd = random.Random(f"{uid}:{int(time.time())}")
    now = time.time()
    with db.tx():
        db.exec("DELETE FROM orders WHERE user_id = ? AND status = 'offer'", (uid,))
        for slot, diff in enumerate((1, 2, 3), start=1):
            keys = sorted(k for k, t in cfg.ORDER_TEMPLATES.items()
                          if t["difficulty"] == diff)
            key = rnd.choice(keys)
            p = _order_params(user, key, income)
            db.exec(
                "INSERT INTO orders (user_id, slot, template, metric, goal, progress, "
                "reward_cookies, reward_bp_xp, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'offer', ?)",
                (uid, slot, key, p["metric"], p["goal"],
                 p["reward_cookies"], p["reward_bp_xp"], now))


def _refresh_offers(user: dict, income: float):
    """Непринятые офферы пересчитываются под текущий доход и уровень: игрок мог
    вырасти (или сделать престиж) с момента их выписки. Цель активного заказа
    НЕ трогаем — иначе накопленный прогресс потерял бы смысл."""
    for o in db.q("SELECT id, template FROM orders WHERE user_id = ? AND status = 'offer'",
                  (user["user_id"],)):
        if o["template"] not in cfg.ORDER_TEMPLATES:
            continue
        p = _order_params(user, o["template"], income)
        db.exec("UPDATE orders SET goal = ?, reward_cookies = ?, reward_bp_xp = ? "
                "WHERE id = ?",
                (p["goal"], p["reward_cookies"], p["reward_bp_xp"], o["id"]))


def _order_unreachable(user: dict, o: dict, income: float) -> bool:
    """Заказ стал невыполнимым: цель зафиксирована по старому прогрессу, а
    прогресс сбросился (престиж). «Сделай печеньку 23 уровня» при максимуме 8
    не закрыть никогда, а активный заказ всего один — вкладка пекарни намертво
    блокируется вместе с шагом чеклиста (фидбек)."""
    if o["progress"] >= o["goal"]:
        return False
    if o["metric"] == "make_item":
        max_unlocked = max((l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                            if cfg.item_unlock_level(l) <= user["level"]), default=1)
        return o["goal"] > max_unlocked
    if o["metric"] == "earned":
        return o["goal"] > max(2000.0, income) * cfg.ORDER_STALE_FACTOR
    return False


def _pack_order(o: dict, income: float) -> dict:
    reward, bp_xp = order_reward(o["template"], income)
    return {"slot": o["slot"], "template": o["template"], "metric": o["metric"],
            "goal": o["goal"], "progress": min(o["progress"], o["goal"]),
            "done": o["progress"] >= o["goal"],
            "reward_cookies": reward, "reward_bp_xp": bp_xp,
            "difficulty": _order_difficulty(o["template"])}


def orders_state(user: dict) -> dict:
    uid = user["user_id"]
    day = _utc_day(time.time())
    used = user["orders_day_count"] if user["orders_day"] == day else 0
    left = max(0, cfg.ORDERS_PER_DAY - used)
    income = hourly_income(uid)
    active = db.q1("SELECT * FROM orders WHERE user_id = ? AND status = 'active'", (uid,))
    # мёртвый заказ снимаем сами и БЕСПЛАТНО (дневной лимит не тратится):
    # игрок в него не виноват, а иначе пекарня заблокирована навсегда
    if active and _order_unreachable(user, active, income):
        db.exec("DELETE FROM orders WHERE id = ?", (active["id"],))
        track(uid, "order_stale_dropped")
        active = None
    offers = []
    if not active and left > 0:
        offers = db.q("SELECT * FROM orders WHERE user_id = ? AND status = 'offer' "
                      "ORDER BY slot", (uid,))
        if len(offers) != 3:
            _gen_order_offers(user, income)
        else:
            _refresh_offers(user, income)
        offers = db.q("SELECT * FROM orders WHERE user_id = ? AND status = 'offer' "
                      "ORDER BY slot", (uid,))
    return {"active": _pack_order(active, income) if active else None,
            "offers": [_pack_order(o, income) for o in offers],
            "left_today": left, "per_day": cfg.ORDERS_PER_DAY}


def take_order(user: dict, slot: int) -> dict:
    uid = user["user_id"]
    if db.q1("SELECT id FROM orders WHERE user_id = ? AND status = 'active'", (uid,)):
        raise ValueError("err_order_active")
    day = _utc_day(time.time())
    used = user["orders_day_count"] if user["orders_day"] == day else 0
    if used >= cfg.ORDERS_PER_DAY:
        raise ValueError("err_orders_limit")
    row = db.q1("SELECT * FROM orders WHERE user_id = ? AND status = 'offer' AND slot = ?",
                (uid, slot))
    if not row:
        raise ValueError("err_no_item")
    # цель фиксируем по СЕГОДНЯШНЕМУ доходу: оффер мог пролежать с прошлой сессии
    income = hourly_income(uid)
    p = _order_params(user, row["template"], income) \
        if row["template"] in cfg.ORDER_TEMPLATES else None
    with db.tx():
        if p:
            db.exec("UPDATE orders SET goal = ?, reward_cookies = ?, reward_bp_xp = ?, "
                    "status = 'active' WHERE id = ?",
                    (p["goal"], p["reward_cookies"], p["reward_bp_xp"], row["id"]))
            row = dict(row, goal=p["goal"])
        else:
            db.exec("UPDATE orders SET status = 'active' WHERE id = ?", (row["id"],))
        db.exec("DELETE FROM orders WHERE user_id = ? AND status = 'offer'", (uid,))
    track(uid, "order_take")
    return _pack_order(dict(row, status="active"), income)


def abandon_order(user: dict) -> dict:
    """Отказ от активного заказа по своей воле. В отличие от снятия мёртвого
    заказа стоит одну попытку из дневного лимита — иначе можно было бы
    бесконечно перебирать офферы в поисках удобного."""
    uid = user["user_id"]
    row = db.q1("SELECT id FROM orders WHERE user_id = ? AND status = 'active'", (uid,))
    if not row:
        raise ValueError("err_no_item")
    day = _utc_day(time.time())
    with db.tx():
        db.exec("DELETE FROM orders WHERE id = ?", (row["id"],))
        _bump_orders_day(uid, day)
    track(uid, "order_abandon")
    return orders_state(db.get_user(uid))


def _bump_orders_day(user_id: int, day: str, completed: bool = False):
    """Дневной счётчик заказов — одним относительным UPDATE.

    Считался в питоне от заранее прочитанного словаря: сдать заказ и бросить
    другой почти одновременно означало записать одно и то же `used + 1` дважды,
    и один из двух заказов дневного лимита не стоил. Смена дня решается тем же
    стейтментом (CASE по orders_day), поэтому отдельного чтения не нужно.

    Лимит тут НЕ проверяется намеренно: заказ уже выполнен, отказать на этом
    шаге значило бы забрать у игрока честную награду. Ворота стоят на взятии
    заказа (take_order), а от двух одновременных взятий защищает уникальность
    активного заказа."""
    db.exec(
        "UPDATE users SET "
        "orders_day = ?, "
        "orders_day_count = CASE WHEN orders_day = ? THEN orders_day_count + 1 "
        "                        ELSE 1 END, "
        + ("orders_completed = orders_completed + 1, " if completed else "") +
        "user_revision = user_revision + 1 "
        "WHERE user_id = ?", (day, day, user_id))


def order_progress(user_id: int, metric: str, amount: float):
    """Прогресс активного заказа. make_item — «лучший достигнутый уровень»."""
    if amount <= 0:
        return
    if metric == "make_item":
        db.exec("UPDATE orders SET progress = MAX(progress, ?) "
                "WHERE user_id = ? AND status = 'active' AND metric = 'make_item'",
                (amount, user_id))
    else:
        db.exec("UPDATE orders SET progress = progress + ? "
                "WHERE user_id = ? AND status = 'active' AND metric = ?",
                (amount, user_id, metric))


def claim_order(user: dict) -> dict:
    uid = user["user_id"]
    row = db.q1("SELECT * FROM orders WHERE user_id = ? AND status = 'active'", (uid,))
    if not row:
        raise ValueError("err_no_item")
    if row["progress"] < row["goal"]:
        raise ValueError("err_not_done")
    day = _utc_day(time.time())
    first = user["orders_completed"] == 0
    # платим по ТЕКУЩЕМУ доходу: хранимая сумма могла быть выписана до престижа
    reward, bp_xp = order_reward(row["template"], hourly_income(uid))
    with db.tx():
        # WHERE status = 'active' + rowcount: два параллельных клейма одного
        # заказа иначе оба прошли бы проверку выше и заплатили дважды
        if db.exec("UPDATE orders SET status = 'done', reward_cookies = ?, reward_bp_xp = ? "
                   "WHERE id = ? AND status = 'active'",
                   (reward, bp_xp, row["id"])) == 0:
            raise ValueError("err_claimed")
        add_cookies(uid, reward, count_earned=False)
        add_xp(uid, 0, bp_xp)
        _bump_orders_day(uid, day, completed=True)
    track(uid, "order_done")
    if first:
        track(uid, "first_order")
    return {"reward_cookies": reward, "reward_bp_xp": bp_xp}


# ---------- коллекция блестящих печенек ----------

def claim_item_record(user: dict, item_level: int) -> dict | None:
    """Награда за ЛИЧНЫЙ РЕКОРД тира — основной источник XP уровня.

    Платим за КАЖДЫЙ тир от старого рекорда до нового, а не только за
    достигнутый: прямая покупка тира (spawn_direct) позволяет перепрыгнуть
    несколько ступеней, и иначе их XP пропадал бы молча.

    Возвращает описание награды для ответа ручки или None, если рекорд не
    побит (подавляющее большинство мерджей)."""
    uid = user["user_id"]
    best = user["best_item_level"] or 0
    if item_level <= best:
        return None
    levels = range(max(best + 1, 2), item_level + 1)
    xp = sum(cfg.first_item_xp(l) for l in levels)
    bp_xp = sum(cfg.first_item_bp_xp(l) for l in levels)
    cookies = cfg.scaled_reward(0, "item_record", hourly_income(uid))
    db.update_user(uid, best_item_level=item_level)
    if cookies:
        add_cookies(uid, cookies)
    add_xp(uid, xp, bp_xp)
    track(uid, "item_record", item_level)
    return {"level": item_level, "xp": xp, "cookies": cookies}


def roll_shiny(user: dict, item_level: int) -> int | None:
    """Бросок на блестяшку при мердже; pity гарантирует дроп раз в SHINY_PITY.
    Возвращает уровень попавший в альбом или None.

    Если выпавший уровень уже собран, отдаём ближайший НЕсобранный (не выше
    выпавшего): раньше дубликат молча терялся в INSERT OR IGNORE, но pity
    обнулялся — альбом можно было не добить никогда."""
    uid = user["user_id"]
    pity = user["shiny_pity"] + 1
    if pity < cfg.SHINY_PITY and random.random() >= cfg.SHINY_CHANCE:
        db.update_user(uid, shiny_pity=pity)
        return None
    owned = {r["item_level"] for r in
             db.q("SELECT item_level FROM collection WHERE user_id = ?", (uid,))}
    target = item_level if item_level not in owned else next(
        (l for l in range(item_level - 1, 0, -1) if l not in owned), None)
    if target is None:  # всё до этого уровня собрано — pity сохраняем на будущее
        db.update_user(uid, shiny_pity=pity)
        return None
    with db.tx():
        db.update_user(uid, shiny_pity=0)
        db.exec("INSERT OR IGNORE INTO collection (user_id, item_level, obtained_at) "
                "VALUES (?, ?, ?)", (uid, target, time.time()))
    track(uid, "shiny_drop", target)
    return target


def collection_sets_done(user_id: int) -> int:
    owned = {r["item_level"] for r in
             db.q("SELECT item_level FROM collection WHERE user_id = ?", (user_id,))}
    return sum(1 for lo, hi in cfg.COLLECTION_SETS
               if all(l in owned for l in range(lo, hi + 1)))


def collection_multiplier(user_id: int) -> float:
    """Постоянный бонус за собранные наборы — применяется ко ВСЕМУ доходу."""
    return 1.0 + collection_sets_done(user_id) * cfg.COLLECTION_SET_BONUS


def collection_state(user: dict) -> dict:
    uid = user["user_id"]
    owned = {r["item_level"]: r["obtained_at"] for r in
             db.q("SELECT item_level, obtained_at FROM collection WHERE user_id = ?", (uid,))}
    sets = []
    for lo, hi in cfg.COLLECTION_SETS:
        have = sum(1 for l in range(lo, hi + 1) if l in owned)
        sets.append({"from": lo, "to": hi, "have": have, "need": hi - lo + 1,
                     "done": have == hi - lo + 1})
    return {"owned": sorted(owned), "sets": sets,
            "set_bonus": cfg.COLLECTION_SET_BONUS,
            "multiplier": collection_multiplier(uid),
            "pity": user["shiny_pity"], "pity_at": cfg.SHINY_PITY,
            "max_level": cfg.MAX_ITEM_LEVEL}


# ---------- стартовый чеклист ----------

def tutorial_state(user: dict) -> dict:
    buildings = db.q1("SELECT COALESCE(SUM(count), 0) c FROM farm WHERE user_id = ?",
                      (user["user_id"],))["c"]
    done = {
        "clicks10": user["total_clicks"] >= 10,
        "merge1": user["total_merges"] >= 1,
        "building1": buildings >= 1,
        "order1": user["orders_completed"] >= 1,
    }
    return {"steps": [{"key": k, "done": done[k]} for k in cfg.TUTORIAL_STEPS],
            "all_done": all(done.values()),
            "claimed": bool(user["tutorial_done"]),
            "reward": tutorial_reward(user["user_id"])}


def tutorial_reward(user_id: int) -> float:
    return cfg.scaled_reward(cfg.TUTORIAL_REWARD, "tutorial", hourly_income(user_id))


def claim_tutorial(user: dict) -> dict:
    st = tutorial_state(user)
    if st["claimed"]:
        raise ValueError("err_claimed")
    if not st["all_done"]:
        raise ValueError("err_not_done")
    reward = tutorial_reward(user["user_id"])
    with db.tx():
        if db.exec("UPDATE users SET tutorial_done = 1 "
                   "WHERE user_id = ? AND tutorial_done = 0", (user["user_id"],)) == 0:
            raise ValueError("err_claimed")
        add_cookies(user["user_id"], reward, count_earned=False)
    track(user["user_id"], "tutorial_complete")
    return {"reward": reward}


# ---------- выдача Stars-покупок ----------

def purchase_blocked(user_id: int | None, item_key: str) -> str | None:
    """Код причины, по которой товар нельзя продать этому игроку, иначе None.

    Проверяется в pre_checkout, а не после оплаты: invoice-ссылка многоразовая
    и живёт в чате вечно, поэтому «спрятать кнопку на фронте» ничего не решает.
    """
    item = cfg.SHOP_ITEMS.get(item_key)
    if not item:
        return "err_no_item"
    if user_id is None:
        return None
    effect = item[3]
    user = db.get_user(user_id)
    if not user:
        return None
    if effect["type"] == "bp_premium" and user["bp_premium"]:
        return "err_owned"
    if effect["type"] == "offline_cap":
        have = offline_bonus_hours(user)
        # младший тир поверх старшего бесполезен: эффект применяется как max()
        if have >= effect["hours"]:
            return "err_owned"
        # апгрейд по цене разницы продаётся только владельцу младшего тира
        if have < effect.get("requires_hours", 0):
            return "err_needs_base"
    return None


def purchase_already_owned(user_id: int | None, item_key: str) -> bool:
    """Товар нельзя выдать этому игроку (используется при выдаче и в магазине)."""
    return purchase_blocked(user_id, item_key) is not None


def record_unmatched_payment(user_id: int, item_key: str, amount: int,
                             charge_id: str, payload: str):
    """Оплата, которая не сходится с конфигом, всё равно должна оставить след.

    Раньше такой платёж молча уходил в `return`: деньги списаны, товара нет,
    сверить и вернуть невозможно. Строка с 'unmatched' попадает в
    /api/admin/payments, где её видит владелец."""
    db.exec(
        "INSERT OR IGNORE INTO purchases (user_id, item_key, stars_amount, "
        "tg_payment_id, status, created_at) VALUES (?, ?, ?, ?, 'unmatched', ?)",
        (user_id, (item_key or payload or "")[:64], amount,
         charge_id or None, time.time()))


def _revoke_purchase_effect(user_id: int, item_key: str):
    """Откат эффекта при возврате звёзд. Вызывается внутри db.tx()."""
    effect = cfg.SHOP_ITEMS.get(item_key, {})[3] \
        if item_key in cfg.SHOP_ITEMS else None
    if not effect:
        return
    user = db.get_user(user_id)
    if not user:
        return
    if effect["type"] == "cookies":
        # снимаем ровно столько, сколько выдали бы сейчас; в минус не уводим
        amount = max(effect.get("min_amount", 0),
                     hourly_income(user_id) * effect.get("income_hours", 0)) \
            if "income_hours" in effect else effect["amount"]
        db.update_user(user_id, cookies=max(0.0, user["cookies"] - amount))
    elif effect["type"] == "boost":
        db.exec("DELETE FROM boosts WHERE user_id = ? AND boost_key = ?",
                (user_id, effect["key"]))
    elif effect["type"] == "bp_premium":
        db.update_user(user_id, bp_premium=0, bp_premium_next=0)
    elif effect["type"] == "offline_cap":
        db.update_user(user_id, offline_bonus_hours=max(
            0.0, offline_bonus_hours(user) - effect["hours"]))
    # energy_full откатывать нечего — энергия и так расходуется


def revoke_charge(charge_id: str) -> bool:
    """Возврат звёзд: снимаем выданное и помечаем покупку 'refunded'.
    Идемпотентно — повторный refund ничего не меняет."""
    with db.tx():
        row = db.q1("SELECT user_id, item_key, status FROM purchases "
                    "WHERE tg_payment_id = ?", (charge_id,))
        if not row or row["status"] == "refunded":
            return False
        if row["status"] == "fulfilled":
            _revoke_purchase_effect(row["user_id"], row["item_key"])
        db.exec("UPDATE purchases SET status = 'refunded' WHERE tg_payment_id = ?",
                (charge_id,))
        return True


def _apply_purchase_effect(user_id: int, item_key: str):
    """Применяет эффект купленного товара. Вызывается внутри db.tx()."""
    effect = cfg.SHOP_ITEMS[item_key][3]
    if effect["type"] == "cookies":
        if "income_hours" in effect:
            amount = max(effect["min_amount"],
                         hourly_income(user_id) * effect["income_hours"])
        else:
            amount = effect["amount"]
        add_cookies(user_id, amount, count_earned=False)
    elif effect["type"] == "energy_full":
        # именно energy_cap, а не cfg.max_energy: иначе купленный за Stars
        # «полный бак» игнорировал апгрейды energy_cap_* и недоливал до 750
        user = db.get_user(user_id)
        db.update_user(user_id, energy=energy_cap(user),
                       energy_updated_at=time.time())
    elif effect["type"] == "boost":
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                (user_id, effect["key"], time.time() + effect["hours"] * 3600))
    elif effect["type"] == "bp_premium":
        grant_bp_premium(user_id)
    elif effect["type"] == "offline_cap":
        # постоянный бонус; max — покупка старшего тира поверх младшего апгрейдит
        user = db.get_user(user_id)
        db.update_user(user_id, offline_bonus_hours=max(
            offline_bonus_hours(user), effect["hours"]))


def fulfill_charge(charge_id: str) -> bool:
    """Выдаёт оплаченную покупку по charge_id. Статус перечитывается УЖЕ
    внутри BEGIN IMMEDIATE: два worker'а не выдадут одно и то же дважды.
    Возвращает True, если выдали сейчас; False — уже было выдано/нет записи."""
    if not charge_id:
        return False
    with db.tx():
        row = db.q1("SELECT user_id, item_key, status FROM purchases "
                    "WHERE tg_payment_id = ?", (charge_id,))
        if not row or row["status"] != "paid":
            return False
        # Товар исчез из конфига или уже куплен навсегда — выдать нечего.
        # Раньше такая строка вечно висела в 'paid': fulfill_pending перебирал
        # её на каждом /auth, каждый раз возвращал False, и никто не узнавал.
        if row["item_key"] not in cfg.SHOP_ITEMS \
                or purchase_already_owned(row["user_id"], row["item_key"]):
            db.exec("UPDATE purchases SET status = 'void' WHERE tg_payment_id = ?",
                    (charge_id,))
            return False
        _apply_purchase_effect(row["user_id"], row["item_key"])
        db.exec("UPDATE purchases SET status = 'fulfilled' WHERE tg_payment_id = ?",
                (charge_id,))
        return True


def fulfill_pending(user_id: int) -> int:
    """Довыдаёт зависшие 'paid' покупки юзера (сбой между оплатой и выдачей).
    Дёргается на /auth — игрок получает недовыданное при следующем входе."""
    rows = db.q("SELECT tg_payment_id FROM purchases WHERE user_id = ? "
                "AND status = 'paid' AND tg_payment_id IS NOT NULL", (user_id,))
    return sum(1 for r in rows if fulfill_charge(r["tg_payment_id"]))


# ---------- профиль целиком (для фронта) ----------

def max_item_unlocked(user_level: int) -> int:
    return max((lvl for lvl in range(1, cfg.MAX_ITEM_LEVEL + 1)
                if cfg.item_unlock_level(lvl) <= user_level), default=1)


def _direct_max_level(user: dict) -> int:
    """Максимальный тир для прямой покупки: топ-тиры только слиянием."""
    return max(1, max_item_unlocked(user["level"]) - cfg.SPAWN_DIRECT_GAP)


def full_state(user_id: int) -> dict:
    # full_state возвращается из каждой изменяющей ручки, поэтому сбрасываем
    # мемо дохода здесь: внутри одного ответа все расчёты используют одно
    # свежее значение, а между запросами оно не переживает мутацию
    invalidate_income(user_id)
    user = db.get_user(user_id)
    user = refresh_energy(user)
    db.update_user(user_id, last_seen_at=time.time())
    board = db.q("SELECT cell, item_level FROM board WHERE user_id = ? ORDER BY cell", (user_id,))
    items_count = len(board)
    income = hourly_income(user_id)          # награды масштабируются от дохода
    board_income = board_base_income(user_id)  # цены спавна — только от доски
    base_income = income_base(user_id)        # сила клика — от фермы и доски
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
            # фактическая скорость регена (с апгрейдами) — клиент рисует такой же тик
            "energy_regen": cfg.ENERGY_REGEN_PER_SEC + eff["energy_regen"],
            "click_level": user["click_level"],
            "click_power": (cfg.click_power(user["click_level"], base_income)
                            * click_multiplier(user_id)),
            # сила СЛЕДУЮЩЕГО уровня приходит с сервера: она считается от
            # дохода, и клиентское «+1 за уровень» дезинформировало
            "click_power_next": (cfg.click_power(user["click_level"] + 1, base_income)
                                 * click_multiplier(user_id)),
            "click_upgrade_cost": cfg.click_upgrade_cost(user["click_level"], base_income),
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
        # стартовый чеклист: показывается, пока награда не забрана
        "tutorial": tutorial_state(user) if not user["tutorial_done"] else None,
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
        "board_cells": board_cells_state(user),
        "spawn_cost": cfg.spawn_cost(items_count, board_income),
        # прямая покупка печенек выше 1 lvl: доступные уровни и цены.
        # Отдаём только реально доступные тиры: цены за 21 lvl (6.5 млн часов
        # дохода) — мусор в ответе, который фронт всё равно не показывает
        "spawn_direct": {
            "max_level": _direct_max_level(user),
            "costs": {str(l): cfg.direct_spawn_cost(l, items_count, board_income)
                      for l in range(1, _direct_max_level(user) + 2)},
        },
        # бейдж на вкладке пекарни: активный заказ выполнен и ждёт сдачи
        "orders_claimable": bool(db.q1(
            "SELECT id FROM orders WHERE user_id = ? AND status = 'active' "
            "AND progress >= goal", (user_id,))),
        "passive_per_hour": passive_per_hour(user_id),
        "boosts": [
            {"key": r["boost_key"], "expires_at": r["expires_at"]}
            for r in db.q("SELECT boost_key, expires_at FROM boosts "
                          "WHERE user_id = ? AND expires_at > ?", (user_id, time.time()))
        ],
        "claimable_level": claimable_level(user),
        "max_item_unlocked": max_item_unlocked(user["level"]),
        # потолок прокачки клика: кнопка апгрейда должна объяснять, почему
        # она погасла, а не просто отдавать ошибку по тапу
        "click_max_level": cfg.click_max_level(user["level"]),
        # закваска и ивент выходных — оба видны на главном экране
        "recipe": recipe_status(user),
        "event": active_event(),
    }
