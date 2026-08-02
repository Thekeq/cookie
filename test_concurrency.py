"""S20 — конкурентность и сходимость. Последний шаг Этапа 1.

Три яруса, каждый строже предыдущего:

  T1 — повтор с тем же токеном X-Op-Id, без параллелизма вообще. Мобильная
       сеть теряет ОТВЕТ чаще, чем запрос, поэтому проверяем не «награда не
       выдалась дважды», а «повтор получил ровно тот ответ, который потерялся».
  T2 — настоящие потоки: 16 воркеров стартуют по барьеру и дерутся за одну и
       ту же строку. Здесь ловятся потерянные апдейты внутри процесса.
  T3 — настоящие процессы: несколько python'ов на один файл базы. Ловится то,
       чего не видит T2, — гонки, которые GIL и общий кэш процесса маскируют.

T4 (PostgreSQL) сюда не входит осознанно: это ворота перед переездом на
Postgres, а не перед мержем Этапа 1.

Критерий приёмки после КАЖДОГО яруса один и тот же: книга сходится с
колонками до нуля, балансы не ушли в минус и в NULL, дублей
(operation_id, currency, seq) нет, у игрока не больше одного активного заказа.

Запуск: python test_concurrency.py
Полный прогон нагрузки: CONC_OPS=10000 python test_concurrency.py
"""
import concurrent.futures as cf
import hashlib
import hmac
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается. Путь берётся из
# окружения, если он там уже есть: так дочерние процессы T3 попадают в ту же
os.environ.setdefault(
    "DATABASE_PATH",
    os.path.join(tempfile.gettempdir(), f"cookie_conc_{os.getpid()}.db"))
DB_PATH = os.environ["DATABASE_PATH"]

IS_WORKER = len(sys.argv) > 1 and sys.argv[1] == "--worker"
if not IS_WORKER:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(DB_PATH + suffix):
            os.remove(DB_PATH + suffix)

from server import economy                     # noqa: E402
from server import game_config as cfg          # noqa: E402
import server.game_logic as gl                 # noqa: E402
from server.game_logic import db               # noqa: E402

THREADS = 16
OPS = int(os.environ.get("CONC_OPS", "2000"))
PROCS = int(os.environ.get("CONC_PROCS", "4"))

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


# ---------- смешанная нагрузка (общая для T2 и T3) ----------

def do_op(uid: int, i: int):
    """Одна операция смешанной нагрузки. Кидать не должна ничего, кроме
    штатных отказов, — их глотаем: отказ по нехватке денег это нормальный
    исход гонки, а вот исключение из примитива — нет."""
    # доход мемоизируется на процесс, а награды считаются от него: без сброса
    # соседний поток подсунул бы устаревшее значение
    gl.invalidate_income(uid)
    kind = i % 6
    try:
        if kind == 0:
            gl.add_cookies(uid, 10, count_earned=True, reason="conc_mint")
        elif kind == 1:
            gl.spend_cookies(uid, 7, "conc_spend")
        elif kind == 2:
            gl.add_xp(uid, 5, bp_xp=2)
        elif kind == 3:
            gl.grant_energy(uid, 3, "conc_energy")
        elif kind == 4:
            gl.spend_energy_clicks(uid, 1)
        else:
            gl.collect_farm(db.get_user(uid))
    except gl.NoFunds:
        pass
    except ValueError:
        pass


def run_load(uids, ops, threads):
    """ops операций, размазанных по потокам и по игрокам."""
    def work(i):
        do_op(uids[i % len(uids)], i)

    with cf.ThreadPoolExecutor(threads) as ex:
        list(ex.map(work, range(ops)))


# ---------- режим дочернего процесса T3 ----------

if IS_WORKER:
    _uids = [int(x) for x in sys.argv[2].split(",")]
    _ops = int(sys.argv[3])
    run_load(_uids, _ops, 4)
    raise SystemExit(0)


# ---------- проверки сходимости ----------

