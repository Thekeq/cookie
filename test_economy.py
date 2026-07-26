"""Книга операций: инварианты, идемпотентность, атомарность трат, сверка.

Три вещи, ради которых написан файл:
  1. баланс нельзя испортить (NaN, бесконечность, абсурдная величина);
  2. книгу нельзя переписать задним числом;
  3. после обычной сессии сумма движений СХОДИТСЯ с колонкой — а если кто-то
     напишет мимо книги, сверка это покажет.

Известные незакрытые дыры (сознательно, каждая — свой шаг плана): сброс престижа
обнуляет cookies напрямую (S14), отзыв покупки за Stars списывает напрямую (S17).
Реген и трата энергии не пишутся в книгу и не будут — энергия производная от
времени, а не запас (см. LEDGERED_PARTIAL); в книгу идут только её выдачи.
Поэтому drift сверяется по cookies, xp и total_earned — по тем валютам, которые
УЖЕ полностью в книге.
"""
import hashlib
import hmac
import json
import math
import os
import sqlite3
import sys
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается
import tempfile
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.gettempdir(), f"cookie_econ_{os.getpid()}.db")

from fastapi.testclient import TestClient

from main import app
import server.economy as ec
import server.game_logic as gl
import server.game_config as cfg
from server.game_logic import db

BOT_TOKEN = os.environ["BOT_TOKEN"]
c = TestClient(app)

_ok = _fail = 0


def check(name, cond, extra=""):
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"OK   {name}")
    else:
        _fail += 1
        print(f"FAIL {name} {extra}")


def raises(name, fn, exc=Exception, match=""):
    """Проверяет, что вызов падает нужным типом (и, если задано, с нужным текстом)."""
    try:
        fn()
    except exc as e:
        check(name, (match in str(e)) if match else True, f"текст: {e}")
        return
    except Exception as e:  # упало, но не тем
        check(name, False, f"ожидали {exc.__name__}, получили {type(e).__name__}: {e}")
        return
    check(name, False, "не упало вовсе")


