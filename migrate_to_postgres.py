"""Перенос данных SQLite -> PostgreSQL.

Схему на приёмнике строит сам `db.DataBase`: конструктор прогоняет
`_auto_migrate`, дедуп и инварианты, то есть новая база сразу такая же, какой
её сделал бы обычный старт бота. Этот скрипт занимается только ДАННЫМИ.

Порядок таблиц не важен: внешних ключей в схеме нет (проверено grep'ом по
REFERENCES) — связи держатся кодом, а не базой.

Ключевой момент — `schema_migrations` ПЕРЕНОСИТСЯ, а не пропускается. Пустая
база отмечает свои миграции сама, но у боевой в журнале есть строки, которых на
чистой не появится (dedupe:* по наборам, которых уже нет в UNIQUES). Если
журнал не перенести, следующий старт сочтёт эти миграции неприменёнными и
повторит разрушающий дедуп поверх живых данных.

Использование:

    DATABASE_URL=postgresql://user:pass@host/cookie \\
        python migrate_to_postgres.py --sqlite data.db --dry-run
    DATABASE_URL=... python migrate_to_postgres.py --sqlite data.db

Скрипт не трогает исходный файл: SQLite открывается в режиме ro.
"""
import argparse
import os
import sqlite3
import sys
import time

import db as dbmod

# Сколько строк в одном executemany. Больше — меньше круговых обменов с
# сервером, но и больше памяти на батч; на таблице events (самой длинной)
# тысяча строк это единицы мегабайт.
BATCH = 1000


