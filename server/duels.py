"""Дуэль пекарей: асинхронный забег 1x1 на сутки.

Зачем. Лидерборд — витрина: ты видишь чужие числа и ничего не можешь с ними
сделать. Дуэль даёт то, чего в игре не было совсем, — конкретного соперника.
Метрика — сколько напёк ЗА сутки дуэли, а не сколько накопил всего, поэтому
ветеран не давит новичка накопленным.

Счёт НОРМАЛИЗОВАН по собственному доходу игрока (см. _norm): «напёк больше»
между игроками с разницей дохода в сто раз — это не результат, а разница
стартовых условий. Дуэль спрашивает «во сколько раз ты перевыполнил СВОЮ
норму», и на этот вопрос новичок и ветеран отвечают в одних единицах.

Асинхронность здесь принципиальна: обоим игрокам не нужно быть онлайн
одновременно, что для Mini App единственный работающий вариант.
"""
import time

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db


def _prize(user_id: int) -> float:
    """Приз победителю: часы ЕГО дохода.

    Считаем от дохода победителя, а не проигравшего: иначе выиграть у богатого
    соперника было бы выгоднее, чем играть, и появился бы смысл искать «жирную»
    цель. Эта же функция показывает игроку приз ДО согласия на суточный забег —
    так число на экране и число при выплате не могут разойтись."""
    return max(cfg.DUEL_REWARD_MIN,
               gl.hourly_income(user_id) * cfg.DUEL_REWARD_HOURS)


def _baseline(user_id: int) -> float:
    """Знаменатель нормализации: часовой доход игрока, но не меньше флора.

    Флор обязателен: у новичка доход близок к нулю, и без него любое деление
    улетало бы в бесконечность — первый же клик давал бы «тысячу норм»."""
    return max(cfg.DUEL_BASELINE_MIN, gl.hourly_income(user_id))


def _score(user_id: int, start: float) -> float:
    """Сколько игрок напёк с начала дуэли — в печеньках, как есть."""
    user = db.get_user(user_id)
    return max(0.0, (user["total_earned"] if user else 0) - start)


def _norm(user_id: int, raw: float) -> float:
    """Тот же счёт, но в ЧАСАХ СОБСТВЕННОГО ДОХОДА. Это и есть результат
    дуэли: он сравним между игроками разного размера, а абсолютные печеньки —
    нет."""
    return raw / _baseline(user_id)


def _notify(kind: str, user_ids, payload: dict) -> None:
    """Точка врезки очереди уведомлений (server/notifications.py).

    Здесь СОЗНАТЕЛЬНО ничего не отправляется: очередь и её воркер живут в
    другом модуле, и дуэли не имеют права знать про Telegram. Нужны эти три
    вида:
      match_found  — соперник найден (ловится ТОЛЬКО здесь: переход
                     waiting -> active фоновым планировщиком не виден);
      duel_ending  — час до конца, и duel_result — итог: оба уже планируются
                     проходом notifications._plan_duels по строкам таблицы.
    Когда очередь будет готова, тело функции — один вызов enqueue на игрока с
    dedup_key вида f"{kind}:{duel_id}:{user_id}"."""
    return None


def _finish_if_due(row: dict) -> dict:
    """Закрывает дуэль, если время вышло. Победитель и приз фиксируются один
    раз — условным UPDATE, чтобы два одновременных запроса не посчитали дважды.

    Победитель определяется по НОРМАЛИЗОВАННОМУ счёту; абсолютный остаётся
    вторым ключом на случай точного равенства норм (два новичка на флоре)."""
    if row["status"] != "active" or time.time() < row["ends_at"]:
        return row
    a_raw = _score(row["user_a"], row["a_start"])
    b_raw = _score(row["user_b"], row["b_start"])
    a_key = (_norm(row["user_a"], a_raw), a_raw)
    b_key = (_norm(row["user_b"], b_raw), b_raw)
    winner = row["user_a"] if a_key >= b_key else row["user_b"]
    reward = _prize(winner)
    with db.tx():
        db.exec("UPDATE duels SET status = 'done', winner_id = ?, reward = ? "
                "WHERE id = ? AND status = 'active'", (winner, reward, row["id"]))
    return db.q1("SELECT * FROM duels WHERE id = ?", (row["id"],))


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
    # приз при победе прямо сейчас — чтобы соглашаться на сутки не вслепую
    prize = _prize(uid)
    if not row:
        return {"duel": None, "reward_hours": cfg.DUEL_REWARD_HOURS,
                "prize": prize, "hours": cfg.DUEL_HOURS,
                "baseline": _baseline(uid)}
    is_a = row["user_a"] == uid
    foe_id = row["user_b"] if is_a else row["user_a"]
    foe = db.get_user(foe_id) if foe_id else None
    my_raw = _score(uid, row["a_start"] if is_a else row["b_start"])
    foe_raw = (_score(foe_id, row["b_start"] if is_a else row["a_start"])
               if foe_id else 0.0)
    return {
        "duel": {
            "id": row["id"],
            "status": row["status"],
            "ends_at": row["ends_at"],
            # абсолютные печеньки остаются для показа «сколько напёк»…
            "my_score": my_raw,
            "foe_score": foe_raw,
            # …но счёт дуэли — нормализованный, и сравнивать надо его
            "my_norm": _norm(uid, my_raw),
            "foe_norm": _norm(foe_id, foe_raw) if foe_id else 0.0,
            # имя соперника показываем, telegram-id — нет: он не нужен клиенту
            "foe_name": (foe["first_name"] or foe["username"] or "Baker") if foe else None,
            "foe_level": foe["level"] if foe else None,
            "won": row["winner_id"] == uid if row["winner_id"] else None,
            "reward": row["reward"],
            "claimed": bool(row["claimed_a"] if is_a else row["claimed_b"]),
        },
        "reward_hours": cfg.DUEL_REWARD_HOURS,
        "prize": prize,
        "hours": cfg.DUEL_HOURS,
        # знаменатель показываем явно: игрок должен видеть, откуда «1.4 нормы»
        "baseline": _baseline(uid),
    }


