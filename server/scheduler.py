"""Расписание фоновых задач, общее для всех процессов.

Задачи (ролловер сезона, чистки, бэкап, пуши) крутил тот же процесс, что и API,
и это было верно, пока процесс один. Со вторым воркером каждая задача
запускается дважды: два ролловера дерутся за один write-lock и пересчитывают
прогресс одним и тем же игрокам, две чистилки удаляют одни и те же строки, два
пуш-прохода шлют игроку по два сообщения — а на пуши Telegram банит.

Механизмов здесь ДВА, и каждый отвечает за свой вопрос:

  * КОГДА пора — в базе (таблица job_runs). Отметка последнего запуска обязана
    переживать рестарт: у бэкапа она жила в модульной переменной, и цикл
    перезапусков (а именно так выглядит падающий процесс под systemd) снимал
    полную копию базы на каждом старте. База — единственное состояние, которое
    у всех процессов уже общее, поэтому расписание не требует Redis.

  * КТО прямо сейчас — замок в cache.lock. Отметка «взял в работу» ставится в
    начале, но этого мало: пуш-проход спит 0.05 с на игрока, и на сотне тысяч
    аккаунтов один проход легко переживает свой же интервал. Замок с ttl держит
    работу в одних руках и сам освобождается, если владельца убили посреди неё.

Заявка на интервал — условный UPDATE (`WHERE last_run_at <= now - interval`):
выигрывает ровно один процесс, проигравший не ждёт и не переспрашивает. Это
работает и без Redis, поэтому одиночный запуск ничего не теряет.

Оба порога — про длительность работы: interval и ttl должны быть заметно больше
самого долгого прохода задачи, иначе следующий интервал откроется поверх ещё
идущего.
"""
import logging
import os
import socket
import time
from contextlib import contextmanager

import db as db_module
from server import cache

log = logging.getLogger(__name__)

# Кто именно отработал задачу. Нужно ровно для разбора «почему бэкапа нет»:
# в кластере это первый вопрос, а по логам одного процесса на него не ответить.
OWNER = f"{socket.gethostname()}:{os.getpid()}"


def _due(key: str, interval: float, now: float) -> bool:
    """Дешёвая проверка «пора ли», до похода за замком."""
    row = db_module.shared().q1(
        "SELECT last_run_at FROM job_runs WHERE job_key = ?", (key,))
    return row is None or row["last_run_at"] <= now - interval


def _claim(key: str, interval: float, now: float) -> bool:
    """Забрать интервал себе. True ровно у одного процесса.

    Строку создаём с last_run_at = 0, поэтому первый запуск после деплоя
    происходит сразу, а не через interval."""
    d = db_module.shared()
    with d.tx():
        d.exec("INSERT INTO job_runs (job_key, last_run_at, last_ok_at) "
               "VALUES (?, 0, 0) ON CONFLICT (job_key) DO NOTHING", (key,))
        return d.exec("UPDATE job_runs SET last_run_at = ?, owner = ?, "
                      "interval_s = ?, runs = runs + 1 "
                      "WHERE job_key = ? AND last_run_at <= ?",
                      (now, OWNER, interval, key, now - interval)) == 1


def _finish(key: str, now: float, error: str | None = None):
    d = db_module.shared()
    if error:
        d.exec("UPDATE job_runs SET fails = fails + 1, last_error = ? "
               "WHERE job_key = ?", (error, key))
    else:
        d.exec("UPDATE job_runs SET last_ok_at = ?, last_error = NULL "
               "WHERE job_key = ?", (now, key))


@contextmanager
def job(key: str, interval: float, ttl: float):
    """`with job("db_backup", 86400, 3600) as mine:` — mine True не чаще раза в
    interval секунд и ровно в одном процессе кластера.

    Тело выполняется ТОЛЬКО при mine=True — проверять обязан вызывающий, как и
    у cache.lock: молчаливое проглатывание чужого тика спрятало бы разницу
    между «не моя очередь» и «работа сделана».

    Исключение из тела не глотается: оно попадает в last_error (в кластере это
    единственный след, если процесс с логом уже сменился) и летит дальше."""
    now = time.time()
    if not _due(key, interval, now):
        yield False
        return
    with cache.lock("job:" + key, ttl) as mine:
        if not mine:
            yield False
            return
        # Заявку берём уже под замком и по свежему времени: между _due и замком
        # интервал мог закрыть другой процесс, и тогда сработает условие UPDATE
        if not _claim(key, interval, time.time()):
            yield False
            return
        try:
            yield True
        except Exception as e:
            _finish(key, time.time(), error=f"{type(e).__name__}: {e}"[:300])
            raise
        _finish(key, time.time())


def health(now: float | None = None) -> dict:
    """Состояние планировщика для /healthz.

    Нужно потому, что при ROLE=api и ROLE=scheduler это РАЗНЫЕ процессы: у
    воркера, который отвечает на /healthz, фоновых задач нет вовсе, и молчащий
    планировщик снаружи выглядел бы как здоровая система. Просроченной считаем
    задачу, которая не доходила до конца дольше трёх своих периодов — одного
    мало, интервал и тик расходятся на длину самой работы."""
    now = time.time() if now is None else now
    rows = db_module.shared().q(
        "SELECT job_key, last_ok_at, interval_s, runs, fails, last_error "
        "FROM job_runs")
    if not rows:
        return {"jobs": 0, "note": "планировщик ещё ни разу не отчитывался"}
    stale, failing = [], {}
    for r in rows:
        if r["interval_s"] and now - r["last_ok_at"] > r["interval_s"] * 3:
            stale.append(r["job_key"])
        if r["last_error"]:
            failing[r["job_key"]] = r["last_error"]
    out = {"jobs": len(rows),
           "last_ok_age": round(min(now - r["last_ok_at"] for r in rows), 1)}
    if stale:
        out["stale"] = stale
    if failing:
        out["failing"] = failing
    return out


def reset(key: str | None = None):
    """Забыть расписание (тесты, а также ручной «сделай бэкап сейчас»)."""
    d = db_module.shared()
    if key is None:
        d.exec("DELETE FROM job_runs")
    else:
        d.exec("DELETE FROM job_runs WHERE job_key = ?", (key,))