def _open_sqlite(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        sys.exit(f"Нет файла базы: {path}")
    # mode=ro: гарантия, что скрипт физически не может испортить источник.
    # immutable не ставим — база может быть в WAL и с живыми коммитами.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _sqlite_columns(src: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in src.execute(f"PRAGMA table_info({table})")]


def _count(src: sqlite3.Connection, table: str) -> int:
    return src.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _coerce(value, spec: str):
    """Значение SQLite в типе, который примет PostgreSQL.

    SQLite типизирован по значению, а не по колонке: в столбце INTEGER легко
    лежит строка '5', а в REAL — целое. PostgreSQL строг, и такая строка уронит
    вставку целого батча на середине таблицы."""
    if value is None:
        return None
    kind = spec.split(" ", 1)[0]
    if kind == "INTEGER":
        return int(value)
    if kind == "REAL":
        return float(value)
    if kind == "TEXT":
        return value if isinstance(value, str) else str(value)
    return value


def _copy_table(src: sqlite3.Connection, dst, table: str, spec: dict,
                dry: bool) -> int:
    columns = [c for c in spec if not c.startswith("__")]
    have = set(_sqlite_columns(src, table))
    if not have:
        print(f"  {table}: в источнике нет такой таблицы, пропуск")
        return 0
    # переносим пересечение: у боевой базы может не быть колонки, добавленной
    # уже после снятия дампа, а у приёмника — старой, выпиленной из схемы
    cols = [c for c in columns if c in have]
    missing = [c for c in columns if c not in have]
    if missing:
        print(f"  {table}: в источнике нет колонок {missing} — уйдут дефолты")

    total = _count(src, table)
    if dry or not total:
        print(f"  {table}: {total} строк")
        return total

    placeholders = ", ".join(["%s"] * len(cols))
    sql = (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT DO NOTHING")
    specs = [spec[c] for c in cols]
    cur = dst.cursor
    done = 0
    rows = src.execute(f"SELECT {', '.join(cols)} FROM {table}")
    while True:
        chunk = rows.fetchmany(BATCH)
        if not chunk:
            break
        cur.executemany(sql, [
            tuple(_coerce(r[i], specs[i]) for i in range(len(cols)))
            for r in chunk])
        done += len(chunk)
        print(f"  {table}: {done}/{total}", end="\r", flush=True)
    print(f"  {table}: {done}/{total} перенесено")
    return done


def _fix_sequences(dst, tables: dict):
    """Догоняет BIGSERIAL до максимального перенесённого id.

    Последовательность приёмника стоит на единице: строки пришли со своими
    id, минуя nextval. Без этого ПЕРВАЯ ЖЕ вставка после переезда возьмёт id=1
    и упадёт на первичном ключе — и так на каждой строке, пока счётчик не
    догонит таблицу."""
    for table, spec in tables.items():
        if spec.get("id", "").startswith("INTEGER PRIMARY KEY"):
            # третий аргумент is_called: на пустой таблице false, иначе
            # nextval отдал бы 2 и первый id остался бы незанятым
            dst.cursor.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 1), COALESCE(MAX(id), 0) > 0) FROM {table}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Перенос данных SQLite -> PostgreSQL")
    ap.add_argument("--sqlite", default=os.environ.get("DATABASE_PATH", "data.db"),
                    help="файл базы-источника (по умолчанию DATABASE_PATH)")
    ap.add_argument("--url", default=os.environ.get("DATABASE_URL", ""),
                    help="строка подключения приёмника (по умолчанию DATABASE_URL)")
    ap.add_argument("--dry-run", action="store_true",
                    help="только посчитать строки; схема на приёмнике всё равно "
                         "создаётся — это идемпотентно")
    ap.add_argument("--force", action="store_true",
                    help="переносить, даже если в приёмнике уже есть данные")
    args = ap.parse_args()

    if not args.url:
        sys.exit("Не задан приёмник: --url или DATABASE_URL")
    if not args.url.startswith(("postgresql://", "postgres://")):
        sys.exit("--url должен быть postgresql://-строкой")

    src = _open_sqlite(args.sqlite)
    print(f"[*] источник: {args.sqlite}")
    print("[*] приёмник: postgres, создаю/проверяю схему…")
    # url передаём явным аргументом: полагаться на os.environ здесь нельзя,
    # адрес мог прийти флагом
    dst = dbmod.DataBase(db_file=":memory:", url=args.url)
    # на время переноса предохранитель снимаем: батч в тысячу строк по большой
    # таблице законно идёт дольше минуты, и убивать его по таймауту нечем
    dst._set_statement_timeout(ms=0)

    tables = dict(dst.tables_schema)
    occupied = [t for t in tables if dst.q1(f"SELECT 1 AS x FROM {t} LIMIT 1")]
    if occupied and not args.force:
        sys.exit(f"В приёмнике уже есть данные: {', '.join(occupied)}. "
                 f"Перенос с ON CONFLICT DO NOTHING оставит их как есть и "
                 f"смешает две базы. Нужен пустой приёмник или --force.")

    started = time.time()
    # журнал миграций идёт первым и отдельно: он не в tables_schema, но без
    # него приёмник повторит дедуп (см. модульную docstring)
    print("[*] перенос:")
    order = {"schema_migrations": dst.MIGRATIONS_SCHEMA, **tables}
    moved = 0
    for table, spec in order.items():
        moved += _copy_table(src, dst, table, spec, args.dry_run)

    if args.dry_run:
        print(f"[*] dry-run: перенеслось бы {moved} строк, ничего не записано")
        return 0

    _fix_sequences(dst, tables)
    dst.connection.commit()
    print(f"[*] готово: {moved} строк за {time.time() - started:.1f} с")

    print("[*] сверка:")
    bad = 0
    for table in order:
        if not _sqlite_columns(src, table):
            continue
        was = _count(src, table)
        now = dst.q1(f"SELECT COUNT(*) AS c FROM {table}")["c"]
        if was != now:
            bad += 1
            print(f"  РАСХОЖДЕНИЕ {table}: было {was}, стало {now}")
    if bad:
        print(f"[!] таблиц с расхождением: {bad}. Скорее всего в источнике были "
              f"дубли по уникальному индексу — их отсеял ON CONFLICT DO NOTHING.")
        return 1
    print("  все таблицы совпали")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
