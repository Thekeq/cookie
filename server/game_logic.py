"""Общая игровая логика поверх БД. Сервер — единственный источник правды."""
import datetime
import hashlib
import json
import random
import time
import uuid

from server import cache
from server import economy
from server import game_config as cfg

# один экземпляр базы на процесс: книга операций обязана писаться в ТОЙ ЖЕ
# транзакции, что и само движение денег, а значит и через то же соединение
db = economy.db


# ---------- лимитер запросов ----------
# Смысл — не пустить перебор промокодов и не дать одному игроку выжечь тяжёлые
# ручки: /api/state делает под сотню SQL-запросов, а SQLite синхронный и
# блокирует весь процесс, включая поллинг бота.
#
# Само окно переехало в server.cache: пока процесс один, оно по-прежнему живёт
# в памяти, но с несколькими воркерами лимит на общей памяти становился
# N-кратным — восемь воркеров означали восьмикратный перебор промокодов.


def check_rate_limit(user_id: int, bucket: str, limit: int, window: float):
    """Кидает HTTP 429, если за window секунд было больше limit обращений."""
    from fastapi import HTTPException
    allowed, _ = cache.incr_window(f"{bucket}:{user_id}", limit, window)
    if not allowed:
        raise HTTPException(429, "err_too_fast")


# ---------- аналитика ----------

def track(user_id: int, event: str, value: float = 0):
    """Пишет событие аналитики. Одна вставка, никогда не роняет игровой код."""
    try:
        db.exec("INSERT INTO events (user_id, event, value, created_at) "
                "VALUES (?, ?, ?, ?)", (user_id, event, value, time.time()))
    except Exception:  # noqa: S110 — аналитика не имеет права ронять игру
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


# bp_claimed_free/bp_claimed_premium тут больше не чистятся: забранные награды
# живут строками bp_claims с season_id в ключе, поэтому новый сезон — просто
# другой набор строк, сбрасывать нечего. Колонки остались в схеме нетронутыми,
# чтобы откат кода на прошлую версию не потерял историю.
_SEASON_RESET_SQL = (
    "UPDATE users SET season_id = ?, season_earned = 0, bp_xp = 0, "
    "bp_premium = bp_premium_next, bp_premium_next = 0 ")


def _ensure_season_snapshot(season: int):
    """Снапшот топа сезона и выплата призов — ровно один раз.

    Маркер — наличие строк season_results: без него после частичного сброса
    победители пересчитались бы по остатку. add_cookies(count_earned=False)
    не трогает season_earned, поэтому платить можно до сброса.

    Платит ТОЛЬКО тот вызов, чья вставка снапшота действительно прошла. Раньше
    выдача стояла рядом с `INSERT OR IGNORE` и не смотрела на его результат:
    конфликт гасился молча, а деньги уходили всё равно — и в момент смены сезона,
    когда ролловер по таймеру и первый запрос игрока сходятся на одной секунде,
    призы выдавались дважды. Проверка строк выше — быстрый выход, а не гарантия;
    гарантию даёт rowcount. Плюс токен операции: даже если движение когда-нибудь
    вызовут в обход этой проверки, книга не пропустит второе начисление."""
    if db.q1("SELECT 1 x FROM season_results WHERE season_id = ? LIMIT 1", (season,)):
        return
    with db.tx():
        now = time.time()
        for uid, rank, earned in _season_winners(season):
            reward = cfg.season_reward(rank, earned)
            fresh = db.exec(
                "INSERT INTO season_results (season_id, user_id, "
                "rank, earned, reward_cookies, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (season_id, user_id) DO NOTHING",
                (season, uid, rank, earned, reward, now))
            if fresh and reward:
                add_cookies(uid, reward, count_earned=False,
                            operation_id=f"season_prize:{season}:{uid}",
                            reason="season_prize")


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


def grant_bp_premium(user_id: int, now: float | None = None, *,
                     source: str = "manual", source_ref: str = ""):
    """Выдаёт premium-пасс за 100⭐. Флаг переноса на следующий сезон ставится
    в двух случаях, и оба — про то, чтобы покупка не сгорела:

    1) до конца сезона осталось меньше BP_PREMIUM_GRACE_DAYS — иначе покупка
       накануне ролловера обнулялась бы через несколько часов;
    2) сезон УЖЕ сменился, но пакетный сброс (порциями по 500) ещё не дошёл
       до этой строки — без флага ближайший чужой запрос прогнал бы её чанк
       и стёр только что оплаченный товар. Довыдачи в этом случае не будет:
       покупка уже помечена 'fulfilled'.

    Кроме флага пишется СТРОКА ПРАВА с источником. Флаг — один бит, по нему
    нельзя сказать, чем он поднят, и возврат покупки гасил его целиком: игрок,
    у которого пасс был ещё и за 25 рефералов, терял оба. Возврат снимает своё
    право и пересобирает флаг по остаткам (см. _recompute_bp_premium)."""
    now = now or time.time()
    user = db.get_user(user_id)
    cur = current_season()
    season = user["season_id"] if user else cur
    rollover_pending = bool(user) and user["season_id"] < cur
    carry = (season_end_ts(cur) - now <= cfg.BP_PREMIUM_GRACE_DAYS * 86400
             or rollover_pending)
    with db.tx():
        # право привязано к сезону, потому что и флаг привязан: ролловер
        # обнуляет bp_premium, и право прошлого сезона не должно его вернуть
        seasons = [season]
        if carry:
            # куда уедет перенос: отставшей строке сброс поставит ТЕКУЩИЙ сезон,
            # актуальной — следующий
            seasons.append(cur if rollover_pending else cur + 1)
        for s in seasons:
            db.exec("INSERT INTO entitlements (user_id, kind, source, source_ref, "
                    "season_id, created_at) VALUES (?, 'bp_premium', ?, ?, ?, ?) "
                    "ON CONFLICT (user_id, kind, source, source_ref, season_id) "
                    "DO NOTHING", (user_id, source, source_ref, s, now))
        fields = {"bp_premium": 1}
        if carry:
            fields["bp_premium_next"] = 1
        db.update_user(user_id, **fields)


def _recompute_bp_premium(user_id: int):
    """Пересобирает флаги пасса по оставшимся правам. Зовётся после отзыва.

    Вычитать нельзя: флаг не считает источники, он один. Поэтому после снятия
    своего права флаг выставляется заново по факту — есть ли у игрока хоть одно
    право на этот сезон (и на тот, куда уедет перенос)."""
    user = db.get_user(user_id)
    if not user:
        return
    cur = current_season()
    season = user["season_id"]
    nxt = cur if season < cur else cur + 1
    have = {r["season_id"] for r in db.q(
        "SELECT DISTINCT season_id FROM entitlements "
        "WHERE user_id = ? AND kind = 'bp_premium'", (user_id,))}
    db.update_user(user_id, bp_premium=1 if season in have else 0,
                   bp_premium_next=1 if nxt in have else 0)


def my_last_season_result(user_id: int) -> dict | None:
    return db.q1(
        "SELECT season_id, rank, earned, reward_cookies FROM season_results "
        "WHERE user_id = ? ORDER BY season_id DESC LIMIT 1", (user_id,))


# ---------- ежедневная награда (стрик) ----------

def _utc_day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")