def assert_converged(tag, uids):
    """Единый критерий приёмки яруса."""
    drifted = []
    for uid in uids:
        rec = economy.reconcile(uid)
        for currency, r in rec.items():
            if abs(r["drift"]) > 1e-6:
                drifted.append((uid, currency, r))
    check(f"{tag}: книга сходится с колонками", not drifted, str(drifted[:3]))

    bad = db.q("SELECT user_id, cookies, xp, energy FROM users WHERE user_id IN "
               f"({','.join('?' * len(uids))}) AND (cookies IS NULL OR cookies < 0 "
               "OR xp IS NULL OR xp < 0 OR energy IS NULL OR energy < 0)", uids)
    check(f"{tag}: нет отрицательных и NULL балансов", not bad,
          str([dict(r) for r in bad]))

    dupes = db.q("SELECT operation_id, currency, seq, COUNT(*) c FROM economy_ledger "
                 "WHERE operation_id <> '' GROUP BY operation_id, currency, seq "
                 "HAVING COUNT(*) > 1")
    check(f"{tag}: нет дублей (operation_id, currency, seq)", not dupes,
          str([dict(r) for r in dupes[:3]]))

    many = db.q("SELECT user_id, COUNT(*) c FROM orders WHERE status = 'active' "
                "GROUP BY user_id HAVING COUNT(*) > 1")
    check(f"{tag}: не больше одного активного заказа на игрока", not many,
          str([dict(r) for r in many]))


# =====================================================================
print("\n=== T1: повтор с тем же X-Op-Id отдаёт тот же ответ ===")
# =====================================================================

from fastapi.testclient import TestClient      # noqa: E402
from main import app                           # noqa: E402

BOT_TOKEN = os.environ["BOT_TOKEN"]