def _fits(me: dict, my_income: float, cand: dict, now: float) -> bool:
    """Сопоставим ли соперник: доход и престиж, а не один только уровень.

    Лига (то есть уровень) остаётся первым грубым ситом, но внутри неё доход
    отличается на порядки — донат, престиж и доска делают «равного по уровню»
    соперника недосягаемым. Поэтому коридор считается по доходу и престижу.

    Коридор РАСШИРЯЕТСЯ с возрастом заявки, и это не украшение: у редкого
    профиля (высокий доход в пустой лиге) точный коридор не сойдётся никогда, и
    игрок остался бы в очереди навсегда. За DUEL_MATCH_WIDEN_H часов коридор
    дохода возводится в квадрат, а лига перестаёт быть фильтром."""
    age_h = max(0.0, (now - (cand["created_at"] or now)) / 3600)
    widen = 1.0 + age_h / cfg.DUEL_MATCH_WIDEN_H
    if cand["league"] != cfg.league_of(me["level"])[0] and age_h < cfg.DUEL_MATCH_WIDEN_H:
        return False
    prestige_gap = abs((cand["prestige_points"] or 0)
                       - (me["prestige_points"] or 0))
    if prestige_gap > cfg.DUEL_MATCH_PRESTIGE_BAND * widen:
        return False
    foe_income = max(cfg.DUEL_BASELINE_MIN, gl.hourly_income(cand["user_a"]))
    ratio = max(my_income, foe_income) / min(my_income, foe_income)
    return ratio <= cfg.DUEL_MATCH_INCOME_BAND ** widen


def find(user: dict) -> dict:
    """Найти соперника или встать в очередь.

    Живого матчмейкинга нет и не нужно: подбор идёт среди тех, кто уже ждёт.
    Очередь остаётся FIFO (первым берётся самый старый подходящий), коридор
    только отсекает несопоставимых — иначе честная очередь превратилась бы в
    поиск удобной жертвы."""
    uid = user["user_id"]
    if user["level"] < cfg.DUEL_MIN_LEVEL:
        raise ValueError(f"err_req_level|{cfg.DUEL_MIN_LEVEL}")
    league = cfg.league_of(user["level"])[0]
    now = time.time()
    my_income = max(cfg.DUEL_BASELINE_MIN, gl.hourly_income(uid))

    with db.tx():
        # «у меня уже есть дуэль» проверяется ВНУТРИ транзакции: снаружи два
        # запроса вплотную (двойной тап по кнопке поиска) оба её проходили —
        # игрок оказывался и в очереди, и в дуэли одновременно
        if my_duel(uid):
            raise ValueError("err_duel_active")
        # кандидатов берём пачкой и просеиваем в питоне: коридор считается по
        # доходу, а доход — это ферма, доска, апгрейды и престиж вместе, одним
        # SQL-условием он не выражается
        cands = db.q(
            "SELECT d.id, d.user_a, d.league, d.created_at, "
            "       u.level, u.prestige_points "
            "FROM duels d JOIN users u ON u.user_id = d.user_a "
            "WHERE d.status = 'waiting' AND d.user_a != ? AND d.created_at > ? "
            "ORDER BY d.created_at LIMIT ?",
            (uid, now - cfg.DUEL_QUEUE_TTL_H * 3600, cfg.DUEL_MATCH_CANDIDATES))
        foe = next((c for c in cands if _fits(user, my_income, c, now)), None)
        if foe:
            # старт обоих счётчиков берём В МОМЕНТ старта: заявка могла висеть
            # часами, и заработок за это время не должен попадать в дуэль
            a_now = db.get_user(foe["user_a"])["total_earned"]
            taken = db.exec(
                "UPDATE duels SET user_b = ?, status = 'active', a_start = ?, "
                "b_start = ?, started_at = ?, ends_at = ? "
                "WHERE id = ? AND status = 'waiting'",
                (uid, a_now, user["total_earned"], now,
                 now + cfg.DUEL_HOURS * 3600, foe["id"]))
            if taken == 0:                   # заявку перехватил кто-то ещё
                raise ValueError("err_duel_taken")
            gl.track(uid, "duel_start")
            # единственное место, где виден переход waiting -> active
            _notify("match_found", (uid, foe["user_a"]),
                    {"duel": foe["id"], "ends_at": now + cfg.DUEL_HOURS * 3600})
            return state(db.get_user(uid))

        # INSERT ... SELECT ... WHERE NOT EXISTS, а не VALUES: право встать в
        # очередь решает база, и уникальный индекс uq_duels_waiting не отдаёт
        # игроку 500 вместо понятной ошибки
        queued = db.exec(
            "INSERT INTO duels (user_a, league, a_start, created_at, status) "
            "SELECT ?, ?, ?, ?, 'waiting' WHERE NOT EXISTS ("
            "  SELECT 1 FROM duels WHERE (user_a = ? OR user_b = ?) "
            "  AND (status <> 'done' OR (user_a = ? AND claimed_a = 0) "
            "                        OR (user_b = ? AND claimed_b = 0)))",
            (uid, league, user["total_earned"], now, uid, uid, uid, uid))
        if queued == 0:
            raise ValueError("err_duel_active")
    return state(db.get_user(uid))


