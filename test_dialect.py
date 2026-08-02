"""Тесты второго диалекта БД: перевод схемы и SQL под PostgreSQL.

Почему офлайн. Драйвера psycopg на машине разработки может не быть вовсе, а
поднимать сервер ради проверки того, как СТРОИТСЯ строка DDL, не нужно: весь
перевод — чистые функции над схемой. Экземпляр создаётся через
`object.__new__`, чтобы не звать `__init__` (он бы полез коннектиться), и ему
руками выставляется диалект.

Живая половина (реальный PostgreSQL) включается переменной TEST_DATABASE_URL и
по умолчанию пропускается. Пропуск печатается явно: «тестов не было» и «тесты
прошли» обязаны выглядеть по-разному.
"""
import os
import sys
import tempfile

DB_PATH = os.path.join(tempfile.gettempdir(),
                       f"cookie_dialect_test_{os.getpid()}.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        os.remove(DB_PATH + _suffix)
os.environ["DATABASE_PATH"] = DB_PATH
# страховка от .env разработчика: с заполненным DATABASE_URL эталонный
# экземпляр ниже поехал бы в чужой PostgreSQL вместо временного файла
os.environ.pop("DATABASE_URL", None)

import db as dbmod

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def pg() -> dbmod.DataBase:
    """Пустой экземпляр в режиме PostgreSQL, без соединения."""
    inst = object.__new__(dbmod.DataBase)
    inst.DIALECT = "postgres"
    inst.GREATEST = "GREATEST"
    inst.LEAST = "LEAST"
    return inst


def lite() -> dbmod.DataBase:
    inst = object.__new__(dbmod.DataBase)
    inst.DIALECT = "sqlite"
    return inst


# Эталон схемы берём у настоящего экземпляра на временном файле: схема живёт в
# __init__, и хардкодить её копию в тесте значило бы проверять копию, а не то,
# что реально создаётся на проде.
real = dbmod.DataBase(DB_PATH, url="")
SCHEMA = real.tables_schema
P, L = pg(), lite()

print("\n=== 1. Перевод объявлений колонок ===")

check("1.1 REAL -> DOUBLE PRECISION",
      P._ddl("cookies", "REAL DEFAULT 0") == "cookies DOUBLE PRECISION DEFAULT 0")
check("1.2 INTEGER -> BIGINT (telegram id не влезает в 32 бита)",
      P._ddl("user_id", "INTEGER UNIQUE") == "user_id BIGINT UNIQUE")
check("1.3 TEXT остаётся TEXT",
      P._ddl("username", "TEXT") == "username TEXT")
check("1.4 двойные кавычки DEFAULT -> одинарные",
      P._ddl("lang", 'TEXT DEFAULT "en"') == "lang TEXT DEFAULT 'en'")
check("1.5 id INTEGER PRIMARY KEY -> BIGSERIAL (аналог rowid-автоинкремента)",
      P._ddl("id", "INTEGER PRIMARY KEY") == "id BIGSERIAL PRIMARY KEY")
check("1.6 чужой PRIMARY KEY автоинкрементом НЕ становится",
      P._ddl("user_id", "INTEGER PRIMARY KEY") == "user_id BIGINT PRIMARY KEY")
check("1.7 TEXT PRIMARY KEY не трогается",
      P._ddl("job_key", "TEXT PRIMARY KEY") == "job_key TEXT PRIMARY KEY")
check("1.8 на sqlite объявление отдаётся как есть",
      L._ddl("lang", 'TEXT DEFAULT "en"') == 'lang TEXT DEFAULT "en"')
check("1.9 NOT NULL и прочий хвост сохраняются",
      P._ddl("amount", "REAL NOT NULL DEFAULT 0")
      == "amount DOUBLE PRECISION NOT NULL DEFAULT 0")

print("\n=== 2. Вся боевая схема переводится без остатка ===")

# Ни одна колонка не должна уехать в PostgreSQL с типом, которого он не знает,
# или с двойными кавычками в DEFAULT (это имя объекта, а не строка).
_KNOWN = {"BIGINT", "DOUBLE", "TEXT", "BIGSERIAL"}
bad_type, bad_quote, serials = [], [], []
for _table, _spec in {"schema_migrations": real.MIGRATIONS_SCHEMA, **SCHEMA}.items():
    for _col, _decl in _spec.items():
        if _col.startswith("__"):
            continue
        out = P._ddl(_col, _decl)
        kind = out.split(" ")[1]
        if kind not in _KNOWN:
            bad_type.append(f"{_table}.{_col}: {out}")
        if '"' in out:
            bad_quote.append(f"{_table}.{_col}: {out}")
        if kind == "BIGSERIAL":
            serials.append(f"{_table}.{_col}")

check(f"2.1 все типы схемы известны PostgreSQL (проблемных: {len(bad_type)})",
      not bad_type)
if bad_type:
    print("      " + "\n      ".join(bad_type))
check(f"2.2 двойных кавычек в DDL не осталось ({bad_quote})", not bad_quote)
check("2.3 BIGSERIAL получают только колонки id",
      all(s.endswith(".id") for s in serials))
check(f"2.4 автоинкрементов столько же, сколько таблиц с id ({len(serials)})",
      len(serials) == sum(1 for s in SCHEMA.values()
                          if s.get("id", "").startswith("INTEGER PRIMARY KEY")))
# economy_opening.user_id — telegram id, его кладёт код. Последовательность там
# подставляла бы свои числа поверх чужих, и входящие остатки уехали бы не тем
# игрокам. Отдельной проверкой, потому что цена ошибки — порча книги операций.
check("2.5 economy_opening.user_id остаётся BIGINT PRIMARY KEY",
      P._ddl("user_id", SCHEMA["economy_opening"]["user_id"])
      == "user_id BIGINT PRIMARY KEY")

print("\n=== 3. CREATE TABLE и ограничения по диалектам ===")

_sql_users = P._create_table_sql("users", {"id": "INTEGER PRIMARY KEY",
                                           "cookies": "REAL DEFAULT 0"})
check("3.1 CREATE TABLE IF NOT EXISTS со списком колонок",
      _sql_users == "CREATE TABLE IF NOT EXISTS users "
                    "(id BIGSERIAL PRIMARY KEY, cookies DOUBLE PRECISION DEFAULT 0)")
check("3.2 ограничения дописываются в конец",
      P._create_table_sql("t", {"a": "REAL"}, ["CHECK (a > 0)"])
      == "CREATE TABLE IF NOT EXISTS t (a DOUBLE PRECISION, CHECK (a > 0))")

check("3.3 _dialect_list: обычный список годится обоим",
      P._dialect_list({"__c__": ["X"]}, "__c__")
      == L._dialect_list({"__c__": ["X"]}, "__c__") == ["X"])
_mix = {"__c__": {"sqlite": ["S"], "postgres": ["P"], "*": ["BOTH"]}}
check("3.4 _dialect_list: dict отдаёт свою часть плюс общую",
      P._dialect_list(_mix, "__c__") == ["P", "BOTH"]
      and L._dialect_list(_mix, "__c__") == ["S", "BOTH"])
check("3.5 _dialect_list: отсутствующий ключ — пустой список",
      P._dialect_list({}, "__c__") == [])
check("3.6 _dialect_list: диалект без своей части получает только общую",
      P._dialect_list({"__c__": {"sqlite": ["S"], "*": ["BOTH"]}}, "__c__")
      == ["BOTH"])

print("\n=== 4. NaN и append-only книги ===")

_ledger = SCHEMA["economy_ledger"]
_pg_checks = " ".join(P._dialect_list(_ledger, "__constraints__"))
_lite_checks = " ".join(L._dialect_list(_ledger, "__constraints__"))
# В PostgreSQL NaN = NaN истинно (NaN там больше любого числа и равен себе),
# поэтому проверка «x = x» из SQLite его бы не поймала.
check("4.1 на postgres NaN ловится сравнением с 'NaN'::double precision",
      "'NaN'::double precision" in _pg_checks and "amount = amount" not in _pg_checks)
check("4.2 на sqlite NaN ловится через x = x (он там ложится как NULL)",
      "amount = amount" in _lite_checks)
check("4.3 границы диапазона общие для обоих движков",
      "1e15" in _pg_checks and "1e15" in _lite_checks)

_pg_after = " ".join(P._dialect_list(_ledger, "__after_create__"))
_lite_after = " ".join(L._dialect_list(_ledger, "__after_create__"))
check("4.4 текст ошибки append-only одинаков на обоих движках",
      "economy_ledger is append-only" in _pg_after
      and "economy_ledger is append-only" in _lite_after)
# CREATE TRIGGER IF NOT EXISTS появился только в PostgreSQL 14; на 13 и ниже
# это синтаксическая ошибка прямо на старте. Идемпотентность даёт связка
# DROP TRIGGER IF EXISTS + CREATE TRIGGER.
check("4.5 postgres не использует CREATE TRIGGER IF NOT EXISTS",
      "CREATE TRIGGER IF NOT EXISTS" not in _pg_after
      and _pg_after.count("DROP TRIGGER IF EXISTS") == 2)
check("4.6 оба триггера книги на месте (UPDATE и DELETE)",
      "trg_ledger_no_update" in _pg_after and "trg_ledger_no_delete" in _pg_after)

print("\n=== 5. Плейсхолдеры ===")

check("5.1 на sqlite '?' остаётся '?'",
      L._sql("SELECT 1 WHERE a = ? AND b = ?") == "SELECT 1 WHERE a = ? AND b = ?")
check("5.2 на postgres '?' становится '%s'",
      P._sql("SELECT 1 WHERE a = ? AND b = ?") == "SELECT 1 WHERE a = %s AND b = %s")
check("5.3 GREATEST/LEAST различаются по диалектам",
      (P.GREATEST, P.LEAST) == ("GREATEST", "LEAST")
      and (real.GREATEST, real.LEAST) == ("MAX", "MIN"))

print("\n=== 6. Снимок через pg_dump ===")

_calls = []
_real_which, _real_run = dbmod.shutil.which, dbmod.subprocess.run


class _Res:
    returncode = 0
    stderr = ""


def _fake_run(args, **kw):
    _calls.append((args, kw))
    # pg_dump сам создаёт файл; чтобы _prune_snapshots было что видеть, делаем
    # это за него
    open(args[args.index("--file") + 1], "w").close()
    return _Res()


dbmod.shutil.which = lambda name: "/usr/bin/pg_dump" if name == "pg_dump" else None
dbmod.subprocess.run = _fake_run
try:
    _snap = pg()
    _snap.db_file = os.path.join(tempfile.gettempdir(), f"pgsnap_{os.getpid()}.db")
    _snap.url = "postgresql://cookie:s3cr%40t@db.example.com:6432/cookiedb"
    _path = _snap._snapshot_postgres(keep=5)
    _args, _kw = _calls[-1]
    _argv = " ".join(_args)
    # Главное свойство: argv видно всей машине через ps, поэтому пароля там
    # быть не должно ни в каком виде — ни в URL, ни отдельным флагом.
    check("6.1 пароля нет в argv", "s3cr@t" not in _argv and "s3cr%40t" not in _argv)
    check("6.2 URL целиком в argv не уезжает", _snap.url not in _argv)
    check("6.3 пароль передан через PGPASSWORD и раскодирован",
          _kw["env"]["PGPASSWORD"] == "s3cr@t")
    check("6.4 хост, порт, пользователь и база разобраны из URL",
          "--host db.example.com" in _argv and "--port 6432" in _argv
          and "--username cookie" in _argv and _args[-1] == "cookiedb")
    check("6.5 формат custom (восстанавливается pg_restore по таблицам)",
          "--format=custom" in _args)
    check("6.6 у снимка есть таймаут — иначе задача навсегда держит замок",
          _kw.get("timeout"))
    check("6.7 путь снимка возвращён и файл создан",
          _path and os.path.exists(_path) and _path.endswith(".dump"))

    _calls.clear()
    dbmod.shutil.which = lambda name: None
    check("6.8 без pg_dump в PATH снимок пропускается, а не падает",
          _snap._snapshot_postgres(keep=5) is None and not _calls)
finally:
    dbmod.shutil.which, dbmod.subprocess.run = _real_which, _real_run

print("\n=== 7. Выбор движка и конфиг ===")

check("7.1 пустой url — sqlite", real.DIALECT == "sqlite" and not real.url)
check("7.2 дефолт класса — sqlite (экземпляр переключает сам себя)",
      dbmod.DataBase.DIALECT == "sqlite")

import importlib

import dotenv

from server import settings as _settings


def _reload(**env):
    saved = {k: os.environ.get(k) for k in env}
    real_load, dotenv.load_dotenv = dotenv.load_dotenv, lambda *a, **k: False
    try:
        for k, v in env.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, str(v))
        return importlib.reload(_settings)
    finally:
        dotenv.load_dotenv = real_load
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