def sign(user_id, username="econ", first_name="Econ"):
    data = {"user": json.dumps({"id": user_id, "username": username,
                                "first_name": first_name}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def H(uid):
    return {"Authorization": "tma " + sign(uid)}


def ledger(uid, currency=None):
    sql = "SELECT * FROM economy_ledger WHERE user_id = ?"
    p = [uid]
    if currency:
        sql += " AND currency = ?"
        p.append(currency)
    return db.q(sql + " ORDER BY id", tuple(p))


BASE = 970_000_000 + int(time.time()) % 1_000_000

# ==========================================================================
# 0. Входящие остатки: игрок, пришедший в игру ДО книги
#    Идёт первым: backfill проходит по всем игрокам сразу, и запускать его
#    после того, как у остальных появилась история, значило бы приписать им
#    вторые «входящие» поверх настоящих движений.
# ==========================================================================
OLD = BASE
db.create_user(OLD, "old", "Old")
db.update_user(OLD, cookies=12345.5, total_earned=99999.0, xp=700.0,
               prestige_points=3)

check("0.1 старый игрок без книги — сверка видит расхождение",
      abs(ec.reconcile(OLD)["cookies"]["drift"] - 12345.5) < 1e-6,
      ec.reconcile(OLD)["cookies"])

db.exec("DELETE FROM schema_migrations WHERE name = ?", ("backfill:ledger_opening",))
ec.backfill_opening()

st = ec.reconcile(OLD)
check("0.2 после backfill cookies сходятся", abs(st["cookies"]["drift"]) < 1e-6, st["cookies"])
check("0.3 после backfill total_earned сходится",
      abs(st["total_earned"]["drift"]) < 1e-6, st["total_earned"])
check("0.4 входящий остаток — одна строка на валюту",
      len(ledger(OLD, "cookies")) == 1 and ledger(OLD, "cookies")[0]["reason"] == "opening_balance")
check("0.5 нулевые валюты входящих строк не получили",
      len(ledger(OLD, "offline_hours")) == 0)
check("0.6 снимок earned сохранён",
      (db.q1("SELECT total_earned FROM economy_opening WHERE user_id = ?",
             (OLD,)) or {}).get("total_earned") == 99999.0)

ec.backfill_opening()  # повторный вызов после отметки — должен быть no-op
check("0.7 backfill не повторяется", len(ledger(OLD, "cookies")) == 1)

# ==========================================================================
# 1. Инварианты баланса (триггер trg_users_balance_sane)
# ==========================================================================
U = BASE + 1
db.create_user(U, "u", "U")
gl.add_cookies(U, 1000.0)
before = db.get_user(U)["cookies"]

raises("1.1 NaN в баланс не пролезает",
       lambda: db.update_user(U, cookies=float("nan")), sqlite3.IntegrityError)
raises("1.2 бесконечность не пролезает",
       lambda: db.update_user(U, cookies=float("inf")), sqlite3.IntegrityError)
raises("1.3 абсурдный плюс не пролезает",
       lambda: db.update_user(U, cookies=1e20), sqlite3.IntegrityError)
raises("1.4 абсурдный минус не пролезает",
       lambda: db.update_user(U, cookies=-2e6), sqlite3.IntegrityError)
check("1.5 после отбитых записей баланс цел", db.get_user(U)["cookies"] == before)

db.update_user(U, cookies=before)  # нормальная запись проходит
check("1.6 нормальная запись проходит", db.get_user(U)["cookies"] == before)

raises("1.7 NaN не доходит до SQL — его ловит _sane",
       lambda: gl.add_cookies(U, float("nan")), ValueError, "err_bad_amount")
raises("1.8 бесконечность ловится до SQL",
       lambda: gl.add_cookies(U, float("inf")), ValueError, "err_bad_amount")
raises("1.9 None ловится до SQL",
       lambda: gl.add_cookies(U, None), ValueError, "err_bad_amount")
raises("1.10 величина за пределом диапазона ловится до SQL",
       lambda: gl.add_cookies(U, 1e16), ValueError, "err_bad_amount")
check("1.11 отбитые начисления не оставили следа в книге",
      len(ledger(U, "cookies")) == 1)

# ==========================================================================
# 2. Книга только дописывается
# ==========================================================================
rid = ledger(U, "cookies")[0]["id"]
raises("2.1 строку книги нельзя изменить",
       lambda: db.exec("UPDATE economy_ledger SET amount = 0 WHERE id = ?", (rid,)),
       sqlite3.IntegrityError, "append-only")
raises("2.2 строку книги нельзя удалить",
       lambda: db.exec("DELETE FROM economy_ledger WHERE id = ?", (rid,)),
       sqlite3.IntegrityError, "append-only")
check("2.3 строка на месте и не тронута",
      len(ledger(U, "cookies")) == 1 and ledger(U, "cookies")[0]["amount"] == 1000.0)

# ==========================================================================
# 3. record: повтор одной операции не удваивает движение
# ==========================================================================
R = BASE + 2
db.create_user(R, "r", "R")
op = "test-op-dup"
ec.record(R, "cookies", 50, "unit", 50, op)
raises("3.1 дубль (op, currency, seq) по умолчанию рвёт транзакцию",
       lambda: ec.record(R, "cookies", 50, "unit", 50, op), sqlite3.IntegrityError)
ec.record(R, "cookies", 50, "unit", 50, op, idempotent=True)
check("3.1b с idempotent=True дубль гасится молча",
      len(ledger(R, "cookies")) == 1)
check("3.1c already_recorded видит записанное движение",
      ec.already_recorded(op, "cookies") and not ec.already_recorded(op, "cookies", 9))
ec.record(R, "cookies", 50, "unit", 100, op, seq=1)
check("3.2 другой seq — законное второе движение той же операции",
      len(ledger(R, "cookies")) == 2)
ec.record(R, "xp", 10, "unit", 10, op)
check("3.3 другая валюта той же операции пишется отдельно",
      len(ledger(R, "xp")) == 1)
ec.record(R, "stars", 100, "purchase", 0, "test-op-stars")
check("3.4 Stars помечены как внешняя валюта", ledger(R, "stars")[0]["external"] == 1)
check("3.5 внешняя валюта в сверку не входит", "stars" not in ec.reconcile(R))

# ==========================================================================
# 4. add_cookies
# ==========================================================================
A = BASE + 3
db.create_user(A, "a", "A")
bal = gl.add_cookies(A, 500.0, reason="test_mint", ref_type="unit", ref_id="x")
row = ledger(A, "cookies")[-1]
check("4.1 возвращён новый баланс", bal == 500.0, bal)
check("4.2 колонка обновилась", db.get_user(A)["cookies"] == 500.0)
check("4.3 в книге сумма, причина и ссылка",
      row["amount"] == 500.0 and row["reason"] == "test_mint"
      and row["ref_type"] == "unit" and row["ref_id"] == "x")
check("4.4 balance_after совпадает с балансом", row["balance_after"] == 500.0)
check("4.5 заработок засчитан", row["counts_earned"] == 1
      and db.get_user(A)["total_earned"] == 500.0)

gl.add_cookies(A, 300.0, count_earned=False, reason="compensation")
check("4.6 count_earned=False не двигает total_earned",
      db.get_user(A)["total_earned"] == 500.0)
check("4.7 ...но движение в книге есть",
      ledger(A, "cookies")[-1]["amount"] == 300.0
      and ledger(A, "cookies")[-1]["counts_earned"] == 0)

gl.add_cookies(A, -100.0, reason="penalty")
check("4.8 отрицательное начисление не идёт в заработок",
      db.get_user(A)["total_earned"] == 500.0 and db.get_user(A)["cookies"] == 700.0)

raises("4.9 начисление несуществующему игроку падает",
       lambda: gl.add_cookies(BASE + 999_999, 10.0), ValueError, "err_no_user")
check("4.10 и не оставляет висячей строки в книге",
      len(ledger(BASE + 999_999)) == 0)

gl.add_cookies(A, 42.0, operation_id="fixed-op-1", reason="rewarded")
second = gl.add_cookies(A, 42.0, operation_id="fixed-op-1", reason="rewarded")
check("4.11 явный operation_id пишет ровно одну строку книги",
      len([r for r in ledger(A, "cookies") if r["operation_id"] == "fixed-op-1"]) == 1)
check("4.12 повтор по тому же токену НЕ двигает баланс второй раз",
      db.get_user(A)["cookies"] == 742.0, db.get_user(A)["cookies"])
check("4.13 идемпотентный повтор возвращает текущий баланс", second == 742.0, second)
check("4.14 и не задваивает заработок", db.get_user(A)["total_earned"] == 542.0,
      db.get_user(A)["total_earned"])
check("4.15 после идемпотентного повтора сверка сходится",
      abs(ec.reconcile(A)["cookies"]["drift"]) < 1e-6, ec.reconcile(A)["cookies"])

# без токена повтор — законное второе начисление
n = len(ledger(A, "cookies"))
gl.add_cookies(A, 5.0, reason="tick")
gl.add_cookies(A, 5.0, reason="tick")
check("4.16 без токена два одинаковых начисления проходят оба",
      len(ledger(A, "cookies")) == n + 2 and db.get_user(A)["cookies"] == 752.0)

# дубль токена без предварительной проверки — рвёт транзакцию, а не молчит
raises("4.17 record без idempotent на дубле падает, а не глушит",
       lambda: ec.record(A, "cookies", 1, "unit", 0, "fixed-op-1"),
       sqlite3.IntegrityError)
check("4.18 сверка цела и после отбитого дубля",
      abs(ec.reconcile(A)["cookies"]["drift"]) < 1e-6)

# ==========================================================================
# 5. spend_cookies — проверка и списание одним стейтментом
# ==========================================================================
S = BASE + 4
db.create_user(S, "s", "S")
gl.add_cookies(S, 1000.0)
n_before = len(ledger(S, "cookies"))

left = gl.spend_cookies(S, 250.0, "unit_spend", ref_type="thing", ref_id="k")
check("5.1 списание вернуло остаток", left == 750.0, left)
check("5.2 колонка уменьшилась", db.get_user(S)["cookies"] == 750.0)
spent = ledger(S, "cookies")[-1]
check("5.3 в книге трата со знаком минус", spent["amount"] == -250.0)
check("5.4 трата не считается заработком",
      spent["counts_earned"] == 0 and db.get_user(S)["total_earned"] == 1000.0)

raises("5.5 списание сверх баланса отказывает",
       lambda: gl.spend_cookies(S, 100_000.0, "too_much"), gl.NoFunds, "err_no_cookies")
check("5.6 ПРИЁМКА: баланс после отказа не изменился",
      db.get_user(S)["cookies"] == 750.0, db.get_user(S)["cookies"])
check("5.7 ПРИЁМКА: отказ не написал строку в книгу",
      len(ledger(S, "cookies")) == n_before + 1)
check("5.8 после отказа сверка по-прежнему сходится",
      abs(ec.reconcile(S)["cookies"]["drift"]) < 1e-6, ec.reconcile(S)["cookies"])

check("5.9 списание ровно по балансу проходит",
      gl.spend_cookies(S, 750.0, "exact") == 0.0)
check("5.10 баланс обнулился ровно", db.get_user(S)["cookies"] == 0.0)
raises("5.11 с нуля не списывается даже копейка",
       lambda: gl.spend_cookies(S, 0.01, "nope"), gl.NoFunds)
check("5.12 сверка сошлась и на нуле",
      abs(ec.reconcile(S)["cookies"]["drift"]) < 1e-6)

raises("5.13 NaN в трате ловится до SQL",
       lambda: gl.spend_cookies(S, float("nan"), "bad"), ValueError, "err_bad_amount")

gl.add_cookies(S, 400.0)
gl.spend_cookies(S, 150.0, "paid", operation_id="spend-once")
rest = gl.spend_cookies(S, 150.0, "paid", operation_id="spend-once")
check("5.14 повторное списание по тому же токену не снимает второй раз",
      db.get_user(S)["cookies"] == 250.0 and rest == 250.0, db.get_user(S)["cookies"])
check("5.15 и сверка после этого сходится",
      abs(ec.reconcile(S)["cookies"]["drift"]) < 1e-6)

# ==========================================================================
# 6. buy_click_upgrade — устаревший click_level не проходит
# ==========================================================================
K = BASE + 5
db.create_user(K, "k", "K")
gl.add_cookies(K, 10_000.0)
lvl = db.get_user(K)["click_level"]

raises("6.1 покупка с чужим click_level отбита",
       lambda: gl.buy_click_upgrade(K, 100.0, lvl + 5), gl.NoFunds)
check("6.2 после отбитой покупки уровень и баланс целы",
      db.get_user(K)["click_level"] == lvl and db.get_user(K)["cookies"] == 10_000.0)
check("6.3 отбитая покупка не написала в книгу", len(ledger(K, "cookies")) == 1)

gl.buy_click_upgrade(K, 100.0, lvl)
check("6.4 покупка с актуальным уровнем прошла",
      db.get_user(K)["click_level"] == lvl + 1 and db.get_user(K)["cookies"] == 9900.0)
check("6.5 списание попало в книгу",
      ledger(K, "cookies")[-1]["amount"] == -100.0
      and ledger(K, "cookies")[-1]["reason"] == "click_upgrade")
raises("6.6 повтор с уже устаревшим уровнем отбит",
       lambda: gl.buy_click_upgrade(K, 100.0, lvl), gl.NoFunds)
check("6.7 сверка сходится после серии покупок",
      abs(ec.reconcile(K)["cookies"]["drift"]) < 1e-6)

# ==========================================================================
# 7. Токены операций
# ==========================================================================
O = BASE + 6
db.create_user(O, "o", "O")

raises("7.1 begin_op вне транзакции — ошибка",
       lambda: ec.begin_op("op-outside", O, "unit"), RuntimeError, "вне транзакции")

with db.tx():
    first = ec.begin_op("op-1", O, "unit")
check("7.2 первый вызов отдаёт None — операция наша", first is None)

with db.tx():
    ec.finish_op("op-1", {"reward": 7})

with db.tx():
    replay = ec.begin_op("op-1", O, "unit")
check("7.3 ретрай получает сохранённый ответ", replay == {"reward": 7}, replay)

with db.tx():
    replay2 = ec.begin_op("op-1", BASE + 777, "unit")
check("7.4 токен глобальный: чужой user_id получает тот же ответ",
      replay2 == {"reward": 7})

with db.tx():
    ec.finish_op("op-1", {"reward": 999})
with db.tx():
    again = ec.begin_op("op-1", O, "unit")
check("7.5 закрытую операцию нельзя переписать", again == {"reward": 7}, again)

def _begin_open():
    with db.tx():
        ec.begin_op("op-open", O, "unit")


_begin_open()   # открыли и не закрыли
raises("7.6 незакрытая операция — конфликт, а не тихий повтор",
       _begin_open, ec.ConflictError, "err_state_conflict")

# откат: эффект и токен уходят вместе
try:
    with db.tx():
        ec.begin_op("op-rollback", O, "unit")
        gl.add_cookies(O, 500.0, operation_id="op-rollback")
        raise RuntimeError("падение на середине")
except RuntimeError:
    pass
check("7.7 упавшая операция не оставила токена",
      db.q1("SELECT 1 x FROM economy_ops WHERE operation_id = ?", ("op-rollback",)) is None)
check("7.8 ...и не оставила ни денег, ни строки книги",
      db.get_user(O)["cookies"] == 0 and len(ledger(O, "cookies")) == 0)

with db.tx():
    if ec.begin_op("op-rollback", O, "unit") is None:
        gl.add_cookies(O, 500.0, operation_id="op-rollback")
        ec.finish_op("op-rollback", {"ok": True})
check("7.9 повтор после отката выдаёт награду ровно один раз",
      db.get_user(O)["cookies"] == 500.0)
with db.tx():
    r = ec.begin_op("op-rollback", O, "unit")
check("7.10 а следующий ретрай — уже реплей", r == {"ok": True}
      and db.get_user(O)["cookies"] == 500.0)

# ==========================================================================
# 8. ПРИЁМКА: сверка после живой сессии через API
# ==========================================================================
P = BASE + 7
r = c.post("/api/auth", headers=H(P))
check("8.1 игрок завёлся через API", r.status_code == 200, r.text)

gl.add_cookies(P, 5_000_000.0, reason="test_grant")
for i in range(6):
    c.post("/api/click", json={"clicks": 30, "batch_id": f"b{i}"}, headers=H(P))
c.post("/api/click", json={"clicks": 30, "batch_id": "b0"}, headers=H(P))  # дубль
c.post("/api/click/upgrade", headers=H(P))
c.post("/api/merge/spawn", json={"level": 1}, headers=H(P))
c.post("/api/merge/spawn", json={"level": 1}, headers=H(P))
for key in list(cfg.FARM_BUILDINGS)[:2]:
    c.post("/api/farm/buy_building", json={"key": key}, headers=H(P))
    c.post("/api/farm/buy_building", json={"key": key}, headers=H(P))
c.post("/api/daily/claim", headers=H(P))
c.get("/api/farm", headers=H(P))
c.post("/api/quests/claim", json={"quest_key": "clicks"}, headers=H(P))

state = ec.reconcile(P)
check("8.2 ПРИЁМКА: cookies сходятся после сессии",
      abs(state["cookies"]["drift"]) < 1e-6, state["cookies"])
check("8.3 ПРИЁМКА: total_earned сходится после сессии",
      abs(state["total_earned"]["drift"]) < 1e-6, state["total_earned"])
check("8.4 сессия действительно двигала деньги",
      len(ledger(P, "cookies")) >= 5, len(ledger(P, "cookies")))
check("8.5 balance_after последней строки равен колонке",
      abs(ledger(P, "cookies")[-1]["balance_after"] - db.get_user(P)["cookies"]) < 1e-6)

# ==========================================================================
# 8b. XP и bp_xp
# ==========================================================================
X = BASE + 9
db.create_user(X, "x", "X")
gl.add_xp(X, 100.0)
u = db.get_user(X)
check("8b.1 xp и bp_xp начислены", u["xp"] == 100.0 and u["bp_xp"] == 100.0)
check("8b.2 оба движения в книге под одним токеном",
      len(ledger(X, "xp")) == 1 and len(ledger(X, "bp_xp")) == 1
      and ledger(X, "xp")[0]["operation_id"] == ledger(X, "bp_xp")[0]["operation_id"])
check("8b.3 второе движение операции под seq=1", ledger(X, "bp_xp")[0]["seq"] == 1)

gl.add_xp(X, 50.0, 5.0)
u = db.get_user(X)
check("8b.4 отдельный bp_xp не равен xp", u["xp"] == 150.0 and u["bp_xp"] == 105.0)
check("8b.5 xp сходится с книгой", abs(ec.reconcile(X)["xp"]["drift"]) < 1e-6,
      ec.reconcile(X)["xp"])
check("8b.6 bp_xp из сверки исключён — его обнуляет ролловер",
      "bp_xp" not in ec.reconcile(X))

gl.add_xp(X, 0, 30.0)
check("8b.7 начисление только в пасс не трогает xp",
      db.get_user(X)["xp"] == 150.0 and db.get_user(X)["bp_xp"] == 135.0)
check("8b.8 и не пишет строку по xp", len(ledger(X, "xp")) == 2)

n_xp = len(ledger(X, "xp"))
gl.add_xp(X, 0, 0)
check("8b.9 пустое начисление не пишет и не двигает",
      len(ledger(X, "xp")) == n_xp and db.get_user(X)["xp"] == 150.0)

raises("8b.10 NaN в xp ловится до SQL",
       lambda: gl.add_xp(X, float("nan")), ValueError, "err_bad_amount")

# потолок уровней: xp переливается в пасс
db.update_user(X, level=cfg.MAX_LEVEL)
xp_before, bp_before = db.get_user(X)["xp"], db.get_user(X)["bp_xp"]
gl.add_xp(X, 1000.0)
u = db.get_user(X)
expect_bp = bp_before + 1000.0 + min(1000.0 * cfg.MAXLEVEL_XP_TO_BP, cfg.MERGE_BP_XP_CAP)
check("8b.11 на потолке уровней xp не растёт", u["xp"] == xp_before, u["xp"])
check("8b.12 ...а перелив уходит в пасс", u["bp_xp"] == expect_bp,
      f"{u['bp_xp']} != {expect_bp}")
check("8b.13 книга не написала строку по xp, которой не было",
      len(ledger(X, "xp")) == n_xp)
check("8b.14 xp по-прежнему сходится на потолке",
      abs(ec.reconcile(X)["xp"]["drift"]) < 1e-6, ec.reconcile(X)["xp"])
check("8b.15 перелив в книге равен тому, что записано в колонку",
      ledger(X, "bp_xp")[-1]["amount"] == 1000.0 + min(
          1000.0 * cfg.MAXLEVEL_XP_TO_BP, cfg.MERGE_BP_XP_CAP))

raises("8b.16 начисление xp несуществующему игроку падает",
       lambda: gl.add_xp(BASE + 999_998, 10.0), ValueError, "err_no_user")

# ==========================================================================
# 8c. Дневной счётчик заказов — относительный
# ==========================================================================
N = BASE + 10
db.create_user(N, "n", "N")
today = gl._utc_day(time.time())
gl._bump_orders_day(N, today, completed=True)
gl._bump_orders_day(N, today)
u = db.get_user(N)
check("8c.1 два прохода дают два, а не один",
      u["orders_day_count"] == 2 and u["orders_day"] == today, dict(u)["orders_day_count"])
check("8c.2 completed считается отдельно", u["orders_completed"] == 1)

gl._bump_orders_day(N, "1970-01-01")
u = db.get_user(N)
check("8c.3 новый день сбрасывает счётчик в единицу",
      u["orders_day_count"] == 1 and u["orders_day"] == "1970-01-01")
check("8c.4 всего выполненных не сбрасывается днём", u["orders_completed"] == 1)

# ==========================================================================
# 8d. Энергия: реген в SQL, выдачи в книгу, трата от фактического остатка
# ==========================================================================
E = BASE + 11
db.create_user(E, "e", "E")
CAP = gl.energy_cap(db.get_user(E))
PER = cfg.ENERGY_PER_CLICK

# --- реген
db.update_user(E, energy=0.0, energy_updated_at=time.time() - 100)
u = gl.refresh_energy(db.get_user(E))
want = 100 * cfg.ENERGY_REGEN_PER_SEC
check("8d.1 реген начислен за прошедшее время", abs(u["energy"] - want) < 2.0,
      f"{u['energy']} вместо ~{want}")
check("8d.2 ...и записан в базу", abs(db.get_user(E)["energy"] - u["energy"]) < 1e-6)

db.update_user(E, energy=0.0, energy_updated_at=time.time() - 100_000)
u = gl.refresh_energy(db.get_user(E))
check("8d.3 реген упирается в бак, а не переливает", u["energy"] == float(CAP),
      u["energy"])

# метка в будущем = параллельный запрос уже досчитал энергию за нас
db.update_user(E, energy=10.0, energy_updated_at=time.time() + 600)
u = gl.refresh_energy(db.get_user(E))
check("8d.4 метка из будущего не даёт начислить интервал второй раз",
      u["energy"] == 10.0 and db.get_user(E)["energy"] == 10.0, u["energy"])

# NULL в метке: раньше такой аккаунт просто не регенерировал бы никогда
db.exec("UPDATE users SET energy = 5.0, energy_updated_at = NULL WHERE user_id = ?",
        (E,))
u = gl.refresh_energy(db.get_user(E))
check("8d.5 NULL в метке не отравляет выражение", u["energy"] == 5.0, u["energy"])
check("8d.6 ...и метка проставляется", db.get_user(E)["energy_updated_at"] is not None)

# --- выдачи
db.update_user(E, energy=0.0, energy_updated_at=time.time())
before_rows = len(ledger(E, "energy"))
new = gl.grant_energy(E, 200.0, "energy_promo")
check("8d.7 выдача долила ровно сколько просили", abs(new - 200.0) < 0.5, new)
rows = ledger(E, "energy")
check("8d.8 выдача попала в книгу", len(rows) == before_rows + 1
      and rows[-1]["reason"] == "energy_promo" and abs(rows[-1]["amount"] - 200.0) < 0.5)

new = gl.grant_energy(E, 10_000.0, "energy_bp_reward")
check("8d.9 выдача сверх бака срезана по потолку", new == float(CAP), new)
rows = ledger(E, "energy")
check("8d.10 в книге записан ФАКТ, а не запрошенное",
      abs(rows[-1]["amount"] - (CAP - 200.0)) < 0.5, rows[-1]["amount"])

n_rows = len(ledger(E, "energy"))
new = gl.grant_energy(E, 500.0, "energy_stars_full")
check("8d.11 выдача в полный бак ничего не меняет", new == float(CAP))
check("8d.12 ...и не пишет строку на ноль", len(ledger(E, "energy")) == n_rows)

check("8d.13 нулевая выдача — no-op", gl.grant_energy(E, 0.0, "energy_promo") == float(CAP))
raises("8d.14 NaN в выдаче ловится до SQL",
       lambda: gl.grant_energy(E, float("nan"), "energy_promo"),
       ValueError, "err_bad_amount")

# идемпотентность: тот же токен не доливает второй раз
db.update_user(E, energy=0.0, energy_updated_at=time.time())
op = f"test:energy:{E}"
first = gl.grant_energy(E, 100.0, "energy_promo", operation_id=op)
second = gl.grant_energy(E, 100.0, "energy_promo", operation_id=op)
check("8d.15 повтор токена не доливает",
      abs(first - 100.0) < 0.5 and second == first,
      f"{first} / {second}")
check("8d.16 ...и не удваивает строку в книге",
      len([r for r in ledger(E, "energy") if r["operation_id"] == op]) == 1)

# --- трата под клики
db.update_user(E, energy=100.0, energy_updated_at=time.time())
check("8d.17 хватает энергии — оплачены все клики",
      gl.spend_energy_clicks(E, 40) == 40)
check("8d.18 ...и списано ровно за них",
      abs(db.get_user(E)["energy"] - (100.0 - 40 * PER)) < 1e-6,
      db.get_user(E)["energy"])

db.update_user(E, energy=7.0 * PER, energy_updated_at=time.time())
paid = gl.spend_energy_clicks(E, 50)
check("8d.19 не хватает — оплачено столько, на сколько хватило", paid == 7, paid)
check("8d.20 ...и энергия не ушла в минус", db.get_user(E)["energy"] >= 0,
      db.get_user(E)["energy"])

db.update_user(E, energy=0.0, energy_updated_at=time.time())
check("8d.21 пустой бак — ни одного оплаченного клика",
      gl.spend_energy_clicks(E, 50) == 0)
check("8d.22 нулевой батч не трогает базу", gl.spend_energy_clicks(E, 0) == 0
      and db.get_user(E)["energy"] == 0.0)

# два батча вплотную не могут оплатиться из одного и того же остатка
db.update_user(E, energy=30.0 * PER, energy_updated_at=time.time())
a = gl.spend_energy_clicks(E, 20)
b = gl.spend_energy_clicks(E, 20)
check("8d.23 два батча вплотную суммарно не превышают бак", a + b == 30,
      f"{a} + {b}")

check("8d.24 трата энергии в книгу не пишется — она обратная сторона регена",
      all(r["amount"] > 0 for r in ledger(E, "energy")))
check("8d.25 энергия по-прежнему вне сверки", "energy" not in ec.reconcile(E))

# ==========================================================================
# 8e. Окно античита копится в SQL и не восстанавливается двумя батчами вплотную
# ==========================================================================
W = BASE + 12
db.create_user(W, "w", "W")
t0 = time.time()
gl.bump_click_window(W, "2030-01-01", 30, t0)
u = db.get_user(W)
check("8e.1 первый батч получает полный запас минус свои клики",
      abs(u["cps_allowance"] - (cfg.MAX_CPS * 3 - 30)) < 1e-6, u["cps_allowance"])
check("8e.2 клики посчитаны", u["total_clicks"] == 30 and u["clicks_day_count"] == 30)

gl.bump_click_window(W, "2030-01-01", 30, t0)  # тот же момент времени
u = db.get_user(W)
check("8e.3 батч вплотную не намерил себе допуск заново",
      abs(u["cps_allowance"] - (cfg.MAX_CPS * 3 - 60)) < 1e-6, u["cps_allowance"])
check("8e.4 ...и клики сложились, а не затёрлись", u["clicks_day_count"] == 60)

gl.bump_click_window(W, "2030-01-01", 0, t0 + 10)
u = db.get_user(W)
check("8e.5 за 10 секунд окно упирается в трёхсекундный запас",
      abs(u["cps_allowance"] - cfg.MAX_CPS * 3) < 1e-6, u["cps_allowance"])

gl.bump_click_window(W, "2030-01-02", 5, t0 + 20)
u = db.get_user(W)
check("8e.6 новый день сбрасывает дневной счётчик, но не общий",
      u["clicks_day_count"] == 5 and u["total_clicks"] == 65, dict(u)["clicks_day_count"])

# ==========================================================================
# 8f. Ежедневная награда: право на неё решает база, стрик считается там же
# ==========================================================================
DL = BASE + 13
db.create_user(DL, "dl", "DL")
NOW = time.time()
WEEK = gl._iso_week(NOW)
D0 = gl._day_start(NOW)


def daily_setup(claimed_at, streak, freeze_week=None):
    db.update_user(DL, daily_claimed_at=claimed_at, daily_streak=streak,
                   streak_freeze_week=freeze_week)


daily_setup(D0 - 86400 + 3600, 3)          # забрал вчера
r = gl.claim_daily(db.get_user(DL))
check("8f.1 забирал вчера — стрик растёт", r["streak"] == 4, r)
check("8f.2 заморозка не тратилась", r["freeze_used"] is False)
check("8f.3 награда начислена и попала в книгу",
      any(x["reason"] == "daily" or x["amount"] == r["reward"]
          for x in ledger(DL, "cookies")), ledger(DL, "cookies"))

raises("8f.4 второй клейм в тот же день отбит базой",
       lambda: gl.claim_daily(db.get_user(DL)), ValueError, "err_already_today")
n_rows = len(ledger(DL, "cookies"))
raises("8f.5 ...и повторно тоже", lambda: gl.claim_daily(db.get_user(DL)),
       ValueError, "err_already_today")
check("8f.6 отбитые клеймы не начислили ничего", len(ledger(DL, "cookies")) == n_rows)

# клейм по устаревшему словарю: в базе уже забрано сегодня, а на руках старая копия
daily_setup(D0 - 86400 + 3600, 7)
stale = db.get_user(DL)
gl.claim_daily(stale)                      # первый проходит
raises("8f.7 гонка двух нажатий: второй с тем же словарём проигрывает",
       lambda: gl.claim_daily(stale), ValueError, "err_already_today")
check("8f.8 стрик вырос ровно на один, а не на два",
      db.get_user(DL)["daily_streak"] == 8, db.get_user(DL)["daily_streak"])

daily_setup(D0 - 2 * 86400 + 3600, 5)      # пропустил ровно день, заморозка есть
r = gl.claim_daily(db.get_user(DL))
check("8f.9 заморозка спасает стрик", r["streak"] == 6, r)
check("8f.10 ...и помечена потраченной на эту неделю",
      r["freeze_used"] is True and db.get_user(DL)["streak_freeze_week"] == WEEK)

daily_setup(D0 - 2 * 86400 + 3600, 6, WEEK)   # заморозка на этой неделе уже была
r = gl.claim_daily(db.get_user(DL))
check("8f.11 второй пропуск за неделю сжигает стрик", r["streak"] == 1, r)
check("8f.12 ...и заморозку не тратит второй раз", r["freeze_used"] is False)

daily_setup(D0 - 5 * 86400, 9)             # пропустил много дней
check("8f.13 длинный пропуск сжигает стрик даже с заморозкой",
      gl.claim_daily(db.get_user(DL))["streak"] == 1)

daily_setup(D0 - 2 * 86400 + 3600, 0)      # стрика нет — замораживать нечего
check("8f.14 нулевой стрик заморозку не получает",
      gl.claim_daily(db.get_user(DL))["streak"] == 1)

daily_setup(None, 0)                       # первый заход в игру
r = gl.claim_daily(db.get_user(DL))
check("8f.15 первый клейм — стрик один", r["streak"] == 1, r)
check("8f.16 пустая метка не мешает охране",
      db.get_user(DL)["daily_claimed_at"] is not None)

# ==========================================================================
# 8g. Призы сезона выдаются ровно один раз
# ==========================================================================
SEA = -777                                  # свой сезон, чтобы не мешать другим
S1_ = BASE + 14
S2_ = BASE + 15
for uid, earned in ((S1_, 900_000.0), (S2_, 400_000.0)):
    db.create_user(uid, f"s{uid}", "S")
    db.update_user(uid, season_id=SEA, season_earned=earned, level=5)

gl._ensure_season_snapshot(SEA)
paid1 = db.get_user(S1_)["cookies"]
prize = db.q1("SELECT reward_cookies FROM season_results WHERE season_id = ? "
              "AND user_id = ?", (SEA, S1_))["reward_cookies"]
check("8g.1 приз выдан", paid1 > 0 and abs(paid1 - prize) < 1e-6, f"{paid1} / {prize}")
check("8g.2 снапшот записал обоих",
      db.q1("SELECT COUNT(*) c FROM season_results WHERE season_id = ?",
            (SEA,))["c"] == 2)

gl._ensure_season_snapshot(SEA)              # быстрый выход по наличию строк
check("8g.3 повторный ролловер не платит второй раз",
      db.get_user(S1_)["cookies"] == paid1, db.get_user(S1_)["cookies"])

# теряем маркер: теперь вставка снапшота ПРОЙДЁТ, и от второй выплаты защищает
# только токен операции в книге — проверяем именно этот рубеж
db.exec("DELETE FROM season_results WHERE season_id = ?", (SEA,))
gl._ensure_season_snapshot(SEA)
check("8g.4 даже без маркера книга не пропускает вторую выплату",
      db.get_user(S1_)["cookies"] == paid1, db.get_user(S1_)["cookies"])
check("8g.5 приз в книге одной строкой",
      len([r for r in ledger(S1_, "cookies")
           if r["operation_id"] == f"season_prize:{SEA}:{S1_}"]) == 1)
check("8g.6 баланс победителя сходится с книгой",
      abs(ec.reconcile(S1_)["cookies"]["drift"]) < 1e-6, ec.reconcile(S1_)["cookies"])

# ==========================================================================
# 9. drift_report ловит запись мимо книги
# ==========================================================================
D = BASE + 8
db.create_user(D, "d", "D")
gl.add_cookies(D, 100.0)
check("9.1 до порчи расхождения нет", abs(ec.reconcile(D)["cookies"]["drift"]) < 1e-6)

db.update_user(D, cookies=999.0)   # прямой UPDATE в обход книги — это и есть баг
drift = ec.reconcile(D)["cookies"]["drift"]
check("9.2 запись мимо книги даёт расхождение", abs(drift - 899.0) < 1e-6, drift)

bad = {b["user_id"] for b in ec.drift_report(limit=500)}
check("9.3 drift_report называет виноватого", D in bad)
check("9.4 ...и не трогает чистых", P not in bad and S not in bad and K not in bad)

check("9.5 сверка несуществующего игрока — пустой словарь",
      ec.reconcile(BASE + 888_888) == {})
check("9.6 энергия из сверки исключена", "energy" not in ec.reconcile(D))

print(f"\n{_ok} passed, {_fail} failed")
sys.exit(1 if _fail else 0)
