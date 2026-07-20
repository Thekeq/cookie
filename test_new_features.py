"""Смоук-тесты новых фич: дейлик, задания, сезоны, реф-майлстоуны, канал, рассылка."""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается
import tempfile
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.gettempdir(), f"cookie_test_{os.getpid()}.db")

from fastapi.testclient import TestClient

from main import app
from server.game_logic import db
import server.game_logic as gl
import server.game_config as cfg

BOT_TOKEN = os.environ["BOT_TOKEN"]


def sign(user_id, username="tester", first_name="Test", start_param=""):
    data = {"user": json.dumps({"id": user_id, "username": username, "first_name": first_name}),
            "auth_date": str(int(time.time()))}
    if start_param:
        data["start_param"] = start_param
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


c = TestClient(app)
UID = 900_000_000 + int(time.time()) % 10_000_000
UID2 = UID + 1


def H(uid, **kw):
    return {"Authorization": "tma " + sign(uid, **kw)}


ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


# --- auth ---
r = c.post("/api/auth", headers=H(UID))
check("auth 200", r.status_code == 200, r.text[:200])
s = r.json()
check("state has season", "season" in s and s["season"]["ends_at"] > time.time())
check("state has daily", s["daily"]["can_claim"] is True and s["daily"]["next_streak"] == 1)
check("quests_claimable=0", s["quests_claimable"] == 0)

# --- daily claim ---
r = c.post("/api/daily/claim", headers=H(UID))
check("daily claim", r.status_code == 200 and r.json()["streak"] == 1
      and r.json()["reward"] == 500, r.text[:200])
r = c.post("/api/daily/claim", headers=H(UID))
check("daily double-claim blocked", r.status_code == 400)
db.update_user(UID, daily_claimed_at=time.time() - 86400)
r = c.post("/api/daily/claim", headers=H(UID))
check("daily streak grows", r.status_code == 200 and r.json()["streak"] == 2, r.text[:200])
db.update_user(UID, daily_claimed_at=time.time() - 3 * 86400)
r = c.post("/api/daily/claim", headers=H(UID))
check("daily streak resets after miss", r.status_code == 200 and r.json()["streak"] == 1,
      r.text[:200])

# --- атомарность: сбой внутри tx откатывает всё ---
_before = db.get_user(UID)["cookies"]
try:
    with db.tx():
        db.update_user(UID, cookies=_before + 12345)
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("tx rollback works", db.get_user(UID)["cookies"] == _before)

# daily: если начисление награды упало, клейм не фиксируется (нет claimed без денег)
db.update_user(UID, daily_claimed_at=0, daily_streak=0)
_orig_add = gl.add_cookies
gl.add_cookies = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fail"))
try:
    gl.claim_daily(db.get_user(UID))
except RuntimeError:
    pass
gl.add_cookies = _orig_add
check("daily atomic: no claim on failed reward",
      db.get_user(UID)["daily_claimed_at"] == 0)
r = c.post("/api/daily/claim", headers=H(UID))
check("daily claim works after failed attempt", r.status_code == 200, r.text[:120])
db.update_user(UID, daily_claimed_at=time.time())  # вернуть состояние для следующих тестов

# --- quests ---
r = c.get("/api/quests", headers=H(UID))
qs = r.json()["quests"]
check("3 quests today", len(qs) == cfg.DAILY_QUESTS_PER_DAY, str(len(qs)))
db.update_user(UID, cookies=100000, energy=500)
r = c.post("/api/click", json={"clicks": 10}, headers=H(UID))
check("click ok", r.status_code == 200 and r.json()["accepted"] > 0, r.text[:200])
r = c.get("/api/quests", headers=H(UID))
qs2 = {q["key"]: q for q in r.json()["quests"]}
click_q = [q for q in qs2.values() if q["metric"] == "clicks"]
if click_q:
    check("click quest progressed", click_q[0]["progress"] > 0)
earn_q = [q for q in qs2.values() if q["metric"] == "earned"]
if earn_q:
    check("earned quest progressed", earn_q[0]["progress"] > 0)
r = c.post("/api/merge/spawn", headers=H(UID))
check("spawn ok", r.status_code == 200, r.text[:200])
k0 = qs[0]["key"]
db.exec("UPDATE daily_quests SET progress = 999999 WHERE user_id = ? AND quest_key = ?",
        (UID, k0))
before_bp = db.get_user(UID)["bp_xp"]
r = c.post("/api/quests/claim", json={"key": k0}, headers=H(UID))
check("quest claim", r.status_code == 200, r.text[:200])
check("bp_xp granted", db.get_user(UID)["bp_xp"] > before_bp)
r = c.post("/api/quests/claim", json={"key": k0}, headers=H(UID))
check("quest double-claim blocked", r.status_code == 400)