def _iso_week(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%G-W%V")


def _day_start(ts: float) -> float:
    """Полночь UTC того дня, в который попал ts.

    Нужна там, где «сегодня» проверяется в SQL: строку дня в базе не собрать
    портируемо (strftime у SQLite и to_char у PostgreSQL — разные функции), а
    сравнение метки с границей суток работает одинаково и точнее."""
    d = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    return datetime.datetime(d.year, d.month, d.day,
                             tzinfo=datetime.timezone.utc).timestamp()


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
    """Возвращает {streak, reward} или кидает ValueError.

    Право на награду решает один стейтмент, и решает его база. Раньше проверка
    «сегодня ещё не забирал» стояла в питоне над прочитанным словарём: два
    нажатия вплотную (а именно так и выглядит нажатие на подвисшем соединении)
    оба её проходили и оба выдавали награду — сколько нажатий, столько наград.
    Теперь второй проигрывает по rowcount и получает err_already_today.

    Ту же развилку — продолжить стрик, сжечь его или потратить недельную
    заморозку — тоже считает база, от `daily_claimed_at` в самой строке.
    Начислять по прочитанному `daily_streak` значило бы, что стрик может уехать
    на день назад: два клейма в соседние сутки, разошедшиеся в чтении, записали
    бы одно и то же значение.

    Заморозка выдаётся один раз в ISO-неделю и только если стрик уже есть, — то
    же условие, что и в `_freeze_available`, но выраженное в SQL."""
    now = time.time()
    today0 = _day_start(now)
    yest0 = today0 - 86400
    dayb0 = today0 - 2 * 86400
    week = _iso_week(now)
    # «стрик выживает» = забирал вчера, либо позавчера и есть заморозка
    alive = ("(COALESCE(daily_claimed_at, 0) >= ? "
             " OR (COALESCE(daily_claimed_at, 0) >= ? "
             "     AND COALESCE(streak_freeze_week, '') <> ? AND daily_streak > 0))")
    # «заморозка тратится» = ровно пропущенный день, и она ещё не тратилась
    thaw = ("(COALESCE(daily_claimed_at, 0) < ? AND COALESCE(daily_claimed_at, 0) >= ? "
            " AND COALESCE(streak_freeze_week, '') <> ? AND daily_streak > 0)")
    with db.tx():  # отметка о получении и деньги — одним куском
        row = db.q1w(
            f"UPDATE users SET "
            f"daily_streak = CASE WHEN {alive} THEN daily_streak + 1 ELSE 1 END, "
            f"streak_freeze_week = CASE WHEN {thaw} THEN ? ELSE streak_freeze_week END, "
            f"daily_claimed_at = ?, user_revision = user_revision + 1 "
            f"WHERE user_id = ? AND COALESCE(daily_claimed_at, 0) < ? "
            f"RETURNING daily_streak, streak_freeze_week",
            (yest0, dayb0, week,            # alive
             yest0, dayb0, week, week,      # thaw + новое значение недели
             now, user["user_id"], today0))
        if row is None:
            raise ValueError("err_already_today")
        streak = row["daily_streak"]
        reward = cfg.scaled_reward(cfg.daily_reward(streak), "daily",
                                   hourly_income(user["user_id"]))
        add_cookies(user["user_id"], reward, count_earned=False)
    # метка недели могла измениться только этим стейтментом: любой параллельный
    # клейм проставил бы сегодняшнюю дату и снял бы нашу охрану
    freeze_used = (row["streak_freeze_week"] == week
                   and user.get("streak_freeze_week") != week)
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
        db.exec("INSERT INTO daily_quests (user_id, day, quest_key) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, day, quest_key) DO NOTHING",
                (user_id, day, key))
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
        # право на реролл решает rowcount: проверка выше стоит над прочитанным
        # словарём, и два нажатия вплотную оба её проходили — за день можно было
        # перебрать несколько заданий вместо одного
        if db.exec("UPDATE users SET quest_reroll_day = ?, "
                   "user_revision = user_revision + 1 "
                   "WHERE user_id = ? AND COALESCE(quest_reroll_day, '') <> ?",
                   (day, user["user_id"], day)) == 0:
            raise ValueError("err_no_reroll")
        db.exec("DELETE FROM daily_quests WHERE id = ?", (row["id"],))
        db.exec("INSERT INTO daily_quests (user_id, day, quest_key) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT (user_id, day, quest_key) DO NOTHING",
                (user["user_id"], day, new_key))
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
    xp = cfg.quest_reward_xp(user["level"])
    with db.tx():  # отметка + печеньки + BP XP — одним куском
        # условный UPDATE: параллельный клейм не выдаст награду дважды
        if db.exec("UPDATE daily_quests SET claimed = 1 WHERE id = ? AND claimed = 0",
                   (row["id"],)) == 0:
            raise ValueError("err_claimed")
        add_cookies(user["user_id"], reward, count_earned=False)
        # XP уровня, а не только батл-пасса: дневной контент — единственный
        # источник прогресса, не зависящий от темпа тапа (см. QUEST_XP_SHARE)
        add_xp(user["user_id"], xp, bp_xp)
    return {"reward_cookies": reward, "reward_bp_xp": bp_xp, "reward_xp": xp}


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
        # вставка + rowcount: проверка claimed выше живёт вне транзакции,
        # и два параллельных клейма прошли бы её оба
        if db.exec("INSERT INTO ref_claims (user_id, milestone_key, claimed_at) "
                   "VALUES (?, ?, ?) "
                   "ON CONFLICT (user_id, milestone_key) DO NOTHING",
                   (user["user_id"], key, time.time())) == 0:
            raise ValueError("err_claimed")
        if ms["type"] == "boost":
            # source обязателен: ключ click_x2 выдаёт ещё и покупка за Stars, и
            # её возврат не должен снять буст, заработанный рефералами
            db.exec("INSERT INTO boosts (user_id, boost_key, expires_at, source) "
                    "VALUES (?, ?, ?, 'ref_milestone')",
                    (user["user_id"], "click_x2", time.time() + ms["hours"] * 3600))
        elif ms["type"] == "skin":
            db.exec("INSERT INTO skins (user_id, skin_key) VALUES (?, ?) "
                    "ON CONFLICT (user_id, skin_key) DO NOTHING",
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
    """Доначисляет энергию по прошедшему времени. Возвращает свежего юзера.

    И прошедшее время, и сложение считает база. Раньше и то и другое брали из
    прочитанного заранее словаря: два запроса игрока, пришедшие вплотную (а на
    активной игре они идут вплотную постоянно), читали одну и ту же
    `energy_updated_at` и оба записывали энергию «как будто трат не было» —
    списанные клики возвращались игроку сами.

    `energy_updated_at <= now` — не оптимизация, а защита от отката времени:
    если параллельный запрос уже досчитал энергию до более поздней метки, наш
    UPDATE не проходит вовсе, вместо того чтобы начислить тот же интервал
    второй раз. Тогда возвращаем то, что прочитали, — оно не хуже.

    Реген в книгу не пишется намеренно: он производная от времени, а не минт
    (см. LEDGERED_PARTIAL)."""
    now = time.time()
    uid = user["user_id"]
    eff = upgrade_effects(uid)
    cap = energy_cap(user, eff)
    regen = cfg.ENERGY_REGEN_PER_SEC + eff["energy_regen"]
    # COALESCE обязателен: NULL в метке отравил бы всё выражение, LEAST(cap, NULL)
    # дал бы NULL, а охрана перестала бы срабатывать — энергия встала бы навсегда
    row = db.q1w(
        f"UPDATE users SET "
        f"energy = {db.LEAST}(?, energy + (? - COALESCE(energy_updated_at, ?)) * ?), "
        f"energy_updated_at = ? "
        f"WHERE user_id = ? AND COALESCE(energy_updated_at, 0) <= ? "
        f"RETURNING energy, energy_updated_at",
        (cap, now, now, regen, now, uid, now))
    return dict(user, **row) if row else user


def grant_energy(user_id: int, amount: float, reason: str,
                 operation_id: str | None = None) -> float:
    """Доливает энергию с клэмпом по потолку. Возвращает новое значение.

    Клэмп обязателен и раньше стоял в питоне на каждом вызывающем: без него
    выданный сверх бака излишек всё равно срезал бы первый же `refresh_energy`,
    и игрок молча терял часть награды. Теперь он в SQL и, значит, считается от
    фактического остатка, а не от прочитанного.

    В отличие от регена, выдача — это минт, и она пишется в книгу: по ней видно,
    сколько энергии зашло за Stars, за промокод и за пасс. Записываем ФАКТ, то
    есть сколько влилось после клэмпа, а не сколько просили.

    `operation_id` делает выдачу идемпотентной: повтор ничего не доливает."""
    amount = economy._sane(amount, "grant_energy")
    if amount <= 0:
        return db.get_user(user_id)["energy"]
    op = operation_id or economy.auto_op(user_id, reason)
    with db.tx():
        if operation_id and economy.already_recorded(operation_id, "energy"):
            return db.get_user(user_id)["energy"]
        # сперва доначисляем реген: иначе клэмп сравнивал бы бак с устаревшим
        # остатком и срезал бы выдачу сильнее, чем нужно
        before = refresh_energy(db.get_user(user_id))
        row = db.q1w(f"UPDATE users SET energy = {db.LEAST}(?, energy + ?), "
                     f"user_revision = user_revision + 1 "
                     f"WHERE user_id = ? RETURNING energy",
                     (energy_cap(before), amount, user_id))
        if row is None:
            raise ValueError("err_no_user")
        moved = row["energy"] - before["energy"]
        if moved:
            economy.record(user_id, "energy", moved, reason, row["energy"], op)
        return row["energy"]


def bump_click_window(user_id: int, day: str, clicks: int, now: float):
    """Счётчики кликов и окно античита — одним относительным UPDATE.

    Окно CPS тут важнее счётчиков: это античит, и потерянный апдейт в нём значит,
    что второй батч намерил себе допуск заново — ровно то, чего добивается
    автокликер, присылая батчи вплотную. Раньше и накопление окна, и вычитание
    кликов считались в питоне от заранее прочитанного словаря.

    Зовётся и при отказе (clicks=0): иначе батч, отбитый пустой энергией,
    оставлял бы окно нетронутым, и допуск копился бы, пока энергия не вернётся."""
    db.exec(
        "UPDATE users SET "
        "total_clicks = total_clicks + ?, "
        "clicks_day = ?, "
        "clicks_day_count = CASE WHEN clicks_day = ? THEN clicks_day_count + ? "
        "                        ELSE ? END, "
        # окно копится со скоростью MAX_CPS от своей же прошлой метки и упирается
        # в трёхсекундный запас; пустая метка = первый батч, ему дают полный запас
        f"cps_allowance = {db.LEAST}(?, CASE WHEN COALESCE(cps_ts, 0) = 0 THEN ? "
        f"                                  ELSE cps_allowance + (? - cps_ts) * ? "
        f"                             END) - ?, "
        "cps_ts = ?, "
        "user_revision = user_revision + 1 "
        "WHERE user_id = ?",
        (clicks, day, day, clicks, clicks,
         cfg.MAX_CPS * 3, cfg.MAX_CPS * 3, now, cfg.MAX_CPS, clicks,
         now, user_id))


def spend_energy_clicks(user_id: int, clicks: int) -> int:
    """Списывает энергию под клики и возвращает, сколько кликов ОПЛАЧЕНО.

    Число оплаченных кликов теперь определяет база. Раньше его считали от
    заранее прочитанной энергии (`min(clicks, energy // ENERGY_PER_CLICK)`), и
    два батча, пришедшие вплотную, оба мерили один и тот же остаток: второй
    батч играл бесплатно. Именно этим и пользуется автокликер — он и присылает
    батчи вплотную.

    Охрана `energy >= ?` не проходит только если энергию сняли параллельно; на
    этот случай пересчитываем лимит по фактическому остатку и пробуем ещё раз.
    Двух попыток достаточно: вторая идёт от значения, прочитанного уже внутри
    транзакции. Не сошлось и там — клики не оплачены, это честнее, чем выдать
    награду в долг.

    В книгу не пишем: расход энергии — обратная сторона регена, а он в книгу
    не идёт (см. LEDGERED_PARTIAL)."""
    per = cfg.ENERGY_PER_CLICK
    with db.tx():
        for _ in range(2):
            if clicks <= 0:
                return 0
            need = clicks * per
            if db.exec("UPDATE users SET energy = energy - ?, "
                       "user_revision = user_revision + 1 "
                       "WHERE user_id = ? AND energy >= ?",
                       (need, user_id, need)):
                return clicks
            row = db.q1("SELECT energy FROM users WHERE user_id = ?", (user_id,))
            if not row:
                return 0
            clicks = int(min(clicks, row["energy"] // per))
        return 0


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
    """Тап по золотой печеньке. Возвращает применённый эффект или ValueError.

    Печеньку гасит УСЛОВНЫЙ UPDATE, и он же выдаёт разрешение на награду: раньше
    проверка «не истекла» стояла над прочитанным словарём, а гашение шло
    безусловным UPDATE — два тапа вплотную (а по золотой печеньке тапают именно
    так) оба проходили проверку и оба платили. Эффект берём из RETURNING, а не
    из словаря: он мог смениться следующим спавном."""
    now = time.time()
    uid = user["user_id"]
    with db.tx():
        row = db.q1w("UPDATE users SET golden_expires_at = 0, "
                     "user_revision = user_revision + 1 "
                     "WHERE user_id = ? AND COALESCE(golden_expires_at, 0) > ? "
                     "RETURNING golden_effect, level",
                     (uid, now))
        if row is None:
            raise ValueError("err_golden_gone")
        effect = row["golden_effect"] or "chain"
        if effect == "frenzy":
            e = cfg.GOLDEN_EFFECTS["frenzy"]
            db.exec("INSERT INTO boosts (user_id, boost_key, expires_at, source) "
                    "VALUES (?, ?, ?, 'golden')",
                    (uid, "golden_frenzy", now + e["seconds"]))
            return {"effect": "frenzy", "mult": e["mult"], "seconds": e["seconds"]}
        e = cfg.GOLDEN_EFFECTS["chain"]
        bonus = max(passive_per_hour(uid) * e["passive_hours"],
                    e["min_per_level"] * row["level"])
        add_cookies(uid, bonus)
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
    """Сбрасывает прогресс за постоянный множитель. Возвращает {gained, points, multiplier}.

    Право на перерождение перепроверяется ВНУТРИ транзакции и по свежей строке:
    словарь, пришедший из ручки, к этому моменту уже устарел (между чтением и
    записью успевает встать сбор фермы, клик, награда). Раньше проверка стояла
    только снаружи, и два нажатия вплотную оба её проходили — очки начислялись
    дважды за один и тот же total_earned.

    Гарантию даёт не проверка, а условие `prestige_count = ?`: если параллельный
    запрос переродил профиль первым, счётчик уже другой, наш UPDATE не проходит
    вовсе, и мы отвечаем err_prestige_early вместо второго начисления.

    Обнуление печенек, откат XP и выдача очков — движения в книге, а не тихий
    UPDATE. Раньше все три колонки писались напрямую: сверка видела расхождение
    на весь баланс, и после первого же перерождения игрок навсегда выпадал из
    контроля за экономикой. Суммы берём фактические (после − до), а не
    расчётные, — иначе книга описывала бы не то, что произошло."""
    uid = user["user_id"]
    with db.tx():
        before = db.get_user(uid)
        if not before:
            raise ValueError("err_no_user")
        st = prestige_state(before)
        if not st["can_prestige"]:
            raise ValueError("err_prestige_early")
        gain = st["gain_available"]
        new_points = int(before["prestige_points"]) + gain
        # Уровень сохраняется частично. Полный откат на 1-й означал заново
        # проходить все req_level зданий и предметов, а множитель престижа этого
        # не ускорял — перерождаться было невыгодно ни в какой момент.
        kept_level = prestige_kept_level(before["level"])
        kept_xp = cfg.xp_for_level(kept_level)
        now = time.time()
        # сохраняем: скины, ачивки, рефералов, стрик, БП сезона, покупки Stars, бусты
        db.exec("DELETE FROM board WHERE user_id = ?", (uid,))
        db.exec("DELETE FROM farm WHERE user_id = ?", (uid,))
        db.exec("DELETE FROM upgrades WHERE user_id = ?", (uid,))
        # незавершённые заказы выписаны под старый доход: цель «заработай 60M»
        # недостижима на 1 уровне, а награда по ней была бы читом
        db.exec("DELETE FROM orders WHERE user_id = ? AND status != 'done'", (uid,))
        row = db.q1w(
            "UPDATE users SET cookies = 0, click_level = 1, "
            "level = ?, xp = ?, "
            # энергия наливается по потолку УЖЕ БЕЗ апгрейдов: их строки удалены
            # выше в этой же транзакции, поэтому cfg.max_energy и есть новый бак
            "energy = ?, energy_updated_at = ?, "
            "passive_collected_at = ?, farm_collected_at = ?, combo_mult = 1, "
            "prestige_points = ?, prestige_count = prestige_count + 1, "
            # доска стёрта выше в этой же транзакции — версию двигаем вместе
            "user_revision = user_revision + 1, board_revision = board_revision + 1 "
            "WHERE user_id = ? AND prestige_count = ? "
            "RETURNING cookies, xp, prestige_points",
            (kept_level, kept_xp, cfg.max_energy(kept_level), now, now, now,
             new_points, uid, before["prestige_count"]))
        if row is None:                     # кто-то переродился первым
            raise ValueError("err_prestige_early")
        op = f"prestige:{uid}:{before['prestige_count']}"
        for currency, moved, after in (
                ("cookies", row["cookies"] - before["cookies"], row["cookies"]),
                ("xp", row["xp"] - before["xp"], row["xp"]),
                ("prestige_points", gain, row["prestige_points"])):
            if moved:
                economy.record(uid, currency, moved, "prestige_reset", after, op)
    return {"gained": gain, "points": int(new_points),
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


def collect_all(user_id: int, now: float | None = None) -> dict:
    """Собирает ферму + пассивку доски и возвращает СВЕЖЕГО юзера.
    Обязателен перед любой проверкой «хватает ли печенек»: иначе сервер
    сравнивает цену со вчерашним балансом, а игрок видит уже натикавший —
    «деньги есть, а купить не даёт».

    Обоим сборам отдаём ОДНО «сейчас»: у каждого своя отметка времени, и если
    брать time.time() дважды, отметки разъезжаются на длительность первого
    сбора. Разница копится при каждом сборе и на активной игре (сбор идёт на
    каждый батч кликов) заметно расходится с реальным временем."""
    now = time.time() if now is None else now
    u = db.get_user(user_id)
    if not u:
        return u
    collect_passive(u, now)
    collect_farm(u, now)
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


def _consume_recipe(user_id: int, now: float | None = None) -> float:
    """Забирает множитель закваски и снимает её (одноразовая).
    Возвращает множитель: 1.0, если рано, подгорело или закваску уже съели.

    Зовётся ТОЛЬКО из collect_farm и только внутри его транзакции. Строку читаем
    сами, а не принимаем словарём: закваску могли поставить или съесть между
    входом в ручку и этим моментом, и множитель посчитался бы по чужому тесту.

    Снимает закваску условный UPDATE по (recipe_key, recipe_started_at): два
    сбора вплотную оба видели готовое тесто и оба умножали свой доход на одну и
    ту же закваску. Проигравший возвращает 1.0."""
    now = time.time() if now is None else now
    row = db.q1("SELECT user_id, recipe_key, recipe_started_at "
                "FROM users WHERE user_id = ?", (user_id,))
    if not row:
        return 1.0
    st = recipe_status(row, now)
    if st["state"] in ("none", "rising"):
        return 1.0          # нет закваски или рано вернулся — тесто ещё стоит
    if db.exec("UPDATE users SET recipe_key = NULL, recipe_started_at = 0, "
               "user_revision = user_revision + 1 "
               "WHERE user_id = ? AND recipe_key = ? AND recipe_started_at = ?",
               (user_id, row["recipe_key"], row["recipe_started_at"])) == 0:
        return 1.0          # закваску съел параллельный сбор
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


def collect_farm(user: dict, now: float | None = None) -> float:
    """Начисляет накопленный доход фермы, возвращает сколько упало.

    Отметку времени двигает compare-and-set: `WHERE farm_collected_at = ?` по
    значению, прочитанному в этой же транзакции. Раньше отметка читалась из
    словаря, пришедшего в ручку, а писалась безусловно — два запроса вплотную
    (а /api/state, /api/auth, /api/farm и каждый батч кликов зовут сбор) оба
    видели один и тот же интервал и оба его оплачивали. Проигравший CAS не
    доплачивает и не переспрашивает: интервал уже оплачен, он не наш.

    Пустая отметка (первый сбор) только ставится — платить за время до
    регистрации нечем и незачем.

    При seconds <= 0 отметка НЕ переписывается. Раньше переписывалась, и это
    отматывало её назад по локальным часам: у воркера с часами на секунду
    вперед сбор ставил будущее время, следующий сбор на другом воркере видел
    отрицательный интервал и откручивал отметку к своему «сейчас» — сдвиг NTP
    между воркерами превращался в повторную оплату одних и тех же секунд.

    Закваска съедается ВНУТРИ той же транзакции. Раньше _consume_recipe стоял
    строкой выше `with db.tx()`, а db.update_user на нулевой глубине коммитит
    сам: множитель списывался отдельной транзакцией от дохода, который он
    умножает, и сбой между ними съедал закваску впустую."""
    now = time.time() if now is None else now
    uid = user["user_id"]
    with db.tx():
        # кап и отметку берём СВЕЖИМИ: словарь ручки к этому моменту устарел,
        # а от offline_bonus_hours зависит, сколько часов вообще оплачивать
        row = db.q1("SELECT farm_collected_at, offline_bonus_hours "
                    "FROM users WHERE user_id = ?", (uid,))
        if not row:
            return 0.0
        prev = row["farm_collected_at"]
        if not prev:                       # первый сбор: только ставим метку
            db.exec("UPDATE users SET farm_collected_at = ? WHERE user_id = ? "
                    "AND COALESCE(farm_collected_at, 0) = 0", (now, uid))
            return 0.0
        seconds = min(farm_offline_cap_hours(row) * 3600, now - prev)
        if seconds <= 0:
            return 0.0
        if db.exec("UPDATE users SET farm_collected_at = ? "
                   "WHERE user_id = ? AND farm_collected_at = ?",
                   (now, uid, prev)) == 0:
            return 0.0                     # интервал оплатил параллельный сбор
        income = farm_cps(uid) * seconds
        # закваска умножает ТОЛЬКО оффлайн-доход и только если игрок вернулся в
        # окно готовности; съедается один раз — на ферме, а не на каждом сборе
        if seconds > 60:
            income *= _consume_recipe(uid, now)
        if income > 0:
            # токен привязан к началу оплаченного интервала: ретрай ручки после
            # потерянного ответа не платит второй раз за те же секунды
            add_cookies(uid, income, operation_id=f"farm:{uid}:{int(prev * 1000)}",
                        reason="passive_farm")
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
            "WHERE user_id = ? RETURNING cookies, season_id, cookie_debt",
            (amount, earn, earn, user_id))
        if row is None:
            raise ValueError("err_no_user")
        economy.record(user_id, "cookies", amount, reason, row["cookies"], op,
                       ref_type=ref_type, ref_id=ref_id,
                       counts_earned=1 if earn else 0, season_id=row["season_id"])
        # долг после возврата Stars гасится из ближайшего дохода. Лишний
        # стейтмент выполняется только если долг реально есть: у игрока без
        # возвратов RETURNING вернул ноль, и путь стоит одну проверку в питоне
        balance = row["cookies"]
        if row["cookie_debt"] > 0 and amount > 0:
            balance = _settle_debt(user_id, op, balance)
        if earn:
            # честный заработок кормит дневное задание "заработай N" и заказ пекарни
            quest_progress(user_id, "earned", amount)
            order_progress(user_id, "earned", amount)
    return balance


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


def bump_board(user_id: int) -> None:
    """Отмечает, что доска изменилась.

    Клиент присылает увиденную версию доски заголовком X-Board-Revision, и по
    расхождению его действие отбивается 409 вместо того, чтобы примениться к
    ЧУЖОЙ раскладке: перетаскивание адресуется номерами клеток, а не самими
    печеньками, поэтому «слить 3 и 4», отправленное со старого экрана, слило бы
    в этих клетках то, что там оказалось потом. Двух сессий на одном аккаунте
    достаточно, чтобы это случилось (телефон + десктоп — обычное дело)."""
    db.exec("UPDATE users SET board_revision = board_revision + 1 WHERE user_id = ?",
            (user_id,))


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
            bump_board(user_id)
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

def collect_passive(user: dict, now: float | None = None) -> float:
    """Начисляет накопленный пассивный доход доски, возвращает сколько упало.

    Отметка двигается compare-and-set'ом, как у фермы (см. collect_farm): чтение
    и запись в одной транзакции, проигравший не платит."""
    now = time.time() if now is None else now
    uid = user["user_id"]
    with db.tx():
        row = db.q1("SELECT passive_collected_at, offline_bonus_hours "
                    "FROM users WHERE user_id = ?", (uid,))
        if not row:
            return 0.0
        prev = row["passive_collected_at"]
        if not prev:
            db.exec("UPDATE users SET passive_collected_at = ? WHERE user_id = ? "
                    "AND COALESCE(passive_collected_at, 0) = 0", (now, uid))
            return 0.0
        hours = min(passive_offline_cap_hours(row), (now - prev) / 3600)
        if hours <= 0:
            return 0.0
        if db.exec("UPDATE users SET passive_collected_at = ? "
                   "WHERE user_id = ? AND passive_collected_at = ?",
                   (now, uid, prev)) == 0:
            return 0.0
        income = passive_per_hour(uid) * hours
        if income > 0:
            add_cookies(uid, income,
                        operation_id=f"passive:{uid}:{int(prev * 1000)}",
                        reason="passive_board")
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
       вообще не ускорял бы набор XP (то есть был бы бессмыслен).

    Премия за рекорд тира (record_multiplier) — исключение и входит СЮДА: она
    относится к самой доске, а не к внешним бустам, и цена спавна обязана расти
    вместе с ней, иначе окупаемость предмета падает с 12 часов до полутора."""
    rows = db.q("SELECT item_level FROM board WHERE user_id = ?", (user_id,))
    user = db.get_user(user_id)
    record = (user["best_item_level"] or 0) if user else 0
    return sum(cfg.passive_income_per_hour(r["item_level"])
               for r in rows) * cfg.record_multiplier(record)


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


def _orders_config_rev() -> str:
    """Отпечаток набора заказов. Пишется в строку при выписке.

    Конфиг меняется между релизами, а заказ живёт между сессиями. По этой метке
    видно, по каким правилам заказ выписан: разбирать жалобу «мне обещали
    другое» иначе нечем, а миграция не может отличить свежую строку от строки,
    выписанной прошлым набором шаблонов."""
    blob = json.dumps([cfg.ORDER_TEMPLATES, cfg.ORDER_REWARD_HOURS,
                       cfg.ORDER_REWARD_MIN, cfg.ORDER_BP_XP],
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


ORDER_CONFIG_REV = _orders_config_rev()


def backfill_orders_config():
    """Приводит заказы, выписанные до версионирования, к новым правилам.

    Строки с шаблоном, которого в конфиге больше нет, удаляются: раньше они
    доживали до вкладки пекарни и рисовались там отсутствующим ключом i18n —
    заказ без названия, который нельзя ни сдать, ни понять. Дубли активных
    заказов схлопывает dedupe:orders_active в db.py, он идёт раньше.

    Меткой конфига помечаются только строки без неё: перештамповывать чужой
    отпечаток нельзя, он для этого и нужен."""
    if not db._migration("backfill:orders_version"):
        return
    known = list(cfg.ORDER_TEMPLATES)
    holes = ", ".join("?" * len(known))
    dropped = db.exec(f"DELETE FROM orders WHERE template NOT IN ({holes})", known)
    db.exec("UPDATE orders SET version = 1 WHERE version IS NULL OR version < 1")
    stamped = db.exec("UPDATE orders SET config_rev = ? WHERE config_rev IS NULL",
                      (ORDER_CONFIG_REV,))
    db._mark("backfill:orders_version")
    if dropped or stamped:
        print(f"[*] Миграция: заказы — {dropped} с исчезнувшим шаблоном удалено, "
              f"{stamped} помечено версией конфига")


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
    """Дозаполняет тройку офферов: по одному на каждую сложность.

    Раньше было DELETE всех офферов + три INSERT, а сеялся выбор шаблонов из
    `int(time.time())`. Две ручки, зашедшие в одну секунду (а /api/orders зовёт
    выписку сам, стоит открыть вкладку), получали ОДИН И ТОТ ЖЕ набор шаблонов,
    удаляли офферы друг друга и вставляли шесть строк — игрок видел то три
    заказа, то шесть, а взять мог тот, которого уже нет.

    Теперь номер выписки берётся из базы (`orders_offer_gen`), то есть у
    параллельных вызовов он РАЗНЫЙ, а вставка идёт `ON CONFLICT DO NOTHING` по
    частичному уникальному индексу: занятый слот остаётся за тем, кто успел
    первым, проигравший просто ничего не делает. DELETE больше не нужен —
    отсутствующие слоты дозаполняются, присутствующие не трогаются."""
    uid = user["user_id"]
    income = hourly_income(uid) if income is None else income
    now = time.time()
    with db.tx():
        gen = db.q1w("UPDATE users SET orders_offer_gen = orders_offer_gen + 1 "
                     "WHERE user_id = ? RETURNING orders_offer_gen", (uid,))
        rnd = random.Random(f"{uid}:{gen['orders_offer_gen'] if gen else 0}")
        for slot, diff in enumerate((1, 2, 3), start=1):
            keys = sorted(k for k, t in cfg.ORDER_TEMPLATES.items()
                          if t["difficulty"] == diff)
            key = rnd.choice(keys)
            p = _order_params(user, key, income)
            db.exec(
                "INSERT INTO orders (user_id, slot, template, metric, goal, progress, "
                "reward_cookies, reward_bp_xp, status, created_at, config_rev) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'offer', ?, ?) "
                "ON CONFLICT (user_id, slot) WHERE status = 'offer' DO NOTHING",
                (uid, slot, key, p["metric"], p["goal"],
                 p["reward_cookies"], p["reward_bp_xp"], now, ORDER_CONFIG_REV))


def _refresh_offers(user: dict, income: float):
    """Непринятые офферы пересчитываются под текущий доход и уровень: игрок мог
    вырасти (или сделать престиж) с момента их выписки. Цель активного заказа
    НЕ трогаем — иначе накопленный прогресс потерял бы смысл.

    Оффер с исчезнувшим из конфига шаблоном удаляется, а не пропускается: он
    остаётся в тройке, рисуется на вкладке пустым ключом i18n и занимает слот,
    который иначе дозаполнился бы живым заказом."""
    for o in db.q("SELECT id, template FROM orders WHERE user_id = ? AND status = 'offer'",
                  (user["user_id"],)):
        if o["template"] not in cfg.ORDER_TEMPLATES:
            db.exec("DELETE FROM orders WHERE id = ? AND status = 'offer'", (o["id"],))
            continue
        p = _order_params(user, o["template"], income)
        db.exec("UPDATE orders SET goal = ?, reward_cookies = ?, reward_bp_xp = ?, "
                "config_rev = ? WHERE id = ? AND status = 'offer'",
                (p["goal"], p["reward_cookies"], p["reward_bp_xp"],
                 ORDER_CONFIG_REV, o["id"]))


def _order_unreachable(user: dict, o: dict, income: float) -> bool:
    """Заказ стал невыполнимым: цель зафиксирована по старому прогрессу, а
    прогресс сбросился (престиж). «Сделай печеньку 23 уровня» при максимуме 8
    не закрыть никогда, а активный заказ всего один — вкладка пекарни намертво
    блокируется вместе с шагом чеклиста (фидбек).

    Если игрок с момента взятия не потерял ни уровня, ни дохода, проверять
    нечего: цель считалась ровно от этих величин, значит она достижима. Пустой
    снимок (заказ взят до версионирования) означает «пересчитать по текущим»."""
    if o["progress"] >= o["goal"]:
        return False
    if (o["taken_level"] is not None and o["taken_income"] is not None
            and user["level"] >= o["taken_level"] and income >= o["taken_income"]):
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
    # id и version уезжают клиенту и возвращаются на сдаче/отказе: без них
    # «сдать» адресовало бы просто «активный заказ», а это уже мог быть другой
    return {"id": o["id"], "version": o["version"],
            "slot": o["slot"], "template": o["template"], "metric": o["metric"],
            "goal": o["goal"], "progress": min(o["progress"], o["goal"]),
            "done": o["progress"] >= o["goal"],
            "reward_cookies": reward, "reward_bp_xp": bp_xp,
            "difficulty": _order_difficulty(o["template"])}


def _drop_stale_order(user: dict, o: dict) -> float:
    """Снимает недостижимый заказ и возвращает вложенное в него. Отдаёт сумму
    компенсации (0, если возвращать нечего).

    Заказ снимает СЕРВЕР, а не игрок: цель стала недостижимой после престижа.
    Печеньки, потраченные на спавны и здания ради этого заказа, при этом просто
    исчезали — игрок платил за задание, которое у него отобрали. Возвращаем их.

    DELETE с проверкой rowcount: два параллельных чтения состояния оба видят
    мёртвый заказ, но снимает его один, и только он платит компенсацию.
    Токен операции привязан к id строки — ретрай ручки не заплатит второй раз."""
    uid = user["user_id"]
    with db.tx():
        if db.exec("DELETE FROM orders WHERE id = ? AND status = 'active'",
                   (o["id"],)) == 0:
            return 0.0
        back = max(0.0, o["invested"] or 0.0)
        if back:
            add_cookies(uid, back, count_earned=False,
                        operation_id=f"order_comp:{o['id']}",
                        reason="order_compensation",
                        ref_type="order", ref_id=str(o["id"]))
    track(uid, "order_stale_dropped")
    return back


def orders_state(user: dict) -> dict:
    uid = user["user_id"]
    day = _utc_day(time.time())
    used = user["orders_day_count"] if user["orders_day"] == day else 0
    left = max(0, cfg.ORDERS_PER_DAY - used)
    income = hourly_income(uid)
    active = db.q1("SELECT * FROM orders WHERE user_id = ? AND status = 'active'", (uid,))
    # мёртвый заказ снимаем сами и БЕСПЛАТНО (дневной лимит не тратится):
    # игрок в него не виноват, а иначе пекарня заблокирована навсегда
    compensated = 0.0
    if active and _order_unreachable(user, active, income):
        compensated = _drop_stale_order(user, active)
        active = None
    offers = []
    if not active and left > 0:
        # сперва чистка и пересчёт, потом дозаполнение: refresh удаляет офферы с
        # исчезнувшим шаблоном, и без этого порядка игрок увидел бы два заказа
        # вместо трёх до следующего запроса
        _refresh_offers(user, income)
        offers = db.q("SELECT * FROM orders WHERE user_id = ? AND status = 'offer' "
                      "ORDER BY slot", (uid,))
        if len(offers) != 3:
            _gen_order_offers(user, income)
            offers = db.q("SELECT * FROM orders WHERE user_id = ? AND status = 'offer' "
                          "ORDER BY slot", (uid,))
    return {"active": _pack_order(active, income) if active else None,
            "offers": [_pack_order(o, income) for o in offers],
            "left_today": left, "per_day": cfg.ORDERS_PER_DAY,
            # чтобы вкладка могла сказать, за что пришли печеньки
            "compensated": compensated}


def _active_order(user_id: int, order_id: int | None = None,
                  version: int | None = None) -> dict:
    """Активный заказ игрока — с проверкой, что клиент имеет в виду ЕГО.

    Ручки сдачи и отказа раньше работали с «тем заказом, который вернул SELECT».
    Между показом экрана и нажатием кнопки активный заказ мог смениться: старый
    сняли как недостижимый, игрок со второй сессии взял новый — и «сдать» сдало
    бы совсем другое задание. Клиент присылает (id, version), расхождение —
    409 вместо тихой подмены.

    Ни id, ни версии нет — проверка не включается: сборки Mini App, которые их
    ещё не знают, обязаны продолжать играть."""
    row = db.q1("SELECT * FROM orders WHERE user_id = ? AND status = 'active'",
                (user_id,))
    if not row:
        raise ValueError("err_no_item")
    if order_id is not None and row["id"] != order_id:
        raise economy.ConflictError(user_id)
    if version is not None and row["version"] != version:
        raise economy.ConflictError(user_id)
    return row


def take_order(user: dict, slot: int, order_id: int | None = None,
               version: int | None = None) -> dict:
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
    if order_id is not None and row["id"] != order_id:
        # слот тот же, а заказ в нём уже другой: офферы пересобрались, пока
        # игрок смотрел на старый экран
        raise economy.ConflictError(uid)
    if row["template"] not in cfg.ORDER_TEMPLATES:
        # шаблон исчез из конфига: раньше такой оффер молча становился активным
        # и вставал на вкладке безымянным заданием, которое нельзя выполнить
        db.exec("DELETE FROM orders WHERE id = ? AND status = 'offer'", (row["id"],))
        raise ValueError("err_no_item")
    # цель фиксируем по СЕГОДНЯШНЕМУ доходу: оффер мог пролежать с прошлой сессии
    income = hourly_income(uid)
    p = _order_params(user, row["template"], income)
    with db.tx():
        # version в условии: два /orders/take вплотную иначе оба «взяли бы» один
        # оффер, а дневной лимит списался бы дважды
        if db.exec("UPDATE orders SET goal = ?, reward_cookies = ?, reward_bp_xp = ?, "
                   "status = 'active', version = version + 1, config_rev = ?, "
                   "taken_level = ?, taken_income = ?, invested = 0 "
                   "WHERE id = ? AND status = 'offer' AND version = ?",
                   (p["goal"], p["reward_cookies"], p["reward_bp_xp"],
                    ORDER_CONFIG_REV, user["level"], income,
                    row["id"], row["version"])) == 0:
            raise economy.ConflictError(uid)
        row = dict(row, goal=p["goal"], version=row["version"] + 1)
        db.exec("DELETE FROM orders WHERE user_id = ? AND status = 'offer'", (uid,))
    track(uid, "order_take")
    return _pack_order(dict(row, status="active"), income)


def abandon_order(user: dict, order_id: int | None = None,
                  version: int | None = None) -> dict:
    """Отказ от активного заказа по своей воле. В отличие от снятия мёртвого
    заказа стоит одну попытку из дневного лимита — иначе можно было бы
    бесконечно перебирать офферы в поисках удобного.

    Компенсации тут нет намеренно: заказ бросает сам игрок, вложенное — его
    решение, а не отобранное сервером."""
    uid = user["user_id"]
    row = _active_order(uid, order_id, version)
    day = _utc_day(time.time())
    with db.tx():
        if db.exec("DELETE FROM orders WHERE id = ? AND status = 'active'",
                   (row["id"],)) == 0:
            raise ValueError("err_no_item")   # успел сняться параллельно
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


def order_progress(user_id: int, metric: str, amount: float, spent: float = 0.0):
    """Прогресс активного заказа. make_item — «лучший достигнутый уровень».

    Адресуется по id найденной строки, а не условием
    `status = 'active' AND metric = ?` без LIMIT: пока уникальность активного
    заказа не была фактом базы, такой UPDATE двигал ОБА заказа, если их
    оказывалось два, и один из них потом было нечем объяснить.

    `spent` — печеньки, которые игрок вложил в этот заказ (спавн, здание). Они
    копятся в строке, чтобы вернуть их, если заказ снимет сервер."""
    if amount <= 0:
        return
    row = db.q1("SELECT id, metric FROM orders WHERE user_id = ? AND status = 'active'",
                (user_id,))
    if not row or row["metric"] != metric:
        return
    setter = ("progress = MAX(progress, ?)" if metric == "make_item"
              else "progress = progress + ?")
    db.exec(f"UPDATE orders SET {setter}, invested = invested + ? "
            f"WHERE id = ? AND status = 'active'",
            (amount, max(0.0, spent), row["id"]))


def claim_order(user: dict, order_id: int | None = None,
                version: int | None = None) -> dict:
    uid = user["user_id"]
    row = _active_order(uid, order_id, version)
    if row["progress"] < row["goal"]:
        raise ValueError("err_not_done")
    day = _utc_day(time.time())
    first = user["orders_completed"] == 0
    # платим по ТЕКУЩЕМУ доходу: хранимая сумма могла быть выписана до престижа
    reward, bp_xp = order_reward(row["template"], hourly_income(uid))
    with db.tx():
        # WHERE status = 'active' + version + rowcount: два параллельных клейма
        # одного заказа иначе оба прошли бы проверку выше и заплатили дважды
        if db.exec("UPDATE orders SET status = 'done', reward_cookies = ?, "
                   "reward_bp_xp = ?, version = version + 1 "
                   "WHERE id = ? AND status = 'active' AND version = ?",
                   (reward, bp_xp, row["id"], row["version"])) == 0:
            raise ValueError("err_claimed")
        # токен по строке заказа: ретрай ручки после потерянного ответа не
        # заплатит награду второй раз
        add_cookies(uid, reward, count_earned=False,
                    operation_id=f"order_claim:{row['id']}",
                    reason="order_reward", ref_type="order", ref_id=str(row["id"]))
        # XP уровня, а не только батл-пасса (см. ORDER_XP_SHARE). Сложность
        # берём через _order_difficulty: шаблон в строке мог остаться от старой
        # версии конфига, и обращение по ключу уронило бы клейм заказа.
        add_xp(uid, cfg.order_reward_xp(_order_difficulty(row["template"]),
                                        user["level"]), bp_xp)
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
    побит (подавляющее большинство мерджей).

    Рекорд двигается compare-and-set'ом: платим ровно за тот диапазон, который
    сдвинули мы. Раньше рекорд писался безусловно поверх прочитанного значения —
    два мерджа вплотную (пачки мерджей на активной игре — норма) оба видели
    старый рекорд и оба платили XP за один и тот же тир. Это основной источник
    XP уровня, то есть дублировалась вся ветка прогресса.

    Проигравший CAS повторяет чтение: победитель мог поднять рекорд НИЖЕ нашего
    (мерджи приходят вразнобой), и тогда за остаток диапазона всё ещё должны."""
    uid = user["user_id"]
    with db.tx():
        for _ in range(3):
            cur = db.q1("SELECT best_item_level FROM users WHERE user_id = ?", (uid,))
            if not cur:
                return None
            best = cur["best_item_level"] or 0
            if item_level <= best:
                return None
            if db.exec("UPDATE users SET best_item_level = ?, "
                       "user_revision = user_revision + 1 "
                       "WHERE user_id = ? AND COALESCE(best_item_level, 0) = ?",
                       (item_level, uid, best)):
                break
        else:
            return None                      # рекорд всё время уезжал — не платим
        levels = range(max(best + 1, 2), item_level + 1)
        xp = sum(cfg.first_item_xp(l) for l in levels)
        bp_xp = sum(cfg.first_item_bp_xp(l) for l in levels)
        cookies = cfg.scaled_reward(0, "item_record", hourly_income(uid))
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
    обнулялся — альбом можно было не добить никогда.

    Счётчик pity двигается ОТНОСИТЕЛЬНО (`shiny_pity + 1`), а не записью
    посчитанного значения: пачка мерджей читала одно и то же число и писала
    одно и то же, счётчик стоял на месте и гарантия дропа не наступала никогда.
    Обнуляет его только тот вызов, чья вставка в альбом действительно прошла."""
    uid = user["user_id"]
    pity = (user["shiny_pity"] or 0) + 1
    if pity < cfg.SHINY_PITY and random.random() >= cfg.SHINY_CHANCE:
        _bump_pity(uid)
        return None
    owned = {r["item_level"] for r in
             db.q("SELECT item_level FROM collection WHERE user_id = ?", (uid,))}
    target = item_level if item_level not in owned else next(
        (l for l in range(item_level - 1, 0, -1) if l not in owned), None)
    if target is None:  # всё до этого уровня собрано — pity сохраняем на будущее
        _bump_pity(uid)
        return None
    with db.tx():
        if db.exec("INSERT INTO collection (user_id, item_level, obtained_at) "
                   "VALUES (?, ?, ?) "
                   "ON CONFLICT (user_id, item_level) DO NOTHING",
                   (uid, target, time.time())) == 0:
            _bump_pity(uid)                  # уровень успели собрать параллельно
            return None
        db.exec("UPDATE users SET shiny_pity = 0, "
                "user_revision = user_revision + 1 WHERE user_id = ?", (uid,))
    track(uid, "shiny_drop", target)
    return target


def _bump_pity(user_id: int):
    """+1 к счётчику гарантированного дропа, относительным UPDATE."""
    db.exec("UPDATE users SET shiny_pity = COALESCE(shiny_pity, 0) + 1, "
            "user_revision = user_revision + 1 WHERE user_id = ?", (user_id,))


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
        # частичный индекс -> и в ON CONFLICT нужно то же условие: платёж без
        # charge_id ни с чем не конфликтует и должен вставиться
        "INSERT INTO purchases (user_id, item_key, stars_amount, "
        "tg_payment_id, status, created_at) VALUES (?, ?, ?, ?, 'unmatched', ?) "
        "ON CONFLICT (tg_payment_id) WHERE tg_payment_id IS NOT NULL DO NOTHING",
        (user_id, (item_key or payload or "")[:64], amount,
         charge_id or None, time.time()))


def _claw_back_cookies(user_id: int, amount: float, charge_id: str = "") -> float:
    """Снимает выданные печеньки при возврате Stars. Возвращает снятое.

    Чего не хватило на балансе — уходит в долг, а не пропадает. Раньше списание
    делалось через max(0, ...): игрок, успевший потратить полученное, отдавал
    сколько было и оставался в плюсе на разницу — покупка за Stars превращалась
    в бесплатные печеньки через возврат.

    Долг — отдельная валюта книги: открытие и погашение сходятся с колонкой
    cookie_debt так же, как остальные валюты. Записать нехватку строкой по
    cookies было нельзя — движения баланса на эту сумму не было, и сверка по
    печенькам разошлась бы навсегда.

    Сумма списания фиксируется compare-and-set'ом по балансу: и снятое, и долг
    считаются от того значения, которое мы действительно сдвинули, — иначе в
    книгу ушло бы одно число, а с колонки списалось бы другое."""
    amount = economy._sane(amount, "refund_clawback")
    if amount <= 0:
        return 0.0
    op = f"refund:{charge_id}" if charge_id \
        else economy.auto_op(user_id, "stars_refund_clawback")
    with db.tx():
        # обе валюты, а не только печеньки: у игрока с нулевым балансом возврат
        # уходит в долг ЦЕЛИКОМ, строки по cookies не появляется вовсе — и
        # проверка по одной валюте пропустила бы повтор, удвоив долг
        if charge_id and (economy.already_recorded(op, "cookies")
                          or economy.already_recorded(op, "cookie_debt", 1)):
            return 0.0                       # возврат уже проведён
        for _ in range(5):
            row = db.q1("SELECT cookies FROM users WHERE user_id = ?", (user_id,))
            if not row:
                return 0.0
            taken = min(amount, max(0.0, row["cookies"]))
            debt = amount - taken
            after = db.q1w(
                "UPDATE users SET cookies = cookies - ?, "
                "cookie_debt = cookie_debt + ?, "
                "user_revision = user_revision + 1 "
                "WHERE user_id = ? AND cookies = ? "
                "RETURNING cookies, cookie_debt",
                (taken, debt, user_id, row["cookies"]))
            if after is not None:
                break
        else:
            # баланс уезжал пять раз подряд: на SQLite невозможно (BEGIN
            # IMMEDIATE выстраивает писателей), на PostgreSQL — только под
            # шквалом параллельных начислений. Рвём транзакцию целиком: возврат
            # останется неоформленным и будет виден в /api/admin/payments,
            # а списать «примерно столько» нельзя
            raise ValueError("err_refund_busy")
        if taken:
            economy.record(user_id, "cookies", -taken, "stars_refund_clawback",
                           after["cookies"], op, ref_type="charge",
                           ref_id=charge_id or None)
        if debt:
            economy.record(user_id, "cookie_debt", debt, "refund_debt_opened",
                           after["cookie_debt"], op, seq=1, ref_type="charge",
                           ref_id=charge_id or None)
    return taken


def _settle_debt(user_id: int, op: str, credited_balance: float) -> float:
    """Гасит долг из первого же положительного начисления. Зовётся из add_cookies.
    Возвращает баланс ПОСЛЕ погашения — его и должен вернуть вызывающий.

    Возврат Stars может забрать больше, чем есть на балансе (игрок успел
    потратить), и остаток висит долгом. Гасить его отдельной ручкой нельзя —
    игрок просто не станет её звать; поэтому долг съедает ближайший доход.

    Списываем LEAST(долг, баланс): доход меньше долга гасит его частично и
    никогда не уводит баланс в минус."""
    row = db.q1w(f"UPDATE users SET cookies = cookies - {db.LEAST}(cookie_debt, cookies), "
                 f"cookie_debt = cookie_debt - {db.LEAST}(cookie_debt, cookies), "
                 f"user_revision = user_revision + 1 "
                 f"WHERE user_id = ? AND cookie_debt > 0 AND cookies > 0 "
                 f"RETURNING cookies, cookie_debt", (user_id,))
    if row is None:
        return credited_balance
    paid = credited_balance - row["cookies"]
    if paid <= 0:
        return row["cookies"]
    # два движения одной операцией: сколько ушло с баланса и на сколько
    # уменьшился долг. Обе колонки после этого сходятся с книгой
    economy.record(user_id, "cookies", -paid, "debt_settlement",
                   row["cookies"], op, seq=2)
    economy.record(user_id, "cookie_debt", -paid, "debt_settlement",
                   row["cookie_debt"], op, seq=3)
    return row["cookies"]


def _revoke_purchase_effect(user_id: int, item_key: str,
                            payload_json: str | None = None,
                            instance_id: str | None = None,
                            charge_id: str = ""):
    """Откат эффекта при возврате звёзд. Вызывается внутри db.tx().

    Работает по записи о выдаче (granted_payload), а не по пересчёту «сколько
    выдали бы сейчас». Пересчёт был сломан без всякой конкуренции: ящик за 300⭐,
    купленный при доходе 2 500/ч, выдавал 25 000 печенек, а при доходе 10 млн/ч
    возврат пытался снять 100 000 000 и обнулял банк целиком.

    payload_json = NULL — покупка сделана до появления записи. Для неё остаётся
    прежний путь: пересчёт по конфигу и снятие одной строки буста. Гарантий
    точности тут нет и быть не может, но окно закрытое — только платежи,
    выданные до этого деплоя."""
    effect = cfg.SHOP_ITEMS.get(item_key, {})[3] \
        if item_key in cfg.SHOP_ITEMS else None
    if not effect:
        return
    if not db.get_user(user_id):
        return
    try:
        payload = json.loads(payload_json) if payload_json else None
    except (ValueError, TypeError):
        payload = None                      # битый json — как будто его нет
    kind = (payload or {}).get("type") or effect["type"]

    if kind == "cookies":
        amount = (payload or {}).get("amount")
        if amount is None:                  # покупка до миграции
            amount = max(effect.get("min_amount", 0),
                         hourly_income(user_id) * effect.get("income_hours", 0)) \
                if "income_hours" in effect else effect["amount"]
        _claw_back_cookies(user_id, amount, charge_id)
    elif kind == "boost":
        inst = instance_id or (payload or {}).get("instance")
        if inst:
            # именно по ярлыку выдачи: boost_x2_1h и boost_x2_24h делят ключ
            # click_x2, и его же выдаёт награда за 3 рефералов. Удаление по
            # ключу снимало чужие бусты вместе со своим
            db.exec("DELETE FROM boosts WHERE user_id = ? AND effect_instance_id = ?",
                    (user_id, inst))
        else:
            # без ярлыка снимаем РОВНО ОДНУ строку — самую долгую из живых
            db.exec("DELETE FROM boosts WHERE id = (SELECT id FROM boosts "
                    "WHERE user_id = ? AND boost_key = ? "
                    "ORDER BY expires_at DESC LIMIT 1)",
                    (user_id, effect.get("key")))
    elif kind == "bp_premium":
        # снимаем СВОЁ право и пересобираем флаг: у игрока пасс мог быть ещё и
        # за 25 рефералов или перенесённый с прошлого сезона, а раньше возврат
        # обнулял флаг целиком вместе с ними
        db.exec("DELETE FROM entitlements WHERE user_id = ? AND kind = 'bp_premium' "
                "AND source = 'purchase' AND source_ref = ?",
                (user_id, charge_id or ""))
        _recompute_bp_premium(user_id)
    elif kind == "offline_cap":
        # ПЕРЕСЧЁТ по оставшимся выданным покупкам, а не вычитание часов.
        # Эффект применяется как max(), поэтому вычитание ломало владельца
        # старшего тира: возврат базовых 6ч ронял его 12ч до 6ч. Считаем после
        # смены статуса, так что возвращаемая строка в выборку уже не попадает
        hours = max([0.0] + [
            cfg.SHOP_ITEMS[r["item_key"]][3]["hours"]
            for r in db.q("SELECT item_key FROM purchases WHERE user_id = ? "
                          "AND status = 'fulfilled'", (user_id,))
            if r["item_key"] in cfg.SHOP_ITEMS
            and cfg.SHOP_ITEMS[r["item_key"]][3]["type"] == "offline_cap"])
        db.exec("UPDATE users SET offline_bonus_hours = ?, "
                "user_revision = user_revision + 1 WHERE user_id = ?",
                (hours, user_id))
    # energy_full откатывать нечего — энергия и так расходуется


def revoke_charge(charge_id: str, stars: int | None = None) -> str:
    """Возврат звёзд: снимаем выданное и помечаем покупку 'refunded'.
    Идемпотентно — повторный refund ничего не меняет.

    Возвращает ИСХОД, а не «получилось/нет»:
      'revoked'           — товар был выдан, эффект снят;
      'nothing_to_revoke' — платёж не доходил до выдачи ('paid', 'void',
                            'unmatched'): возврат оформлен, снимать нечего;
      'already_refunded'  — по этому платежу возврат уже проведён;
      'no_row'            — платежа нет в базе.
    Различать это нужно игроку: на 'nothing_to_revoke' бот раньше писал «бонус
    снят», и человек шёл искать, чего он лишился, — при том что не получал
    ничего. И нужно человеку в /api/admin/payments: prior_status сохраняет, из
    какого состояния уехал платёж, а сегодняшняя слепая перезапись это стирала.

    Переход статуса — compare-and-set по ТОМУ ЖЕ значению, которое мы прочитали
    и на котором приняли решение откатывать эффект. Раньше UPDATE стоял без
    охраны: между чтением 'paid' и записью 'refunded' успевала пройти выдача, и
    возврат оформлялся, не сняв только что выданное. Не сошлось — перечитываем
    и решаем заново по актуальному статусу."""
    with db.tx():
        for _ in range(2):
            row = db.q1("SELECT user_id, item_key, status, granted_payload, "
                        "effect_instance_id FROM purchases "
                        "WHERE tg_payment_id = ?", (charge_id,))
            if not row:
                return "no_row"
            if row["status"] == "refunded":
                return "already_refunded"
            # предикат — «тот же статус», а не status IN ('paid','fulfilled'):
            # 'void' и 'unmatched' тоже обязаны уметь стать 'refunded', иначе
            # ровно те строки, которые человек и разбирает руками, застряли бы
            # навсегда
            if db.exec("UPDATE purchases SET status = 'refunded', prior_status = ?, "
                       "refunded_at = ?, refund_stars = ? "
                       "WHERE tg_payment_id = ? AND status = ?",
                       (row["status"], time.time(), stars,
                        charge_id, row["status"])) == 0:
                continue                     # статус уехал под нами
            if row["status"] == "fulfilled":
                # откатываем по записи о выдаче: что именно и сколько выдали
                _revoke_purchase_effect(row["user_id"], row["item_key"],
                                        row["granted_payload"],
                                        row["effect_instance_id"], charge_id)
                return "revoked"
            return "nothing_to_revoke"
        return "already_refunded"            # успел параллельный возврат


def _apply_purchase_effect(user_id: int, item_key: str, charge_id: str = "") -> dict:
    """Применяет эффект купленного товара и возвращает, ЧТО именно выдал.
    Вызывается внутри db.tx().

    `charge_id` идёт в токен операции: один платёж — одно движение денег, даже
    если выдачу когда-нибудь позовут в обход охраны статуса. С ним же выданное
    записывается в purchases.granted_payload — той же транзакцией, что и сама
    выдача, иначе возврат мог бы прочитать пустоту после сбоя в середине.

    Зачем хранить: возврат обязан снять ИМЕННО ВЫДАННОЕ. «10 часов твоего
    дохода» на момент покупки и сегодня — разные числа, и пересчёт при возврате
    забирал в разы больше выданного (в пределе — весь банк). Ярлык выдачи
    (effect_instance_id) отличает буст этой покупки от буста с тем же ключом из
    другой покупки или из награды за рефералов."""
    effect = cfg.SHOP_ITEMS[item_key][3]
    op = f"purchase:{charge_id}" if charge_id else None
    instance = uuid.uuid4().hex
    now = time.time()
    payload = {"schema": 1, "type": effect["type"], "instance": instance}
    if effect["type"] == "cookies":
        if "income_hours" in effect:
            income = hourly_income(user_id)
            amount = max(effect["min_amount"], income * effect["income_hours"])
            payload.update(income_at_grant=income,
                           income_hours=effect["income_hours"],
                           min_amount=effect["min_amount"])
        else:
            amount = effect["amount"]
        payload["amount"] = amount
        add_cookies(user_id, amount, count_earned=False, operation_id=op,
                    reason="purchase_cookies")
    elif effect["type"] == "energy_full":
        # именно energy_cap, а не cfg.max_energy: иначе купленный за Stars
        # «полный бак» игнорировал апгрейды energy_cap_* и недоливал до 750.
        # Выдаём бак целиком: клэмп в grant_energy всё равно доведёт ровно до
        # потолка, а в книгу ляжет фактически влившееся
        before = db.get_user(user_id)["energy"]
        after = grant_energy(user_id, energy_cap(db.get_user(user_id)),
                             "energy_stars_full", operation_id=op)
        # откатывать нечего (энергия и так расходуется), но по записи видно,
        # что игрок получил, — иначе спор по возврату разбирать нечем
        payload.update(energy_before=before, energy_after=after)
    elif effect["type"] == "boost":
        expires = now + effect["hours"] * 3600
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at, "
                "effect_instance_id, source) VALUES (?, ?, ?, ?, 'purchase')",
                (user_id, effect["key"], expires, instance))
        payload.update(boost_key=effect["key"], expires_at=expires)
    elif effect["type"] == "bp_premium":
        grant_bp_premium(user_id, now, source="purchase",
                         source_ref=charge_id or "")
        payload["source_ref"] = charge_id or ""
    elif effect["type"] == "offline_cap":
        # постоянный бонус; max — покупка старшего тира поверх младшего апгрейдит
        before = offline_bonus_hours(db.get_user(user_id))
        after = max(before, effect["hours"])
        db.update_user(user_id, offline_bonus_hours=after)
        payload.update(hours=effect["hours"], hours_before=before,
                       hours_after=after)
    if charge_id:
        db.exec("UPDATE purchases SET granted_payload = ?, granted_at = ?, "
                "effect_instance_id = ? WHERE tg_payment_id = ?",
                (json.dumps(payload, ensure_ascii=False), now, instance, charge_id))
    return payload


def fulfill_charge(charge_id: str) -> bool:
    """Выдаёт оплаченную покупку по charge_id.
    Возвращает True, если выдали сейчас; False — уже было выдано/нет записи.

    Право на выдачу забирается ПЕРВЫМ стейтментом: 'paid' -> 'fulfilled' под
    охраной статуса, и только выигравший rowcount применяет эффект. Раньше
    статус читался, потом применялся эффект, и только потом записывался новый
    статус — два worker'а (или webhook и /auth, а они приходят вплотную) успевали
    прочитать 'paid' оба и выдать товар дважды.

    Промежуточное 'fulfilled' перед проверкой «а есть ли что выдавать» наружу не
    видно: всё внутри одной транзакции, и в 'void' строка уезжает до коммита."""
    if not charge_id:
        return False
    with db.tx():
        row = db.q1w("UPDATE purchases SET status = 'fulfilled' "
                     "WHERE tg_payment_id = ? AND status = 'paid' "
                     "RETURNING user_id, item_key", (charge_id,))
        if row is None:
            return False
        # Товар исчез из конфига или уже куплен навсегда — выдать нечего.
        # Раньше такая строка вечно висела в 'paid': fulfill_pending перебирал
        # её на каждом /auth, каждый раз возвращал False, и никто не узнавал.
        if row["item_key"] not in cfg.SHOP_ITEMS \
                or purchase_already_owned(row["user_id"], row["item_key"]):
            db.exec("UPDATE purchases SET status = 'void' WHERE tg_payment_id = ?",
                    (charge_id,))
            return False
        _apply_purchase_effect(row["user_id"], row["item_key"], charge_id)
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


def _revisions(user_id: int) -> dict:
    """Пара версий: состояние игрока и раскладка доски."""
    row = db.q1("SELECT user_revision, board_revision FROM users WHERE user_id = ?",
                (user_id,))
    return {"user": row["user_revision"] if row else 0,
            "board": row["board_revision"] if row else 0}


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
    board_income = board_base_income(user_id)  # цены спавна — только от доски
    record = user["best_item_level"] or 0      # премия за рекорд входит и в цену
    base_income = income_base(user_id)        # сила клика — от фермы и доски
    nxt = user["level"] + 1
    eff = upgrade_effects(user_id)
    owned_skins = {r["skin_key"] for r in
                   db.q("SELECT skin_key FROM skins WHERE user_id = ?", (user_id,))}
    owned_skins.add("classic")
    state = {
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
        "spawn_cost": cfg.spawn_cost(items_count, board_income, record=record),
        # прямая покупка печенек выше 1 lvl: доступные уровни и цены.
        # Отдаём только реально доступные тиры: цены за 21 lvl (6.5 млн часов
        # дохода) — мусор в ответе, который фронт всё равно не показывает
        "spawn_direct": {
            "max_level": _direct_max_level(user),
            "costs": {str(l): cfg.direct_spawn_cost(l, items_count, board_income,
                                                    record=record)
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
    # версии читаются ПОСЛЕДНИМИ: выше по функции refresh_energy и last_seen_at
    # уже успели тронуть строку, и снятая заранее версия уехала бы в ответ
    # заведомо устаревшей — клиент вернул бы её и получил 409 на ровном месте
    state["revision"] = _revisions(user_id)
    return state


# Разовая миграция заказов выполняется на импорте — как и backfill книги в
# economy.py: к моменту, когда первая ручка тронет пекарню, строки уже должны
# быть приведены к текущему набору шаблонов.
backfill_orders_config()
