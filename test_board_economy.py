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
# lvl1 обязан оставаться в разумных минутах дохода при любой заполненности:
# именно перемножение трёх экспонент раньше давало цену в 1e17 (фидбек)
for _items in (0, 12, 24):
    _mins = cfg.spawn_cost(_items, 1e9) / 1e9 * 60
    check(f"lvl1 cost sane at {_items} items ({_mins:.0f} min)", _mins <= 30, f"{_mins:.1f}")
check("direct lvl5 under 5h of income",
      cfg.direct_spawn_cost(5, 12, 1e9) / 1e9 <= 5,
      f"{cfg.direct_spawn_cost(5, 12, 1e9) / 1e9:.1f}h")
# премия над честным merge-путём (2^(N-1)) остаётся умеренной
check("direct premium over merge path ~3x at lvl10",
      2.5 <= (cfg.SPAWN_LEVEL_FACTOR / 2) ** 9 <= 4.5,
      str((cfg.SPAWN_LEVEL_FACTOR / 2) ** 9))

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

# --- заказы: награда считается от ТЕКУЩЕГО дохода, а не из хранимой строки ---
# подделываем строку под «выписана до престижа»: 60M за лёгкий заказ
db.exec("UPDATE orders SET reward_cookies = 60000000, reward_bp_xp = 9999 "
        "WHERE user_id = ? AND status = 'active'", (UID,))
st = gl.orders_state(db.get_user(UID))
fresh_reward = gl.order_reward(
    db.q1("SELECT template FROM orders WHERE user_id = ? AND status = 'active'",
          (UID,))["template"], gl.hourly_income(UID))[0]
check("stale reward not shown", st["active"]["reward_cookies"] < 60_000_000
      and abs(st["active"]["reward_cookies"] - fresh_reward) < 1, str(st["active"]))
before = db.get_user(UID)["cookies"]
r = c.post("/api/orders/claim", headers=H)
paid = db.get_user(UID)["cookies"] - before
check("stale reward not paid", r.status_code == 200 and paid < 60_000_000
      and abs(paid - fresh_reward) < 1, f"paid={paid}")

# --- офферы пересчитываются под выросший доход, без «добивания за копейки» ---
r = c.get("/api/orders", headers=H)
cheap = [o["reward_cookies"] for o in r.json()["offers"]]
db.exec("UPDATE farm SET count = 500 WHERE user_id = ? AND building_key = 'granny'", (UID,))
r = c.get("/api/orders", headers=H)
rich = [o["reward_cookies"] for o in r.json()["offers"]]
check("offers rescale when income grows", all(b > a for a, b in zip(cheap, rich)),
      f"{cheap} -> {rich}")
check("offer goals rescale too",
      db.q1("SELECT MAX(goal) g FROM orders WHERE user_id = ? AND status = 'offer'",
            (UID,))["g"] > 0)

# --- престиж чистит незавершённые заказы (иначе цель недостижима на 1 lvl) ---
c.post("/api/orders/take", json={"slot": 1}, headers=H)
db.update_user(UID, total_earned=cfg.PRESTIGE_MIN_EARNED * 2)
r = c.post("/api/prestige", headers=H)
check("prestige done", r.status_code == 200, r.text[:200])
check("orders wiped on prestige", not db.q1(
    "SELECT id FROM orders WHERE user_id = ? AND status != 'done'", (UID,)))
r = c.get("/api/orders", headers=H)
check("fresh offers after prestige are small",
      all(o["reward_cookies"] <= max(cfg.ORDER_REWARD_MIN.values()) * 1.5
          for o in r.json()["offers"]),
      str([o["reward_cookies"] for o in r.json()["offers"]]))

# --- золотая печенька: в ответе бонус, а не весь баланс ---
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 0, 10, 1000)", (UID,))
db.update_user(UID, cookies=5_000_000, golden_next_at=time.time() - 1,
               golden_expires_at=0, golden_effect="chain", level=5)
c.get("/api/state", headers=H)          # активирует печеньку
# эффект при активации выбирается случайно — фиксируем chain (он даёт печеньки)
db.update_user(UID, golden_effect="chain")
before = db.get_user(UID)["cookies"]
r = c.post("/api/golden/claim", headers=H)
g = r.json()
paid = db.get_user(UID)["cookies"] - before
check("golden returns bonus, not balance",
      g.get("bonus") is not None and abs(g["bonus"] - paid) < 1,
      f"bonus={g.get('bonus')} paid={paid}")
check("golden balance separate", g["cookies"] > g["bonus"], str(g))

# --- доход для цен и наград не раздувается временными бустами ---
db.exec("DELETE FROM boosts WHERE user_id = ?", (UID,))
db.update_user(UID, click_level=20)
calm = gl.hourly_income(UID)
db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, 'golden_frenzy', ?)",
        (UID, time.time() + 60))