# --- season fields ---
u = db.get_user(UID)
check("season_earned counted", u["season_earned"] > 0, str(u["season_earned"]))
r = c.get("/api/leaderboard", headers=H(UID))
lb = r.json()
check("lb has season fields", "season_ends_at" in lb and "season" in lb)
check("lb top has prizes", all("prize" in row for row in lb["top"]))
check("lb top has me", any(row["is_me"] for row in lb["top"]))
# сортировка: уровень главнее, при равном уровне — season_earned
ranks = [(row["level"], row["season_earned"]) for row in lb["top"]]
check("lb sorted by level then earned",
      ranks == sorted(ranks, key=lambda x: (-x[0], -x[1])), str(ranks[:5]))

# --- season rollover ---
db.update_user(UID, season_id=gl.current_season() - 1, season_earned=55555,
               bp_xp=9999, bp_premium=1)
gl.finalize_seasons()
u = db.get_user(UID)
check("rollover reset", u["season_earned"] == 0 and u["bp_xp"] == 0
      and u["bp_premium"] == 0 and u["season_id"] == gl.current_season())
res = gl.my_last_season_result(UID)
check("season result saved", res is not None and res["rank"] >= 1, str(res))
# идемпотентность ролловера: частичный снапшот прошлого сбоя не роняет повтор
db.update_user(UID, season_id=gl.current_season() - 1, season_earned=777)
db.exec("INSERT OR IGNORE INTO season_results (season_id, user_id, rank, earned, "
        "reward_cookies, created_at) VALUES (?, ?, 1, 777, 0, ?)",
        (gl.current_season() - 1, UID, time.time()))
gl.finalize_seasons()  # не должен упасть на IntegrityError
check("rollover survives partial snapshot",
      db.get_user(UID)["season_id"] == gl.current_season())
if res and res["rank"] <= 10:
    check("season reward paid", db.get_user(UID)["cookies"] > 0)

# --- ref milestones ---
r = c.post("/api/auth", headers=H(UID2, username="friend", start_param=f"ref_{UID}"))
check("referral registered", r.status_code == 200)
r = c.get("/api/referrals", headers=H(UID))
ms = {m["key"]: m for m in r.json()["milestones"]}
check("milestones present", len(ms) == 3, str(len(ms)))
check("progress 1/3", ms["refs_boost"]["progress"] == 1)
r = c.post("/api/referrals/milestone", json={"key": "refs_boost"}, headers=H(UID))
check("milestone not-done blocked", r.status_code == 400)
for i in range(2, 4):
    db.exec("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) "
            "VALUES (?, ?, ?)", (UID, UID + i * 100, time.time()))
r = c.post("/api/referrals/milestone", json={"key": "refs_boost"}, headers=H(UID))
check("milestone boost claimed", r.status_code == 200, r.text[:200])
check("boost active", "click_x2" in gl.active_boosts(UID))
r = c.post("/api/referrals/milestone", json={"key": "refs_boost"}, headers=H(UID))
check("milestone double-claim blocked", r.status_code == 400)
for i in range(4, 11):
    db.exec("INSERT OR IGNORE INTO referrals (referrer_id, referred_id, created_at) "
            "VALUES (?, ?, ?)", (UID, UID + i * 100, time.time()))
r = c.post("/api/referrals/milestone", json={"key": "refs_skin"}, headers=H(UID))
check("milestone skin claimed", r.status_code == 200, r.text[:200])
r = c.get("/api/farm", headers=H(UID))
skins = {sk["key"]: sk for sk in r.json()["skins"]}
check("royal skin owned+visible", "royal" in skins and skins["royal"]["owned"])
r = c.post("/api/farm/set_skin", json={"key": "royal"}, headers=H(UID))
check("royal skin equippable", r.status_code == 200, r.text[:200])
r = c.get("/api/state", headers=H(UID))
check("royal emoji in state", r.json()["user"]["skin_emoji"] == "\U0001F451",
      r.json()["user"]["skin_emoji"])

# --- channel ---
r = c.get("/api/channel", headers=H(UID))
check("channel get", r.status_code == 200 and r.json()["channel"] == "")
r = c.post("/api/channel/claim", headers=H(UID))
check("channel claim blocked when unset", r.status_code == 400)

# --- broadcast admin-only ---
r = c.post("/api/admin/broadcast", json={"text": "hi", "test": True}, headers=H(UID))
check("broadcast 403 for non-admin", r.status_code == 403)

# --- notifier picker (unit) ---
from bot.notifier import _pick_notification

now = time.time()
u = dict(db.get_user(UID))
u.update(daily_streak=0, daily_claimed_at=0, farm_collected_at=0,
         energy=0, energy_updated_at=now)
check("no push when nothing", _pick_notification(u, now) is None)
u2 = dict(u)
u2.update(energy=999999, energy_updated_at=now)
check("push on full energy", _pick_notification(u2, now) is not None)

# --- локализация сервера ---
from server.i18n import T as SRV_T, tr