_s = _reload(DATABASE_URL="sqlite:///data.db", BOT_TOKEN="1:x")
check("7.3 DATABASE_URL не postgresql:// — фатальная ошибка конфига",
      any(fatal and "DATABASE_URL" in msg for msg, fatal in _s.problems()))
_s = _reload(DATABASE_URL="postgresql://u:p@h/db", BOT_TOKEN="1:x")
check("7.4 нормальная строка PostgreSQL претензий не вызывает",
      not any("DATABASE_URL" in msg for msg, _ in _s.problems()))
check("7.5 в summary() пароль не печатается",
      "p@h" not in _s.summary() and "db=postgres" in _s.summary())
_s = _reload(DATABASE_URL="postgres://u:p@h/db", BOT_TOKEN="1:x")
check("7.6 короткая схема postgres:// тоже принимается",
      not any("DATABASE_URL" in msg for msg, _ in _s.problems()))
_reload(DATABASE_URL=None)

print("\n=== 8. Скрипт переноса ===")

import pathlib

_mig = pathlib.Path("migrate_to_postgres.py").read_text(encoding="utf-8")
# Пропустить журнал миграций — значит объявить все миграции неприменёнными на
# новой базе. Следующий старт повторит дедуп (он УДАЛЯЕТ строки) и бэкфиллы
# поверх уже перенесённых данных.
check("8.1 перенос включает schema_migrations", "schema_migrations" in _mig)
check("8.2 вставка идемпотентна (ON CONFLICT DO NOTHING)",
      "ON CONFLICT DO NOTHING" in _mig)