check("frenzy does not inflate income", abs(gl.hourly_income(UID) - calm) < 1,
      f"{calm} -> {gl.hourly_income(UID)}")
check("frenzy still boosts clicks", gl.click_multiplier(UID) > gl.permanent_click_multiplier(UID))
db.exec("DELETE FROM boosts WHERE user_id = ?", (UID,))

# --- переплавка возвращает долю вложенного, а не текущей цены ---
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 0, 5, 10000)", (UID,))
before = db.get_user(UID)["cookies"]
r = c.post("/api/merge/trash", json={"cell": 0}, headers=H)
check("refund is 10% of paid", abs(r.json()["trash_refund"] - 1000) < 1, r.text[:200])
# рост дохода не должен увеличивать возврат за уже купленное
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 0, 5, 10000)", (UID,))
db.exec("UPDATE farm SET count = 5000 WHERE user_id = ? AND building_key = 'granny'", (UID,))
r = c.post("/api/merge/trash", json={"cell": 0}, headers=H)
check("refund ignores income growth", abs(r.json()["trash_refund"] - 1000) < 1, r.text[:200])

# --- мердж складывает вложенное ---
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.update_user(UID, level=10)
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 0, 4, 700)", (UID,))
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 1, 4, 300)", (UID,))
c.post("/api/merge/move", json={"from_cell": 0, "to_cell": 1}, headers=H)
check("merge sums paid",
      abs(db.q1("SELECT paid FROM board WHERE user_id = ? AND cell = 1", (UID,))["paid"]
          - 1000) < 1)

# --- BP XP за мердж ограничен капом ---
check("merge bp xp capped", cfg.merge_reward_bp_xp(24) == cfg.MERGE_BP_XP_CAP)
check("top merge is a small slice of the pass",
      cfg.merge_reward_bp_xp(24) / cfg.bp_total_xp(cfg.BP_MAX_LEVEL) < 0.05,
      str(cfg.merge_reward_bp_xp(24) / cfg.bp_total_xp(cfg.BP_MAX_LEVEL)))
check("low merges keep full bp xp", cfg.merge_reward_bp_xp(5) == cfg.merge_reward_xp(5))

# --- полный бак за Stars учитывает апгрейды энергии ---
db.exec("INSERT OR IGNORE INTO upgrades (user_id, upgrade_key) VALUES (?, 'energy_cap_500')",
        (UID,))
db.update_user(UID, energy=0)
gl._apply_purchase_effect(UID, "energy_full")
cap = gl.energy_cap(db.get_user(UID))
check("energy_full respects upgrades", db.get_user(UID)["energy"] == cap and cap >= 500,
      f"energy={db.get_user(UID)['energy']} cap={cap}")

# --- мёртвый заказ снимается сам и бесплатно ---
db.exec("DELETE FROM orders WHERE user_id = ?", (UID,))
db.update_user(UID, level=3, orders_day=None, orders_day_count=0)
db.exec("INSERT INTO orders (user_id, slot, template, metric, goal, progress, "
        "reward_cookies, reward_bp_xp, status, created_at) "
        "VALUES (?, 1, 'special', 'make_item', 23, 8, 100, 10, 'active', ?)",
        (UID, time.time()))
st = gl.orders_state(db.get_user(UID))
check("unreachable make_item order dropped", st["active"] is None, str(st["active"]))
check("fresh offers instead", len(st["offers"]) == 3)
check("daily limit untouched", db.get_user(UID)["orders_day_count"] == 0)
# «заработай 60M» после престижа тоже недостижим
db.exec("DELETE FROM orders WHERE user_id = ?", (UID,))
db.exec("INSERT INTO orders (user_id, slot, template, metric, goal, progress, "
        "reward_cookies, reward_bp_xp, status, created_at) "
        "VALUES (?, 1, 'profit', 'earned', 60000000, 0, 100, 10, 'active', ?)",
        (UID, time.time()))
db.exec("DELETE FROM farm WHERE user_id = ?", (UID,))
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.update_user(UID, click_level=1)
check("unreachable earned order dropped",
      gl.orders_state(db.get_user(UID))["active"] is None)

# --- отказ от заказа тратит попытку из лимита ---
st = gl.orders_state(db.get_user(UID))
c.post("/api/orders/take", json={"slot": 1}, headers=H)
used_before = db.get_user(UID)["orders_day_count"]
r = c.post("/api/orders/abandon", headers=H)
check("abandon ok", r.status_code == 200 and r.json()["orders"]["active"] is None,
      r.text[:200])
check("abandon costs one daily order",
      db.get_user(UID)["orders_day_count"] == used_before + 1)
r = c.post("/api/orders/abandon", headers=H)
check("abandon without order blocked", r.status_code == 400)

