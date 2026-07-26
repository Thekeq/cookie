"""Дуэль пекарей: асинхронный забег 1x1 на сутки.

Зачем. Лидерборд — витрина: ты видишь чужие числа и ничего не можешь с ними
сделать. Дуэль даёт то, чего в игре не было совсем, — конкретного соперника.
Подбор идёт внутри своей лиги, метрика — сколько напёк ЗА сутки дуэли, а не
сколько накопил всего, поэтому ветеран не давит новичка накопленным.

Асинхронность здесь принципиальна: обоим игрокам не нужно быть онлайн
одновременно, что для Mini App единственный работающий вариант.
"""
import time

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db


def _finish_if_due(row: dict) -> dict:
    """Закрывает дуэль, если время вышло. Победитель и приз фиксируются один
    раз — условным UPDATE, чтобы два одновременных запроса не посчитали дважды."""
    if row["status"] != "active" or time.time() < row["ends_at"]:
        return row
    a_score = _score(row["user_a"], row["a_start"])
    b_score = _score(row["user_b"], row["b_start"])
    winner = row["user_a"] if a_score >= b_score else row["user_b"]
    # приз считаем от дохода ПОБЕДИТЕЛЯ: иначе выиграть у богатого соперника
    # было бы выгоднее, чем играть, и появился бы смысл искать «жирную» цель
    reward = max(cfg.DUEL_REWARD_MIN,
                 gl.hourly_income(winner) * cfg.DUEL_REWARD_HOURS)
    with db.tx():
        db.exec("UPDATE duels SET status = 'done', winner_id = ?, reward = ? "
                "WHERE id = ? AND status = 'active'", (winner, reward, row["id"]))
    return db.q1("SELECT * FROM duels WHERE id = ?", (row["id"],))


def _score(user_id: int, start: float) -> float:
    """Сколько игрок напёк с начала дуэли."""
    user = db.get_user(user_id)
    return max(0.0, (user["total_earned"] if user else 0) - start)


def my_duel(user_id: int) -> dict | None:
    row = db.q1(
        "SELECT * FROM duels WHERE (user_a = ? OR user_b = ?) "
        "AND (status != 'done' OR (winner_id IS NOT NULL AND "
        "     ((user_a = ? AND claimed_a = 0) OR (user_b = ? AND claimed_b = 0)))) "
        "ORDER BY id DESC LIMIT 1",
        (user_id, user_id, user_id, user_id))
    return _finish_if_due(row) if row else None


def state(user: dict) -> dict:
    """Состояние дуэли для фронта: свой и чужой счёт, время, приз."""
    uid = user["user_id"]
    row = my_duel(uid)
    if not row:
        return {"duel": None, "reward_hours": cfg.DUEL_REWARD_HOURS,
                "hours": cfg.DUEL_HOURS}
    is_a = row["user_a"] == uid
    foe_id = row["user_b"] if is_a else row["user_a"]
    foe = db.get_user(foe_id) if foe_id else None
    return {
        "duel": {
            "id": row["id"],
            "status": row["status"],
            "ends_at": row["ends_at"],
            "my_score": _score(uid, row["a_start"] if is_a else row["b_start"]),
            "foe_score": (_score(foe_id, row["b_start"] if is_a else row["a_start"])
                          if foe_id else 0),
            # имя соперника показываем, telegram-id — нет: он не нужен клиенту
            "foe_name": (foe["first_name"] or foe["username"] or "Baker") if foe else None,
            "foe_level": foe["level"] if foe else None,
            "won": row["winner_id"] == uid if row["winner_id"] else None,
            "reward": row["reward"],
            "claimed": bool(row["claimed_a"] if is_a else row["claimed_b"]),
        },
        "reward_hours": cfg.DUEL_REWARD_HOURS,
        "hours": cfg.DUEL_HOURS,
    }


def find(user: dict) -> dict:
    """Найти соперника или встать в очередь.

    Подбор внутри своей лиги и только среди тех, кто ждёт: без живого
    матчмейкинга это единственный способ не свести новичка с ветераном."""
    uid = user["user_id"]
    if user["level"] < cfg.DUEL_MIN_LEVEL:
        raise ValueError(f"err_req_level|{cfg.DUEL_MIN_LEVEL}")
    if my_duel(uid):
        raise ValueError("err_duel_active")
    league = cfg.league_of(user["level"])[0]
    now = time.time()

    with db.tx():
        # чужая заявка в этой же лиге — присоединяемся к ней
        foe = db.q1(
            "SELECT * FROM duels WHERE status = 'waiting' AND league = ? "
            "AND user_a != ? AND created_at > ? ORDER BY created_at LIMIT 1",
            (league, uid, now - cfg.DUEL_QUEUE_TTL_H * 3600))
        if foe:
            # старт обоих счётчиков берём В МОМЕНТ старта: заявка могла висеть
            # часами, и заработок за это время не должен попадать в дуэль
            a_now = db.get_user(foe["user_a"])["total_earned"]
            db.exec(
                "UPDATE duels SET user_b = ?, status = 'active', a_start = ?, "
                "b_start = ?, started_at = ?, ends_at = ? "
                "WHERE id = ? AND status = 'waiting'",
                (uid, a_now, user["total_earned"], now,
                 now + cfg.DUEL_HOURS * 3600, foe["id"]))
            if db.cursor.rowcount == 0:      # заявку перехватил кто-то ещё
                raise ValueError("err_duel_taken")
            gl.track(uid, "duel_start")
            return state(db.get_user(uid))

        db.exec(
            "INSERT INTO duels (user_a, league, a_start, created_at, status) "
            "VALUES (?, ?, ?, ?, 'waiting')",
            (uid, league, user["total_earned"], now))
    return state(db.get_user(uid))


def cancel(user: dict) -> dict:
    """Снять свою заявку, пока соперник не нашёлся."""
    db.exec("DELETE FROM duels WHERE user_a = ? AND status = 'waiting'",
            (user["user_id"],))
    return state(db.get_user(user["user_id"]))


def claim(user: dict) -> dict:
    """Забрать приз за победу. Проигравший закрывает дуэль без награды."""
    uid = user["user_id"]
    row = my_duel(uid)
    if not row or row["status"] != "done":
        raise ValueError("err_not_done")
    is_a = row["user_a"] == uid
    col = "claimed_a" if is_a else "claimed_b"
    reward = row["reward"] if row["winner_id"] == uid else 0
    with db.tx():
        db.exec(f"UPDATE duels SET {col} = 1 WHERE id = ? AND {col} = 0", (row["id"],))
        if db.cursor.rowcount == 0:
            raise ValueError("err_claimed")
        if reward:
            gl.add_cookies(uid, reward, count_earned=False)
    return {"reward": reward, "won": row["winner_id"] == uid,
            **state(db.get_user(uid))}