check("8.3 последовательности догоняются после переноса",
      "setval" in _mig and "pg_get_serial_sequence" in _mig)
check("8.4 источник открывается только на чтение", "mode=ro" in _mig)
check("8.5 есть --dry-run и --force",
      '"--dry-run"' in _mig and '"--force"' in _mig)

print("\n=== 9. Живой PostgreSQL ===")

_live = os.environ.get("TEST_DATABASE_URL", "")
if not _live:
    print("  ПРОПУЩЕНО: TEST_DATABASE_URL не задан. Перевод схемы проверен "
          "офлайн выше; чтобы прогнать на живой базе:")
    print("    TEST_DATABASE_URL=postgresql://user:pass@localhost/cookie_test "
          "python test_dialect.py")
else:
    import time

    live = dbmod.DataBase(db_file=":memory:", url=_live)
    check("9.1 схема поднимается на живой базе", live.DIALECT == "postgres")
    check("9.2 колонки читаются из information_schema",
          "cookies" in live._columns("users"))
    uid = int(time.time() * 1000) % 10 ** 12
    live.create_user(uid, "u", "U")
    check("9.3 создание игрока и чтение обратно", live.get_user(uid)["user_id"] == uid)
    live.exec("UPDATE users SET cookies = ? WHERE user_id = ?", (10.0, uid))
    check("9.4 плейсхолдеры переписаны и запись прошла",
          live.get_user(uid)["cookies"] == 10.0)
    try:
        live.exec("UPDATE users SET cookies = ? WHERE user_id = ?", (1e30, uid))
        check("9.5 инвариант баланса ловит переполнение", False)
    except dbmod.INTEGRITY_ERRORS as e:
        check("9.5 инвариант баланса ловит переполнение", "balance_insane" in str(e))
    try:
        live.exec("UPDATE users SET cookies = 'NaN' WHERE user_id = ?", (uid,))
        check("9.6 инвариант баланса ловит NaN", False)
    except dbmod.INTEGRITY_ERRORS as e:
        check("9.6 инвариант баланса ловит NaN", "balance_insane" in str(e))
    with live.tx():
        live.exec("UPDATE users SET cookies = ? WHERE user_id = ?", (5.0, uid))
    check("9.7 tx() коммитит", live.get_user(uid)["cookies"] == 5.0)
    try:
        with live.tx():
            live.exec("UPDATE users SET cookies = ? WHERE user_id = ?", (7.0, uid))
            raise RuntimeError("откат")
    except RuntimeError:
        pass
    check("9.8 tx() откатывает", live.get_user(uid)["cookies"] == 5.0)
    live.exec("DELETE FROM users WHERE user_id = ?", (uid,))

for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        try:
            os.remove(DB_PATH + _suffix)
        except OSError:
            pass

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