missing = [k for k, v in SRV_T.items() if set(v) != {"en", "uk", "ru"}]
check("server i18n: all keys have en/uk/ru", not missing, str(missing[:5]))
for key in cfg.SHOP_ITEMS:
    check(f"shop {key} localized",
          tr("uk", f"shop_{key}_t") != f"shop_{key}_t")
for key in cfg.ACHIEVEMENTS:
    check(f"ach {key} localized",
          tr("uk", f"ach_{key}_t") != f"ach_{key}_t"
          and tr("uk", f"ach_{key}_d") != f"ach_{key}_d")
# ачивки в API переводятся по X-Lang
r = c.get("/api/achievements", headers={**H(UID), "X-Lang": "uk"})
first = r.json()["achievements"][0]
check("achievements API uk", first["title"] == tr("uk", f"ach_{first['key']}_t"),
      first["title"])
# магазин переводится
r = c.get("/api/shop", headers={**H(UID), "X-Lang": "uk"})
item = r.json()["items"][0]
check("shop API uk", item["title"] == tr("uk", f"shop_{item['key']}_t"), item["title"])
# язык синкается в профиль на /auth
c.post("/api/auth", headers={**H(UID), "X-Lang": "uk"})
check("lang synced to profile", db.get_user(UID)["lang"] == "uk")
# ошибки приходят кодами err_*
r = c.post("/api/promo/redeem", json={"code": "NOPE123"}, headers=H(UID))
check("errors are err_ codes", r.json()["detail"].startswith("err_"), r.text[:100])

# --- покупка при «натикавшем» доходе фермы (баг «деньги есть, купить не даёт») ---
db.update_user(UID, cookies=50, click_level=1,
               farm_collected_at=time.time() - 60, passive_collected_at=time.time())
db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, 'cursor', 10) "
        "ON CONFLICT(user_id, building_key) DO UPDATE SET count = 10", (UID,))
# на счету 50, апгрейд стоит 100, но за 60с ферма натикала 10*0.5*60 = 300
r = c.post("/api/click/upgrade", headers=H(UID))
check("buy with pending farm income", r.status_code == 200, r.text[:150])
check("balance collected before charge", db.get_user(UID)["cookies"] > 0)

# --- Stars: зависшая 'paid' покупка довыдаётся на /auth, идемпотентно ---
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'boost_x2_1h', 50, 'test-charge-1', 'paid', ?)",
        (UID, time.time()))
c.post("/api/auth", headers=H(UID))
row = db.q1("SELECT status FROM purchases WHERE tg_payment_id = 'test-charge-1'")
check("stuck paid fulfilled on auth", row and row["status"] == "fulfilled", str(row))
n_boosts = db.q1("SELECT COUNT(*) c FROM boosts WHERE user_id = ? "
                 "AND boost_key = 'click_x2'", (UID,))["c"]
check("boost granted", n_boosts >= 1)
check("re-fulfill returns False", gl.fulfill_charge("test-charge-1") is False)
n2 = db.q1("SELECT COUNT(*) c FROM boosts WHERE user_id = ? "
           "AND boost_key = 'click_x2'", (UID,))["c"]
check("no double fulfill", n2 == n_boosts)

# --- сбор дохода атомарен: сбой начисления не двигает таймер (доход не теряется) ---
db.update_user(UID, farm_collected_at=time.time() - 120)
ts_before = db.get_user(UID)["farm_collected_at"]
_orig_add2 = gl.add_cookies
gl.add_cookies = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("fail"))
try:
    gl.collect_farm(db.get_user(UID))
except RuntimeError:
    pass
gl.add_cookies = _orig_add2
check("collect atomic: timer not advanced on failure",
      db.get_user(UID)["farm_collected_at"] == ts_before)
r = c.get("/api/state", headers=H(UID))
check("income recovered after failed collect", r.status_code == 200)

# --- канал: условный UPDATE отдаёт награду ровно одному запросу ---
db.update_user(UID, channel_claimed=0)
db.exec("UPDATE users SET channel_claimed = 1 WHERE user_id = ? AND channel_claimed = 0", (UID,))
first = db.cursor.rowcount
db.exec("UPDATE users SET channel_claimed = 1 WHERE user_id = ? AND channel_claimed = 0", (UID,))
second = db.cursor.rowcount
check("channel conditional claim once", first == 1 and second == 0, f"{first},{second}")

# --- cleanup ---
for t in ("users", "board", "farm", "upgrades", "skins", "daily_quests",
          "ref_claims", "achievements", "boosts", "purchases"):
    db.exec(f"DELETE FROM {t} WHERE user_id IN (?, ?)", (UID, UID2))
db.exec("DELETE FROM referrals WHERE referrer_id = ? OR referred_id IN (?, ?)",
        (UID, UID, UID2))
db.exec("DELETE FROM season_results WHERE user_id IN (?, ?)", (UID, UID2))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
