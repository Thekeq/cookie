"""Смоук-тесты механик удержания: золотая печенька, комбо, престиж, энергия."""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")

from fastapi.testclient import TestClient

from main import app
from server.game_logic import db
import server.game_logic as gl
import server.game_config as cfg

BOT_TOKEN = os.environ["BOT_TOKEN"]


def sign(user_id, username="tester", first_name="Test"):
    data = {"user": json.dumps({"id": user_id, "username": username, "first_name": first_name}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


c = TestClient(app)
UID = 910_000_000 + int(time.time()) % 10_000_000
H = {"Authorization": "tma " + sign(UID)}

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


r = c.post("/api/auth", headers=H)
check("auth", r.status_code == 200, r.text[:200])
s = r.json()
check("state has golden/combo/prestige",
      "golden" in s and "combo" in s and "prestige" in s)
check("energy base 1000", s["user"]["max_energy"] >= 1000, str(s["user"]["max_energy"]))
check("golden scheduled not active", s["golden"]["active"] is False)

# --- золотая печенька: форсим появление ---
db.update_user(UID, golden_next_at=time.time() - 1, golden_expires_at=0)
r = c.get("/api/state", headers=H)
g = r.json()["golden"]
check("golden appears when due", g["active"] is True and g["effect"] in ("frenzy", "chain"),
      str(g))
# тапаем
before = db.get_user(UID)["cookies"]
r = c.post("/api/golden/claim", headers=H)
check("golden claim ok", r.status_code == 200, r.text[:200])
eff = r.json()["effect"]
if eff == "frenzy":
    check("frenzy boost active", "golden_frenzy" in gl.active_boosts(UID))
else:
    check("chain paid cookies", db.get_user(UID)["cookies"] > before)
r = c.post("/api/golden/claim", headers=H)
check("golden double-claim blocked", r.status_code == 400)

# --- комбо ---
db.update_user(UID, energy=2000, combo_mult=1, combo_last_at=0)
r = c.post("/api/click", json={"clicks": 10}, headers=H)
check("first batch combo=1", r.json().get("combo") == 1.0, str(r.json().get("combo")))
time.sleep(1.2)  # 10 кликов за 1.2с = ~8 cps > COMBO_MIN_CPS, окно не истекло
r = c.post("/api/click", json={"clicks": 10}, headers=H)
check("combo grows", r.json().get("combo", 0) > 1.0, str(r.json().get("combo")))
# пауза дольше окна — сброс
db.update_user(UID, combo_last_at=time.time() - cfg.COMBO_WINDOW - 2)
r = c.post("/api/click", json={"clicks": 10}, headers=H)
check("combo resets after pause", r.json().get("combo") == 1.0, str(r.json().get("combo")))

# --- престиж ---
r = c.get("/api/prestige", headers=H)
check("prestige locked early", r.json()["can_prestige"] is False)
r = c.post("/api/prestige", headers=H)
check("prestige blocked early", r.status_code == 400)
# нафармили 25M — престиж доступен
# (снимаем бусты: если golden выпал frenzy, его x7 исказил бы проверку множителя)
db.exec("DELETE FROM boosts WHERE user_id = ?", (UID,))
db.update_user(UID, total_earned=25_000_000, cookies=123, level=10, click_level=8)
db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, 'granny', 5)", (UID,))
db.exec("INSERT INTO skins (user_id, skin_key) VALUES (?, 'donut')", (UID,))
r = c.get("/api/prestige", headers=H)
pts = r.json()["gain_available"]
check("prestige available", r.json()["can_prestige"] is True and pts == 5, str(pts))
r = c.post("/api/prestige", headers=H)
check("prestige done", r.status_code == 200, r.text[:200])
s = r.json()
u = db.get_user(UID)
check("progress reset", u["cookies"] == 0 and u["level"] == 1 and u["click_level"] == 1)
check("farm wiped", gl.farm_counts(UID) == {})
check("skins kept", db.q1("SELECT id FROM skins WHERE user_id = ? AND skin_key = 'donut'",
                          (UID,)) is not None)
check("points saved", u["prestige_points"] == 5 and u["prestige_count"] == 1)
check("multiplier applied", abs(s["user"]["click_power"] - 1 * 1.10) < 0.001,
      str(s["user"]["click_power"]))
check("total_earned kept", u["total_earned"] == 25_000_000)
r = c.post("/api/prestige", headers=H)
check("re-prestige blocked (no new points)", r.status_code == 400)

# --- прямая покупка печенек высокого уровня ---
db.update_user(UID, level=11, cookies=10_000_000)
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
r = c.get("/api/state", headers=H)
sd = r.json()["spawn_direct"]
# на 11 уровне игрока открыт item 10 => напрямую можно до 10-3=7
check("direct max = unlocked-3", sd["max_level"] == 7, str(sd["max_level"]))
check("direct pricing has premium",
      sd["costs"]["3"] > sd["costs"]["1"] * 4, str(sd["costs"]["3"]))
r = c.post("/api/merge/spawn", json={"level": 5}, headers=H)
check("direct spawn lvl5", r.status_code == 200
      and any(b["item_level"] == 5 for b in r.json()["board"]), r.text[:200])
r = c.post("/api/merge/spawn", json={"level": 8}, headers=H)
check("direct spawn above cap blocked", r.status_code == 400)
r = c.post("/api/merge/spawn", json={"level": 1}, headers=H)
check("plain spawn still works", r.status_code == 200, r.text[:200])
# слияние выше 12 работает (потолок теперь 24)
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.update_user(UID, level=30)
db.exec("INSERT INTO board (user_id, cell, item_level) VALUES (?, 0, 12)", (UID,))
db.exec("INSERT INTO board (user_id, cell, item_level) VALUES (?, 1, 12)", (UID,))
r = c.post("/api/merge/move", json={"from_cell": 0, "to_cell": 1}, headers=H)
check("merge to lvl13 works", r.status_code == 200
      and r.json().get("merged_level") == 13, r.text[:200])

# --- перебаланс: прогрессивный БП, кап XP, динамический магазин, престиж-порог ---
# прогрессивная цена уровня БП
check("bp lvl1 costs 400", cfg.bp_xp_for_level(1) == 400)
check("bp lvl30 costs 12000", cfg.bp_xp_for_level(30) == 12000)
check("bp cumulative consistent",
      cfg.bp_total_xp(30) == sum(cfg.bp_xp_for_level(l) for l in range(1, 31)))
check("bp_level_for_xp", cfg.bp_level_for_xp(cfg.bp_total_xp(5)) == 5
      and cfg.bp_level_for_xp(cfg.bp_total_xp(5) - 1) == 4)

# дневной кап XP кликов: после 10k кликов XP режется вчетверо
db.update_user(UID, energy=200000, clicks_day=gl._utc_day(time.time()),
               clicks_day_count=cfg.CLICK_XP_SOFT_CAP, combo_last_at=0)
xp_before = db.get_user(UID)["xp"]
db.exec("DELETE FROM daily_quests WHERE user_id = ?", (UID,))
import server.routers.game as game_router
game_router._click_windows.pop(UID, None)
r = c.post("/api/click", json={"clicks": 40}, headers=H)
accepted = r.json()["accepted"]
xp_gained = db.get_user(UID)["xp"] - xp_before
check("click xp capped to 0.125",
      abs(xp_gained - accepted * cfg.CLICK_XP_RATE_CAPPED) < 0.01,
      f"{xp_gained} за {accepted}")

# магазин: пачки показывают персональную сумму (минимум при нулевом доходе)
r = c.get("/api/shop", headers=H)
pack = next(i for i in r.json()["items"] if i["key"] == "cookies_pack")
check("shop pack has amount", pack.get("amount", 0) >= 5000, str(pack.get("amount")))

# растущий порог престижа: после 1-го нужен уже 150M
u = db.get_user(UID)
check("prestige threshold grows",
      cfg.prestige_threshold(u["prestige_count"]) == 10_000_000 * 15 ** u["prestige_count"])
r = c.get("/api/prestige", headers=H)
check("2nd prestige needs 150M", r.json()["min_earned"] == 150_000_000
      and r.json()["can_prestige"] is False, str(r.json()["min_earned"]))

# сезонные призы масштабируются от заработка
check("season reward scales", cfg.season_reward(1, 10_000_000) == 3_000_000)
check("season reward has floor", cfg.season_reward(1, 1000) == 100_000)

# --- level-up рефилл энергии ---
db.update_user(UID, level=1, xp=cfg.xp_for_level(2) + 1, energy=3,
               energy_updated_at=time.time())
r = c.post("/api/levels/claim", headers=H)
check("level claim", r.status_code == 200, r.text[:200])
check("energy refilled on level-up",
      db.get_user(UID)["energy"] >= cfg.max_energy(2) - 5,
      str(db.get_user(UID)["energy"]))

# cleanup
for t in ("users", "board", "farm", "upgrades", "skins", "daily_quests",
          "ref_claims", "achievements", "boosts", "purchases"):
    db.exec(f"DELETE FROM {t} WHERE user_id = ?", (UID,))
db.exec("DELETE FROM season_results WHERE user_id = ?", (UID,))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
