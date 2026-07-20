"""Смоук-тесты экономики доски: клетки-дефицит, мусорка, оффлайн-кап за Stars."""
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается
import tempfile
os.environ["DATABASE_PATH"] = os.path.join(
    tempfile.gettempdir(), f"cookie_test_be_{os.getpid()}.db")

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
UID = 920_000_000 + int(time.time()) % 10_000_000
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

# --- клетки: база 12 на 1 lvl без друзей ---
bc = s["board_cells"]
check("base cells = 12", bc["unlocked"] == cfg.MERGE_BASE_CELLS, str(bc))
check("total 25", bc["total"] == cfg.BOARD_SIZE)
check("next unlock at lvl 3", bc["next_unlock_level"] == 3, str(bc["next_unlock_level"]))
check("ref cells listed", [x["friends"] for x in bc["ref_cells"]] == [1, 3, 5, 10])

# --- доска «полная» при 12 занятых открытых клетках ---
db.update_user(UID, cookies=10_000_000)
for cell in range(cfg.MERGE_BASE_CELLS):
    db.exec("INSERT OR IGNORE INTO board (user_id, cell, item_level) VALUES (?, ?, 1)",
            (UID, cell))
r = c.post("/api/merge/spawn", json={"level": 1}, headers=H)
check("spawn blocked when open cells busy", r.status_code == 400
      and "err_board_full" in r.text, r.text[:200])

# --- ход в пустую ЗАКРЫТУЮ клетку запрещён, выход из закрытой — можно ---
r = c.post("/api/merge/move", json={"from_cell": 0, "to_cell": 24}, headers=H)
check("move into locked cell blocked", r.status_code == 400
      and "err_cell_locked" in r.text, r.text[:200])
db.exec("DELETE FROM board WHERE user_id = ? AND cell = 5", (UID,))  # освободили открытую
db.exec("INSERT INTO board (user_id, cell, item_level) VALUES (?, 24, 3)", (UID,))
r = c.post("/api/merge/move", json={"from_cell": 24, "to_cell": 5}, headers=H)
check("move out of locked cell ok", r.status_code == 200, r.text[:200])

# --- анлоки: уровни и друзья добавляют клетки ---
check("lvl5 + 1 friend = 15 cells",
      cfg.merge_cells_unlocked(5, 1) == cfg.MERGE_BASE_CELLS + 2 + 1)
check("maxed = 25", cfg.merge_cells_unlocked(30, 10) == cfg.BOARD_SIZE)

# --- мусорка: клетка освобождается, кэшбек падает ---
before = db.get_user(UID)["cookies"]
r = c.post("/api/merge/trash", json={"cell": 5}, headers=H)
check("trash ok", r.status_code == 200, r.text[:200])
s = r.json()
check("cell emptied", not any(b["cell"] == 5 for b in s["board"]))
check("refund paid", s["trash_refund"] > 0 and db.get_user(UID)["cookies"] > before - 1)
r = c.post("/api/merge/trash", json={"cell": 5}, headers=H)
check("trash empty cell blocked", r.status_code == 400)

# --- цена спавна масштабируется от дохода ---
check("spawn cost scales with income",
      cfg.spawn_cost(0, 100_000) > cfg.spawn_cost(0, 0) * 10)
check("spawn cost floor 50", cfg.spawn_cost(0, 0) == 50)

# --- буст пассивки мерджа ---
check("passive lvl3 = 90/h", cfg.passive_income_per_hour(3) == 90)

# --- оффлайн-кап за Stars ---
user = db.get_user(UID)
check("base farm cap 3h", gl.farm_offline_cap_hours(user) == 3)
gl._apply_purchase_effect(UID, "offline_cap_6h")
user = db.get_user(UID)
check("cap 6h after purchase", gl.farm_offline_cap_hours(user) == 6
      and gl.passive_offline_cap_hours(user) == 6)
gl._apply_purchase_effect(UID, "offline_cap_12h")
user = db.get_user(UID)
check("cap 12h after upgrade", gl.farm_offline_cap_hours(user) == 12)
gl._apply_purchase_effect(UID, "offline_cap_6h")
user = db.get_user(UID)
check("smaller tier does not downgrade", gl.farm_offline_cap_hours(user) == 12)

# кап реально применяется: ферма простояла 24ч — начислит максимум 12ч
db.exec("INSERT OR IGNORE INTO farm (user_id, building_key, count) VALUES (?, 'granny', 1)",
        (UID,))
db.update_user(UID, farm_collected_at=time.time() - 24 * 3600)
income = gl.collect_farm(db.get_user(UID))
expected = gl.farm_cps(UID) * 12 * 3600
check("offline income capped at 12h", abs(income - expected) < expected * 0.01,
      f"{income} vs {expected}")

# --- магазин: owned-флаг и запрет повторного invoice ---
r = c.get("/api/shop", headers=H)
items = {i["key"]: i for i in r.json()["items"]}
check("shop has offline items", "offline_cap_6h" in items and "offline_cap_12h" in items)
check("offline items owned", items["offline_cap_6h"]["owned"]
      and items["offline_cap_12h"]["owned"])
r = c.post("/api/shop/invoice", json={"item_key": "offline_cap_12h"}, headers=H)
check("re-buy owned cap blocked", r.status_code == 400 and "err_owned" in r.text,
      r.text[:200])

# --- бейдж пекарни: активный выполненный заказ ---
r = c.get("/api/state", headers=H)
check("orders_claimable false", r.json()["orders_claimable"] is False)
r = c.get("/api/orders", headers=H)
c.post("/api/orders/take", json={"slot": 1}, headers=H)
db.exec("UPDATE orders SET progress = goal WHERE user_id = ? AND status = 'active'", (UID,))
r = c.get("/api/state", headers=H)
check("orders_claimable true when done", r.json()["orders_claimable"] is True)

print(f"\n{ok} passed, {fail} failed")
if fail:
    raise SystemExit(1)