def sign(user_id):
    data = {"user": json.dumps({"id": user_id, "username": f"u{user_id}",
                                "first_name": "Conc"}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def H(uid, op=""):
    h = {"Authorization": "tma " + sign(uid)}
    if op:
        h["X-Op-Id"] = op
    return h


c = TestClient(app)
BASE = 910_000_000 + int(time.time()) % 1_000_000
U1 = BASE
c.post("/api/auth", headers=H(U1))


def ops_rows(uid, raw):
    return db.q("SELECT * FROM economy_ops WHERE operation_id = ?",
                (f"op:{uid}:{raw}",))


# --- ежедневная награда ---
r1 = c.post("/api/daily/claim", headers=H(U1, "tap-daily-1"))
bal_after_first = db.get_user(U1)["cookies"]
r2 = c.post("/api/daily/claim", headers=H(U1, "tap-daily-1"))
check("t1 daily: оба ответа 200", (r1.status_code, r2.status_code) == (200, 200),
      f"{r1.status_code}/{r2.status_code} {r2.text[:120]}")
check("t1 daily: тело повтора байт в байт то же",
      json.dumps(r1.json(), sort_keys=True) == json.dumps(r2.json(), sort_keys=True),
      f"{r1.text[:100]} != {r2.text[:100]}")
check("t1 daily: баланс сдвинулся один раз",
      db.get_user(U1)["cookies"] == bal_after_first)
check("t1 daily: строка токена одна", len(ops_rows(U1, "tap-daily-1")) == 1)
check("t1 daily: токен закрыт с сохранённым ответом",
      ops_rows(U1, "tap-daily-1")[0]["status"] == "done"
      and ops_rows(U1, "tap-daily-1")[0]["response"])
# без токена (старая сборка Mini App) поведение прежнее: второй клейм — отказ
r3 = c.post("/api/daily/claim", headers=H(U1))
check("t1 daily: без токена повтор по-прежнему отбивается",
      r3.status_code == 400 and "already" in r3.text, r3.text[:80])
# другой токен = другое нажатие, и оно честно упирается в «уже забрал»
r4 = c.post("/api/daily/claim", headers=H(U1, "tap-daily-2"))
check("t1 daily: другой токен не считается повтором", r4.status_code == 400,
      r4.text[:80])
check("t1 daily: проигравший токен не остался в базе",
      not ops_rows(U1, "tap-daily-2"), "токен пережил откат транзакции")

# --- покупка постройки ---
KEY = next(iter(cfg.FARM_BUILDINGS))
gl.add_cookies(U1, 100_000, count_earned=False, reason="conc_seed")
before = db.get_user(U1)["cookies"]
b1 = c.post("/api/farm/buy_building", json={"key": KEY}, headers=H(U1, "tap-buy-1"))
spent = before - db.get_user(U1)["cookies"]
b2 = c.post("/api/farm/buy_building", json={"key": KEY}, headers=H(U1, "tap-buy-1"))
check("t1 buy: оба ответа 200", (b1.status_code, b2.status_code) == (200, 200),
      f"{b1.status_code}/{b2.status_code}")
check("t1 buy: тело повтора то же",
      json.dumps(b1.json(), sort_keys=True) == json.dumps(b2.json(), sort_keys=True))
check("t1 buy: постройка куплена одна",
      gl.farm_counts(U1).get(KEY, 0) == 1, str(gl.farm_counts(U1)))
check("t1 buy: списание одно",
      abs((before - spent) - db.get_user(U1)["cookies"]) < 1e-6)

# --- награда боевого пропуска ---
db.update_user(U1, bp_xp=100_000)
bp1 = c.post("/api/battlepass/claim", json={"level": 1, "track": "free"},
             headers=H(U1, "tap-bp-1"))
bp2 = c.post("/api/battlepass/claim", json={"level": 1, "track": "free"},
             headers=H(U1, "tap-bp-1"))
check("t1 bp: оба ответа 200", (bp1.status_code, bp2.status_code) == (200, 200),
      f"{bp1.status_code}/{bp2.status_code} {bp2.text[:100]}")
check("t1 bp: тело повтора то же",
      json.dumps(bp1.json(), sort_keys=True) == json.dumps(bp2.json(), sort_keys=True))
check("t1 bp: отметка о награде одна",
      db.q1("SELECT COUNT(*) c FROM bp_claims WHERE user_id = ? AND level = 1 "
            "AND track = 'free'", (U1,))["c"] == 1)

# --- сдача заказа ---
gl.orders_state(db.get_user(U1))
_off = db.q1("SELECT id, slot FROM orders WHERE user_id = ? AND status = 'offer' "
             "ORDER BY slot", (U1,))
c.post("/api/orders/take", json={"slot": _off["slot"], "id": _off["id"]},
       headers=H(U1))
_act = db.q1("SELECT id, version FROM orders WHERE user_id = ? AND status = 'active'",
             (U1,))
db.exec("UPDATE orders SET progress = goal WHERE id = ?", (_act["id"],))
ref = {"id": _act["id"], "version": _act["version"]}
o1 = c.post("/api/orders/claim", json=ref, headers=H(U1, "tap-order-1"))
o2 = c.post("/api/orders/claim", json=ref, headers=H(U1, "tap-order-1"))
check("t1 order: оба ответа 200", (o1.status_code, o2.status_code) == (200, 200),
      f"{o1.status_code}/{o2.status_code} {o2.text[:100]}")
check("t1 order: тело повтора то же",
      json.dumps(o1.json(), sort_keys=True) == json.dumps(o2.json(), sort_keys=True))
check("t1 order: награда в книге одна",
      db.q1("SELECT COUNT(*) c FROM economy_ledger WHERE user_id = ? "
            "AND reason = 'order_reward' AND ref_id = ?",
            (U1, str(_act["id"])))["c"] == 1)

# --- токен привязан к игроку ---
U2 = BASE + 1
c.post("/api/auth", headers=H(U2))
same = c.post("/api/daily/claim", headers=H(U2, "tap-daily-1"))
check("t1: тот же сырой токен у другого игрока — своя операция",
      same.status_code == 200 and len(ops_rows(U2, "tap-daily-1")) == 1,
      f"{same.status_code} {same.text[:80]}")

# --- мусор в заголовке не должен ломать игру ---
U3 = BASE + 2
c.post("/api/auth", headers=H(U3))
junk = c.post("/api/daily/claim", headers=H(U3, "a" * 300 + " \n<>'\";"))
check("t1: длинный и грязный токен принят и обрезан",
      junk.status_code == 200
      and db.q1("SELECT COUNT(*) c FROM economy_ops WHERE user_id = ? "
                "AND LENGTH(operation_id) <= 64 + 32", (U3,))["c"] == 1,
      junk.text[:80])

# --- выдача оплаченной покупки ---
CHARGE = f"conc_charge_{BASE}"
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'cookies_pack', 100, ?, 'paid', ?)",
        (U1, CHARGE, time.time()))
