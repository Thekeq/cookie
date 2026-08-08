"""Миграции схемы: межпроцессный замок, перенос аналитики пачками, догон после сбоя.

Почему набор отдельный и почему в нём есть НАСТОЯЩИЕ процессы. Слой БД
поднимается в каждом процессе, а на бою их несколько (WEB_CONCURRENCY > 1), и
стартуют они одновременно. Отметка в schema_migrations пишется ПОСЛЕ работы, то
есть сама по себе от гонки не защищает: все воркеры одинаково видят «не
применено». Проверить это в потоках нельзя — межпроцессный замок на SQLite
файловый, а на PostgreSQL сессионный, и оба различают именно процессы.

Опасное место здесь ровно одно: перенос старой таблицы `events` в
`analytics_events` с последующим DROP TABLE. Гонка на нём означает снос таблицы
из-под читающего соседа и задвоенные строки в аналитике, а объём — миллионы
строк с боевого TTL в 30 дней. Поэтому набор гоняет перенос на нескольких
тысячах строк, а не на пустой базе: пустая база проходит это место, не заходя в
него.

Запуск: python test_migrations.py
"""
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import time

# .env разработчика не должен увести проверки в боевую базу: путь каждому
# экземпляру ниже задаётся аргументом, а DATABASE_URL вычищается совсем
os.environ.pop("DATABASE_URL", None)
TMP = tempfile.gettempdir()
os.environ.setdefault("DATABASE_PATH",
                      os.path.join(TMP, f"cookie_mig_stub_{os.getpid()}.db"))

from db import DataBase  # noqa: E402

DEFAULT_BATCH = DataBase.ANALYTICS_MIGRATION_BATCH
SELF = os.path.abspath(__file__)

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ---------- вспомогательное ----------