def cancel(user: dict) -> dict:
    """Снять свою заявку, пока соперник не нашёлся."""
    db.exec("DELETE FROM duels WHERE user_a = ? AND status = 'waiting'",
            (user["user_id"],))
    return state(db.get_user(user["user_id"]))


def history(user_id: int, limit: int | None = None) -> list[dict]:
    """Последние закрытые дуэли: с кем, чем кончилось, сколько напекли.

    Счёт берётся ИЗ КНИГИ за окно дуэли (gl.earned_between), а не из разницы
    total_earned: тот растёт дальше, и вчерашняя дуэль показывала бы сегодняшний
    заработок. Книга неизменяема, поэтому число в истории больше не меняется.

    Нормализованного счёта в истории нет сознательно: знаменатель (доход на
    момент дуэли) нигде не сохранён, а считать его по СЕГОДНЯШНЕМУ доходу
    значило бы показывать число, которое молча едет вниз по мере прокачки."""
    limit = max(1, min(int(limit or cfg.DUEL_HISTORY_LIMIT), 20))
    rows = db.q(
        "SELECT * FROM duels WHERE (user_a = ? OR user_b = ?) "
        "AND status = 'done' ORDER BY id DESC LIMIT ?",
        (user_id, user_id, limit))
    out = []
    for row in rows:
        is_a = row["user_a"] == user_id
        foe_id = row["user_b"] if is_a else row["user_a"]
        foe = db.get_user(foe_id) if foe_id else None
        started, ended = row["started_at"] or 0.0, row["ends_at"] or 0.0
        out.append({
            "id": row["id"],
            "ended_at": ended,
            "foe_name": (foe["first_name"] or foe["username"] or "Baker") if foe else None,
            "foe_level": foe["level"] if foe else None,
            "my_earned": gl.earned_between(user_id, started, ended),
            "foe_earned": (gl.earned_between(foe_id, started, ended)
                           if foe_id else 0.0),
            "won": row["winner_id"] == user_id,
            "reward": row["reward"] if row["winner_id"] == user_id else 0.0,
            "claimed": bool(row["claimed_a"] if is_a else row["claimed_b"]),
        })
    return out


def claim(user: dict) -> dict:
    """Забрать приз за победу. Проигравший закрывает дуэль без награды.

    Право на приз даёт rowcount условного UPDATE: два нажатия вплотную оба
    видели claimed = 0, и приз уходил дважды. Сумму берём из строки, прочитанной
    УЖЕ внутри транзакции, — снаружи она могла принадлежать другой дуэли."""
    uid = user["user_id"]
    with db.tx():
        row = my_duel(uid)                   # заодно закрывает истёкшую дуэль
        if not row or row["status"] != "done":
            raise ValueError("err_not_done")
        col = "claimed_a" if row["user_a"] == uid else "claimed_b"
        reward = row["reward"] if row["winner_id"] == uid else 0
        if db.exec(f"UPDATE duels SET {col} = 1 WHERE id = ? AND {col} = 0",
                   (row["id"],)) == 0:
            raise ValueError("err_claimed")
        if reward:
            # токен привязан к дуэли и игроку: ретрай ручки не платит второй раз
            # даже если охрана claimed окажется снята вручную
            gl.add_cookies(uid, reward, count_earned=False,
                           operation_id=f"duel_prize:{row['id']}:{uid}",
                           reason="duel_prize")
    return {"reward": reward, "won": row["winner_id"] == uid,
            **state(db.get_user(uid))}
