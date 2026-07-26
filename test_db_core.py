"""Примитивы БД: миграции, rowcount, RETURNING, диалект, потоки, транзакции.

На этих гарантиях стоит вся Этап-1 экономика: идемпотентность клеймов и
покупок читается ИСКЛЮЧИТЕЛЬНО из числа затронутых строк. Если exec() соврёт
хоть раз — награда выдастся дважды, поэтому набор проверяет именно контракт,
а не игровые правила.

Запуск: python test_db_core.py
"""
import os
import sqlite3
import tempfile
import threading
import time

# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается
DB_PATH = os.path.join(tempfile.gettempdir(), f"cookie_dbcore_{os.getpid()}.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + suffix):
        os.remove(DB_PATH + suffix)
os.environ["DATABASE_PATH"] = DB_PATH

from db import DataBase  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


db = DataBase()
UID = 900001
db.create_user(UID, "dbcore", "DbCore")

print("\n=== exec() возвращает число затронутых строк ===")

check("UPDATE, попавший в строку, даёт 1",
      db.exec("UPDATE users SET cookies = 5 WHERE user_id = ?", (UID,)) == 1)
check("UPDATE мимо условия даёт 0",
      db.exec("UPDATE users SET cookies = 5 WHERE user_id = ?", (-1,)) == 0)

# главное, ради чего lastrowid уехал: проигнорированная вставка обязана
# отличаться от прошедшей. lastrowid тут врёт — остаётся значение от прошлой
db.exec("DELETE FROM achievements WHERE user_id = ?", (UID,))
first = db.exec("INSERT INTO achievements (user_id, key, claimed) VALUES (?, ?, 0) "
                "ON CONFLICT (user_id, key) DO NOTHING", (UID, "probe"))
second = db.exec("INSERT INTO achievements (user_id, key, claimed) VALUES (?, ?, 0) "
                 "ON CONFLICT (user_id, key) DO NOTHING", (UID, "probe"))
check("ON CONFLICT DO NOTHING: 1, затем 0", (first, second) == (1, 0), f"{first},{second}")

# lastrowid после проигнорированной вставки остаётся от предыдущей — ровно та
# ловушка, из-за которой идемпотентность нельзя строить на нём
check("last_insert_id доступен отдельно", isinstance(db.last_insert_id, int))

print("\n=== q1w(): RETURNING ===")

row = db.q1w("UPDATE users SET cookies = cookies + 10 WHERE user_id = ? "
             "RETURNING cookies", (UID,))
check("RETURNING отдаёт новое значение", row and row["cookies"] == 15, str(row))
check("несошедшееся условие даёт None",
      db.q1w("UPDATE users SET cookies = 1 WHERE user_id = ? RETURNING cookies",
             (-1,)) is None)
check("ON CONFLICT DO NOTHING + RETURNING даёт None",
      db.q1w("INSERT INTO achievements (user_id, key, claimed) VALUES (?, ?, 0) "
             "ON CONFLICT (user_id, key) DO NOTHING RETURNING id",
             (UID, "probe")) is None)

print("\n=== диалектный шим ===")

check("на sqlite плейсхолдеры не трогаются",
      db._sql("SELECT ? , ?") == "SELECT ? , ?")
try:
    db.DIALECT = "postgres"
    check("на postgres ? -> %s", db._sql("SELECT ?") == "SELECT %s")
finally:
    db.DIALECT = "sqlite"
check("GREATEST/LEAST развёрнуты в скалярные MAX/MIN",
      (db.GREATEST, db.LEAST) == ("MAX", "MIN"))
clamped = db.q1w(f"UPDATE users SET cookies = {db.GREATEST}(0, cookies - ?) "
                 f"WHERE user_id = ? RETURNING cookies", (1000, UID))
check("кламп в ноль вместо минуса", clamped and clamped["cookies"] == 0, str(clamped))

print("\n=== журнал миграций ===")

check("миграции записаны", len(db.q("SELECT name FROM schema_migrations")) > 0)
check("применённая миграция больше не нужна", db._migration("dedupe:board") is False)
check("незнакомая миграция нужна", db._migration("nope:никогда") is True)
db._mark("nope:никогда")
check("после _mark миграция считается применённой",
      db._migration("nope:никогда") is False)
db._mark("nope:никогда")  # повторный _mark не должен падать на PRIMARY KEY
check("повторный _mark идемпотентен",
      len(db.q("SELECT 1 x FROM schema_migrations WHERE name = ?", ("nope:никогда",))) == 1)

print("\n=== перенос наград пасса в строки ===")

# без переноса миграция ДАРИТ пройденный пасс: код смотрит только на строки
db.exec("DELETE FROM bp_claims WHERE user_id = ?", (UID,))
db.update_user(UID, season_id=7, bp_claimed_free='[1, 2, 2, 3]',
               bp_claimed_premium='[1]')
db._backfill_bp_claims()
moved = db.q("SELECT track, level, season_id FROM bp_claims WHERE user_id = ? "
             "ORDER BY track, level", (UID,))
check("перенесены оба трека без дублей",
      [(r["track"], r["level"]) for r in moved]
      == [("free", 1), ("free", 2), ("free", 3), ("premium", 1)], str(moved))
check("сезон взят из строки игрока", all(r["season_id"] == 7 for r in moved))

# битый json от древнего сбоя не должен валить подъём процесса
db.exec("DELETE FROM bp_claims WHERE user_id = ?", (UID,))
db.update_user(UID, bp_claimed_free='[1, 2', bp_claimed_premium='[4]')
db._backfill_bp_claims()
check("битый json пропущен, целый перенесён",
      [r["level"] for r in db.q("SELECT level FROM bp_claims WHERE user_id = ? "
                                "AND track = 'premium'", (UID,))] == [4])
db.exec("DELETE FROM bp_claims WHERE user_id = ?", (UID,))
db.update_user(UID, bp_claimed_free='[]', bp_claimed_premium='[]')
check("перенос отмечен в журнале и второй раз не пойдёт",
      db._migration("backfill_bp_claims") is False)

print("\n=== уникальные индексы ===")

names = {r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type = 'index'")}
check("исторические имена индексов сохранены",
      "uq_purchases_charge" in names and "uq_board_user_id_cell" in names,
      str(sorted(n for n in names if n.startswith("uq_"))))
db.exec("DELETE FROM purchases WHERE user_id = ?", (UID,))
db.exec("INSERT INTO purchases (user_id, item_key, tg_payment_id) VALUES (?, 'x', NULL)",
        (UID,))
db.exec("INSERT INTO purchases (user_id, item_key, tg_payment_id) VALUES (?, 'x', NULL)",
        (UID,))
check("частичный индекс не мешает нескольким NULL",
      len(db.q("SELECT id FROM purchases WHERE user_id = ?", (UID,))) == 2)
db.exec("INSERT INTO purchases (user_id, item_key, tg_payment_id) VALUES (?, 'x', 'ch1')",
        (UID,))
try:
    db.exec("INSERT INTO purchases (user_id, item_key, tg_payment_id) "
            "VALUES (?, 'x', 'ch1')", (UID,))
    check("повторный charge_id отбит", False, "вставка прошла")
except sqlite3.IntegrityError:
    check("повторный charge_id отбит", True)

print("\n=== транзакции ===")

db.update_user(UID, cookies=100)
try:
    with db.tx():
        db.exec("UPDATE users SET cookies = 777 WHERE user_id = ?", (UID,))
        raise RuntimeError("откат")
except RuntimeError:
    pass
check("исключение откатывает всё", db.get_user(UID)["cookies"] == 100)

with db.tx():
    db.exec("UPDATE users SET cookies = 1 WHERE user_id = ?", (UID,))
    with db.tx():  # вложенная присоединяется, а не коммитит сама
        check("вложенная tx() видит глубину > 1", db._tx_depth == 2)
        db.exec("UPDATE users SET cookies = 2 WHERE user_id = ?", (UID,))
check("после выхода глубина обнулена", db._tx_depth == 0)
check("вложенный блок закоммичен вместе с внешним", db.get_user(UID)["cookies"] == 2)

# BEGIN упал — глубина обязана остаться нулевой, иначе следующая tx() решит,
# что она вложенная, и молча не закоммитит
real_cursor = DataBase.cursor


class _Boom:
    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")


DataBase.cursor = property(lambda self: _Boom())
t0 = time.time()
try:
    with db.tx():
        pass
    check("занятая база в итоге падает", False, "исключения не было")
except sqlite3.OperationalError:
    check("занятая база в итоге падает", True)
finally:
    DataBase.cursor = real_cursor
check("перед падением были повторы с паузой", time.time() - t0 >= 0.15,
      f"{time.time() - t0:.3f}s")
check("глубина не отравлена неудачным BEGIN", db._tx_depth == 0)
db.exec("UPDATE users SET cookies = 42 WHERE user_id = ?", (UID,))
check("после неудачного BEGIN запись снова коммитится",
      db.get_user(UID)["cookies"] == 42)

print("\n=== состояние по потокам ===")

seen = {}


def worker():
    # у потока своё соединение и свой курсор: rowcount одного не сбивается
    # SELECT'ом другого
    seen["cur"] = db.cursor
    seen["conn"] = db.connection
    seen["rc"] = db.exec("UPDATE users SET cookies = cookies + 1 WHERE user_id = ?", (UID,))
    with db.tx():
        seen["depth"] = db._tx_depth


t = threading.Thread(target=worker)
t.start()
t.join()
check("у потока свой курсор", seen["cur"] is not db.cursor)
check("у потока своё соединение", seen["conn"] is not db.connection)
check("запись из потока прошла", seen["rc"] == 1)
check("глубина потока независима", seen["depth"] == 1 and db._tx_depth == 0)
check("данные видны из главного потока", db.get_user(UID)["cookies"] == 43)

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