def wipe(path: str):
    for suffix in ("", "-wal", "-shm", ".migrate.lock"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


def fresh_path(tag: str) -> str:
    path = os.path.join(TMP, f"cookie_mig_{tag}_{os.getpid()}.db")
    wipe(path)
    return path


def make_legacy_db(path: str, rows: int) -> int:
    """База в состоянии ДО миграции: та самая старая таблица `events`.

    Каждая третья строка удаляется намеренно. На бою id разрежены — чистилка по
    TTL выкусывает старые строки, — и перенос обязан ходить по существующим id,
    а не по ровным окнам одинаковой ширины, иначе он гоняет пустые обороты.

    Возвращает число строк, которые обязаны доехать до analytics_events (то
    есть без снимков конфига ивентов: тем дорога в app_state)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, "
                 "event TEXT, value REAL DEFAULT 0, created_at REAL DEFAULT 0)")
    now = time.time()
    conn.executemany(
        "INSERT INTO events (user_id, event, value, created_at) VALUES (?, ?, ?, ?)",
        [(1000 + i % 37, "app_open", float(i), now - i) for i in range(rows)])
    conn.executemany(
        "INSERT INTO events (user_id, event, value, created_at) VALUES (?, ?, ?, ?)",
        [(0, f"event_cfg:run7:{k}", 1.0, now) for k in ("pool", "kill", "mult")])
    conn.execute("DELETE FROM events WHERE id % 3 = 0")
    conn.commit()
    expected = conn.execute("SELECT COUNT(*) FROM events "
                            "WHERE event NOT LIKE 'event_cfg:%'").fetchone()[0]
    conn.close()
    return expected


def raw(path: str, sql: str, params=()) -> list[dict]:
    """Чтение базы В ОБХОД слоя доступа: конструктор DataBase сам гоняет
    миграции, и проверять его результат его же экземпляром — значит проверять
    ещё один прогон."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def bare(path: str) -> DataBase:
    """Экземпляр без __init__ — только чтобы взять замок, не трогая схему."""
    inst = object.__new__(DataBase)
    inst.DIALECT = "sqlite"
    inst.db_file = path
    return inst


def spawn(path: str, start_at: float) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("DATABASE_URL", None)
    return subprocess.Popen(
        [sys.executable, SELF, "--worker", path, str(start_at)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", env=env)


def worker(path: str, start_at: float) -> int:
    """Роль отдельного процесса: поднять слой БД, как это делает воркер uvicorn.

    Пачка нарочно мелкая: перенос идёт дольше, окно гонки шире, и если замка
    нет, соседи успевают влезть в середину."""
    os.environ.pop("DATABASE_URL", None)
    DataBase.ANALYTICS_MIGRATION_BATCH = 250
    while time.time() < start_at:      # общий момент старта для всех воркеров
        time.sleep(0.002)
    inst = DataBase(path)
    print(f"WORKER_DONE rows={inst.q1('SELECT COUNT(*) AS n FROM analytics_events')['n']}")
    return 0


# ---------- проверки ----------

def test_batched_copy():
    print("\n=== перенос events идёт пачками ===")
    path = fresh_path("batch")
    expected = make_legacy_db(path, rows=5000)
    batch = 700
    inserts = []
    original = DataBase.exec

    def counting(self, sql, params=()):
        if "INSERT INTO analytics_events" in sql:
            inserts.append(sql)
        return original(self, sql, params)

    DataBase.exec = counting
    DataBase.ANALYTICS_MIGRATION_BATCH = batch
    try:
        DataBase(path)
    finally:
        DataBase.exec = original
        DataBase.ANALYTICS_MIGRATION_BATCH = DEFAULT_BATCH

    moved = raw(path, "SELECT COUNT(*) AS n FROM analytics_events")[0]["n"]
    check("перенесены все строки", moved == expected, f"{moved} != {expected}")
    check("копирование разбито на пачки, а не одним оператором",
          len(inserts) == math.ceil(expected / batch) and len(inserts) > 1,
          f"операторов {len(inserts)}")
    check("id старых строк сохранены в event_id",
          raw(path, "SELECT COUNT(*) AS n FROM analytics_events "
                    "WHERE event_id LIKE 'legacy:%'")[0]["n"] == expected)
    check("снимки конфига ивентов ушли в app_state, а не в аналитику",
          raw(path, "SELECT COUNT(*) AS n FROM app_state")[0]["n"] == 2
          and raw(path, "SELECT COUNT(*) AS n FROM analytics_events "
                        "WHERE event LIKE 'event_cfg:%'")[0]["n"] == 0,
          str(raw(path, "SELECT name FROM app_state")))
    check("старая таблица events удалена",
          not raw(path, "SELECT name FROM sqlite_master WHERE name = 'events'"))
    check("миграция отмечена в журнале",
          len(raw(path, "SELECT name FROM schema_migrations "
                        "WHERE name = 'analytics:events_v2'")) == 1)


def test_resume_after_partial_copy():
    print("\n=== процесс убили посреди переноса ===")
    path = fresh_path("resume")
    expected = make_legacy_db(path, rows=3000)

    class Killed(Exception):
        pass

    original = DataBase.exec
    done = {"batches": 0}

    def dying(self, sql, params=()):
        if "INSERT INTO analytics_events" in sql:
            done["batches"] += 1
            if done["batches"] > 2:
                raise Killed("как будто процесс убили на середине")
        return original(self, sql, params)

    DataBase.exec = dying
    DataBase.ANALYTICS_MIGRATION_BATCH = 400
    killed = False
    try:
        DataBase(path)
    except Killed:
        killed = True
    finally:
        DataBase.exec = original

    check("перенос прервался на середине", killed)
    half = raw(path, "SELECT COUNT(*) AS n FROM analytics_events")[0]["n"]
    check("часть строк уже лежит в аналитике", 0 < half < expected, str(half))
    check("отметка о миграции НЕ поставлена — она ставится после работы",
          not raw(path, "SELECT name FROM schema_migrations "
                        "WHERE name = 'analytics:events_v2'"))
    check("старая таблица на месте: сносить её было рано",
          bool(raw(path, "SELECT name FROM sqlite_master WHERE name = 'events'")))

    try:
        DataBase(path)                 # повторный старт обязан догнать
    finally:
        DataBase.ANALYTICS_MIGRATION_BATCH = DEFAULT_BATCH
    total = raw(path, "SELECT COUNT(*) AS n FROM analytics_events")[0]["n"]
    check("повторный прогон догнал остаток", total == expected, f"{total} != {expected}")
    check("строки не задвоились",
          not raw(path, "SELECT event_id FROM analytics_events "
                        "GROUP BY event_id HAVING COUNT(*) > 1"))
    check("снимки конфига не задвоились",
          raw(path, "SELECT COUNT(*) AS n FROM app_state")[0]["n"] == 2)
    check("после догона таблица events снесена",
          not raw(path, "SELECT name FROM sqlite_master WHERE name = 'events'"))


def test_parallel_start(workers=4):
    print(f"\n=== {workers} процесса стартуют одновременно ===")
    path = fresh_path("race")
    expected = make_legacy_db(path, rows=4000)
    # запас на импорт питона в каждом процессе: старт должен прийтись на момент,
    # когда все уже готовы, иначе первый успеет домигрировать до появления
    # остальных и гонки не будет вовсе
    start_at = time.time() + 3.0
    procs = [spawn(path, start_at) for _ in range(workers)]
    outs = []
    for proc in procs:
        out, _ = proc.communicate(timeout=300)
        outs.append((proc.returncode, out))

    check("все процессы поднялись без ошибок",
          all(code == 0 for code, _ in outs),
          " | ".join(o.strip()[-300:] for c, o in outs if c != 0))
    migrated = sum("аналитика переехала" in out for _, out in outs)
    check("перенос выполнил РОВНО ОДИН процесс", migrated == 1, f"выполнили {migrated}")
    seen = [out for _, out in outs if f"WORKER_DONE rows={expected}" in out]
    check("все процессы увидели полную, уже мигрированную таблицу",
          len(seen) == workers,
          " | ".join(line for _, o in outs for line in o.splitlines()
                     if "WORKER_DONE" in line))
    total = raw(path, "SELECT COUNT(*) AS n FROM analytics_events")[0]["n"]
    check("строк ровно столько, сколько было", total == expected,
          f"{total} != {expected}")
    check("дублей нет",
          not raw(path, "SELECT event_id FROM analytics_events "
                        "GROUP BY event_id HAVING COUNT(*) > 1"))
    check("app_state не задвоен",
          raw(path, "SELECT COUNT(*) AS n FROM app_state")[0]["n"] == 2)
    check("старая таблица снесена один раз и до конца",
          not raw(path, "SELECT name FROM sqlite_master WHERE name = 'events'"))


def test_loser_waits():
    print("\n=== проигравший замок ЖДЁТ, а не идёт вперёд ===")
    path = fresh_path("wait")
    expected = make_legacy_db(path, rows=1000)
    held = 5.0
    holder = bare(path)
    started = time.time()
    with holder._migration_lock():
        proc = spawn(path, time.time())
        time.sleep(held)
        alive = proc.poll() is None
        legacy_still_there = bool(
            raw(path, "SELECT name FROM sqlite_master WHERE name = 'events'"))
    out, _ = proc.communicate(timeout=300)
    took = time.time() - started

    check("процесс без замка не завершился, пока замок занят", alive, out.strip()[-300:])
    check("пока замок занят, схему никто не тронул", legacy_still_there)
    check("дождавшись замка, процесс доводит старт до конца",
          proc.returncode == 0, out.strip()[-300:])
    check("он именно ждал, а не проскочил", took >= held, f"{took:.1f} с")
    total = raw(path, "SELECT COUNT(*) AS n FROM analytics_events")[0]["n"]
    check("данные перенесены целиком", total == expected, f"{total} != {expected}")


def test_lock_release():
    print("\n=== снятие замка ===")
    path = fresh_path("release")

    class Boom(Exception):
        pass

    inst = bare(path)
    inst.MIGRATION_LOCK_TIMEOUT = 2.0
    try:
        with inst._migration_lock():
            raise Boom()
    except Boom:
        pass
    # если бы finally не отработал, здесь был бы таймаут ожидания
    started = time.time()
    released = True
    try:
        with inst._migration_lock():
            pass
    except RuntimeError:
        released = False
    check("замок снят исключением, а не остался висеть",
          released and time.time() - started < 1.0)

    other = bare(path)
    other.MIGRATION_LOCK_TIMEOUT = 1.0
    with inst._migration_lock():
        got = True
        try:
            with other._migration_lock():
                pass
        except RuntimeError:
            got = False
    check("занятый замок не отдаётся молча — процесс падает по таймауту",
          got is False)


def test_postgres_advisory_lock():
    print("\n=== PostgreSQL: advisory-замок ===")
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        print("  СКИП: TEST_DATABASE_URL не задан, живой PostgreSQL не проверяется")
        return
    first, second = DataBase(url=url), DataBase(url=url)
    second.MIGRATION_LOCK_TIMEOUT = 1.0
    with first._migration_lock():
        got = True
        try:
            with second._migration_lock():
                pass
        except RuntimeError:
            got = False
    check("вторая сессия не входит в миграции, пока идёт первая", got is False)
    took = time.time()
    with second._migration_lock():
        pass
    check("после выхода замок отдан", time.time() - took < 1.0)


def main() -> int:
    test_batched_copy()
    test_resume_after_partial_copy()
    test_parallel_start()
    test_loser_waits()
    test_lock_release()
    test_postgres_advisory_lock()
    print(f"\n{'=' * 60}\nПройдено {passed}, провалено {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) > 3 and sys.argv[1] == "--worker":
        sys.exit(worker(sys.argv[2], float(sys.argv[3])))
    sys.exit(main())