# --- премиум, купленный на стыке сезонов, переезжает в новый ---
db.update_user(UID, bp_premium=0, bp_premium_next=0)
gl.grant_bp_premium(UID, now=gl.season_end_ts(gl.current_season()) - 3600)
u = db.get_user(UID)
check("premium carries over when bought near rollover",
      u["bp_premium"] == 1 and u["bp_premium_next"] == 1, str(dict(u)["bp_premium_next"]))
db.update_user(UID, bp_premium=0, bp_premium_next=0)
gl.grant_bp_premium(UID, now=gl.season_end_ts(gl.current_season()) - 10 * 86400)
check("premium mid-season does not carry",
      db.get_user(UID)["bp_premium_next"] == 0)
# ролловер переносит bp_premium_next в bp_premium
db.update_user(UID, bp_premium=1, bp_premium_next=1,
               season_id=gl.current_season() - 1, season_earned=1234)
gl.finalize_seasons()
u = db.get_user(UID)
check("rollover keeps carried premium", u["bp_premium"] == 1 and u["bp_premium_next"] == 0,
      str((u["bp_premium"], u["bp_premium_next"])))
check("rollover reset season progress", u["season_earned"] == 0 and u["bp_xp"] == 0)

# --- легаси-доска: занятые клетки не отбираем, обычную не переставляем ---
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.update_user(UID, level=1)
for cell in (0, 3, 20, 24):
    db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, ?, 2, 100)",
            (UID, cell))
open_cells = gl.merge_cells_unlocked_for(db.get_user(UID))
cells_now = sorted(r["cell"] for r in
                   db.q("SELECT cell FROM board WHERE user_id = ?", (UID,)))
check("legacy board compacted", cells_now == [0, 1, 2, 3], str(cells_now))
check("legacy keeps base cells", open_cells == cfg.MERGE_BASE_CELLS, str(open_cells))
# доска внутри заслуженной зоны с дыркой — не трогаем
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
for cell in (0, 2, 5):
    db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, ?, 2, 100)",
            (UID, cell))
gl.merge_cells_unlocked_for(db.get_user(UID))
check("normal board left alone",
      sorted(r["cell"] for r in db.q("SELECT cell FROM board WHERE user_id = ?", (UID,)))
      == [0, 2, 5])

# --- спавн ложится рядом с занятыми клетками ---
db.exec("DELETE FROM board WHERE user_id = ?", (UID,))
db.update_user(UID, cookies=10_000_000)
db.exec("INSERT INTO board (user_id, cell, item_level, paid) VALUES (?, 6, 2, 100)", (UID,))
c.post("/api/merge/spawn", json={"level": 1}, headers=H)
spawned = [r["cell"] for r in
           db.q("SELECT cell FROM board WHERE user_id = ? AND item_level = 1", (UID,))]
check("spawn lands next to existing cookie", spawned and spawned[0] in (1, 5, 7, 11),
      str(spawned))

# --- лидерборд ранжирует по заработку за сезон ---
r = c.get("/api/leaderboard", headers=H)
top = r.json()["top"]
check("leaderboard sorted by season_earned",
      all(top[i]["season_earned"] >= top[i + 1]["season_earned"]
          for i in range(len(top) - 1)), str([x["season_earned"] for x in top]))

# --- прогресс квестов пишется без предварительного ensure ---
day = gl._utc_day(time.time())
# метрику берём из фактических заданий дня: пул выбирается детерминированно,
# и «кликов» среди сегодняшних трёх может не оказаться
metric = cfg.DAILY_QUEST_POOL[gl.todays_quest_keys(day)[0]]["metric"]
db.exec("DELETE FROM daily_quests WHERE user_id = ?", (UID,))
gl._quest_rows_ready.discard((UID, day))
gl.quest_progress(UID, metric, 7)
rows = db.q("SELECT quest_key, progress FROM daily_quests WHERE user_id = ? AND day = ?",
            (UID, day))
check("quest rows created lazily", len(rows) == cfg.DAILY_QUESTS_PER_DAY, str(rows))
hit = [r for r in rows if cfg.DAILY_QUEST_POOL[r["quest_key"]]["metric"] == metric]
check(f"progress applied to '{metric}' quests",
      len(hit) >= 1 and all(r["progress"] == 7 for r in hit), str(hit))
# claimed-задания прогресс не получают
db.exec("UPDATE daily_quests SET claimed = 1 WHERE user_id = ? AND day = ?", (UID, day))
gl.quest_progress(UID, metric, 5)
check("claimed quests do not advance",
      all(r["progress"] == 7 for r in
          db.q("SELECT progress FROM daily_quests WHERE user_id = ? AND day = ? "
               "AND quest_key IN (%s)" % ", ".join("?" * len(hit)),
               (UID, day, *[r["quest_key"] for r in hit]))))

print(f"\n{ok} passed, {fail} failed")
if fail:
    raise SystemExit(1)