f1 = gl.fulfill_charge(CHARGE)
f2 = gl.fulfill_charge(CHARGE)
check("t1 charge: выдача ровно одна", (f1, f2) == (True, False), f"{f1}/{f2}")

assert_converged("t1", [U1, U2, U3])

# =====================================================================
print("\n=== T2: 16 потоков дерутся за одну строку ===")
# =====================================================================


def race(n, fn):
    """Запускает fn(i) в n потоках, синхронизованных барьером: без него первый
    поток успевает закоммититься раньше, чем последний стартует, и гонки нет."""
    barrier = threading.Barrier(n)

    def run(i):
        barrier.wait()
        try:
            return (fn(i), None)
        except Exception as e:       # noqa: BLE001 — тут интересен сам факт
            return (None, e)

    with cf.ThreadPoolExecutor(n) as ex:
        return list(ex.map(run, range(n)))


def wins(results):
    return sum(1 for value, err in results if err is None)


# --- ежедневная награда: один победитель ---
UD = BASE + 10
db.create_user(UD, "racer_daily", "Racer")
res = race(THREADS, lambda i: gl.claim_daily(db.get_user(UD)))
check("t2 daily: ровно один клейм прошёл", wins(res) == 1,
      str([str(e) for _v, e in res if e][:2]))
check("t2 daily: стрик равен единице", db.get_user(UD)["daily_streak"] == 1)
check("t2 daily: в книге одна строка",
      db.q1("SELECT COUNT(*) c FROM economy_ledger WHERE user_id = ?", (UD,))["c"] == 1)

# --- трата: денег ровно на одну покупку ---
US = BASE + 11
db.create_user(US, "racer_spend", "Racer")
gl.add_cookies(US, 100, count_earned=False, reason="conc_seed")
res = race(THREADS, lambda i: gl.spend_cookies(US, 100, "conc_race"))
check("t2 spend: списание прошло ровно одно", wins(res) == 1)
check("t2 spend: баланс ноль, а не минус", db.get_user(US)["cookies"] == 0,
      str(db.get_user(US)["cookies"]))

# --- выдача покупки за Stars ---
UC = BASE + 12
db.create_user(UC, "racer_charge", "Racer")
CHARGE2 = f"conc_charge2_{BASE}"
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'cookies_pack', 100, ?, 'paid', ?)",
        (UC, CHARGE2, time.time()))
res = race(THREADS, lambda i: gl.fulfill_charge(CHARGE2))
check("t2 charge: True вернул ровно один поток",
      sum(1 for v, _e in res if v is True) == 1,
      str([v for v, _e in res]))

# --- взятие заказа: слот один ---
UO = BASE + 13
db.create_user(UO, "racer_order", "Racer")
gl.orders_state(db.get_user(UO))
_slot = db.q1("SELECT slot, id FROM orders WHERE user_id = ? AND status = 'offer' "
              "ORDER BY slot", (UO,))
res = race(THREADS, lambda i: gl.take_order(db.get_user(UO), _slot["slot"],
                                            _slot["id"]))
check("t2 take: заказ взят ровно один раз", wins(res) == 1,
      str([str(e) for _v, e in res if e][:2]))
check("t2 take: активный заказ один",
      db.q1("SELECT COUNT(*) c FROM orders WHERE user_id = ? AND status = 'active'",
            (UO,))["c"] == 1)

# --- сдача заказа ---
_a = db.q1("SELECT id, version FROM orders WHERE user_id = ? AND status = 'active'",
           (UO,))
db.exec("UPDATE orders SET progress = goal WHERE id = ?", (_a["id"],))
res = race(THREADS, lambda i: gl.claim_order(db.get_user(UO), _a["id"], _a["version"]))
check("t2 claim: награда выдана ровно один раз", wins(res) == 1,
      str([str(e) for _v, e in res if e][:2]))
check("t2 claim: в книге одна награда за этот заказ",
      db.q1("SELECT COUNT(*) c FROM economy_ledger WHERE user_id = ? "
            "AND reason = 'order_reward' AND ref_id = ?",
            (UO, str(_a["id"])))["c"] == 1)

# --- один и тот же токен из 16 потоков ---
UT = BASE + 14
db.create_user(UT, "racer_token", "Racer")


def _replay(i):
    return economy.replayable(f"op:{UT}:same", UT, "conc_test",
                              lambda: {"balance": gl.add_cookies(
                                  UT, 50, count_earned=False, reason="conc_token")})


res = race(THREADS, _replay)
paid = db.q1("SELECT COUNT(*) c FROM economy_ledger WHERE user_id = ? "
             "AND reason = 'conc_token'", (UT,))["c"]
conflicts = sum(1 for _v, e in res if isinstance(e, economy.ConflictError))
check("t2 token: начисление ровно одно", paid == 1, f"{paid} строк")
check("t2 token: баланс сдвинулся один раз", db.get_user(UT)["cookies"] == 50,
      str(db.get_user(UT)["cookies"]))
# проигравшие обязаны получить либо сохранённый ответ, либо честный 409 —
# но не второе начисление и не исключение из примитива
others = [e for _v, e in res if e is not None]
check("t2 token: проигравшие получили ответ или 409",
      all(isinstance(e, economy.ConflictError) for e in others),
      str([repr(e) for e in others[:3]]))
replies = [v for v, e in res if e is None]
check("t2 token: все успешные ответы одинаковы",
      len({json.dumps(v, sort_keys=True) for v in replies}) == 1,
      str(replies[:3]))
if conflicts:
    print(f"      (409 получили {conflicts} из {THREADS} — это штатный исход)")

# --- смешанная нагрузка ---
LOAD_UIDS = [BASE + 20 + i for i in range(8)]
for uid in LOAD_UIDS:
    db.create_user(uid, f"load{uid}", "Load")
    gl.add_cookies(uid, 1_000_000, count_earned=False, reason="conc_seed")
    db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, ?, 3) "
            "ON CONFLICT(user_id, building_key) DO UPDATE SET count = 3",
            (uid, KEY))

t0 = time.time()
run_load(LOAD_UIDS, OPS, THREADS)
dt = time.time() - t0
print(f"      {OPS} операций в {THREADS} потоков за {dt:.1f}s "
      f"({OPS / max(dt, 0.001):.0f} оп/сек)")
check("t2 load: нагрузка прошла без исключений", True)

assert_converged("t2", [UD, US, UC, UO, UT] + LOAD_UIDS)

# =====================================================================
print(f"\n=== T3: {PROCS} процессов на один файл базы ===")
# =====================================================================

# Отдельный набор игроков: у дочерних процессов свой кэш дохода и свой
# _income_memo, и именно на стыке процессов ломается всё, что «работало,
# потому что переменная одна на всех»
P_UIDS = [BASE + 40 + i for i in range(4)]
for uid in P_UIDS:
    db.create_user(uid, f"proc{uid}", "Proc")
    gl.add_cookies(uid, 1_000_000, count_earned=False, reason="conc_seed")

PROC_OPS = max(200, OPS // 4)
env = dict(os.environ, DATABASE_PATH=DB_PATH, PYTHONIOENCODING="utf-8")
args = [sys.executable, os.path.abspath(__file__), "--worker",
        ",".join(str(u) for u in P_UIDS), str(PROC_OPS // PROCS)]
t0 = time.time()
procs = [subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT) for _ in range(PROCS)]
outs = []
for p in procs:
    out, _ = p.communicate(timeout=600)
    outs.append((p.returncode, (out or b"").decode("utf-8", "replace")))
dt = time.time() - t0
bad = [o for code, o in outs if code != 0]
print(f"      {PROC_OPS} операций в {PROCS} процессов за {dt:.1f}s")
check("t3: все процессы завершились без ошибок", not bad, bad[0][-400:] if bad else "")

assert_converged("t3", P_UIDS)

# книга под многопроцессной записью обязана быть непрерывной: пропущенная
# строка — это и есть потерянный апдейт, который сверка поймала бы только
# случайно, если бы он совпал по сумме
minted = db.q1("SELECT COUNT(*) c FROM economy_ledger WHERE reason = 'conc_mint' "
               f"AND user_id IN ({','.join('?' * len(P_UIDS))})", P_UIDS)["c"]
check("t3: начисления действительно дошли до книги", minted > 0, str(minted))

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
