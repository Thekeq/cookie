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

# --- цены доски ---
# Путь слияния до тира L стоит 2^(L-1) спавнов, поэтому цена L1 обязана быть
# почти абсолютной: любая привязка к доходу умножается на 2^L и делает слияние
# в тысячи раз дороже прямой покупки, то есть убивает основной цикл.
check("lvl1 spawn does not scale with income",
      cfg.spawn_cost(0, 0) == cfg.spawn_cost(0, 1e12) == cfg.SPAWN_L1_BASE)
check("lvl1 spawn grows with occupancy",
      cfg.spawn_cost(24) > cfg.spawn_cost(0) * 3)
# слияние ОБЯЗАНО быть выгоднее прямой покупки на всех тирах, и разрыв растёт
_gaps = []
for _lvl, _cells in ((6, 8), (10, 12), (16, 20)):
    _direct = cfg.direct_spawn_cost(_lvl, _cells)
    _merge = (2 ** (_lvl - 1)) * cfg.spawn_cost(_cells)
    _gaps.append(_direct / _merge)
    check(f"merge cheaper than direct at lvl{_lvl}", _direct > _merge,
          f"{_direct:.0f} vs {_merge:.0f}")
check("direct premium grows with tier", _gaps == sorted(_gaps), str(_gaps))
# прямая покупка окупается за разумное время, а не за миллионы часов
for _lvl, _cells in ((6, 8), (16, 20)):
    _h = cfg.direct_spawn_cost(_lvl, _cells) / cfg.passive_income_per_hour(_lvl)
    check(f"direct lvl{_lvl} pays back in {_h:.0f}h", _h <= 60, f"{_h:.1f}h")

# --- слияние ОБЯЗАНО увеличивать доход, а не уменьшать ---
# при базе 1.7 каждый мердж выше 12 уровня давал -15%: две печеньки давали
# больше, чем одна следующего уровня, и основной цикл работал против игрока
for _l in range(3, cfg.MAX_ITEM_LEVEL):
    _two = 2 * cfg.passive_income_per_hour(_l)
    _one = cfg.passive_income_per_hour(_l + 1)
    check(f"merge L{_l} raises income", _one > _two,
          f"{_two:.0f} -> {_one:.0f}")

# --- клик обязан оставаться живым ---
# Окупаемость мягко растёт (сила 1.55 против цены 1.8), но остаётся в часах,
# а не в годах: при линейной силе апгрейд с 30 уровня окупался 52 000 часов.
for _cl in (1, 10, 20, 30):
    _inc = cfg.ENERGY_REGEN_PER_SEC * 3600 * cfg.click_power(_cl)
    _pay = cfg.click_upgrade_cost(_cl) / _inc
    check(f"click upgrade at lvl{_cl} pays back in {_pay:.2f}h", _pay <= 8.0,
          f"{_pay:.2f}h")
# потолок по уровню игрока не даёт ветке клика разгонять инфляцию
check("click level gated by player level",
      cfg.click_max_level(1) < cfg.click_max_level(30) <= 70,
      f"{cfg.click_max_level(1)}..{cfg.click_max_level(30)}")

# --- ферма не должна окупаться мгновенно ---
for _k, _v in cfg.FARM_BUILDINGS.items():
    _min = _v["base_cost"] / (_v["cps"] * 3600) * 60
    check(f"farm {_k} pays back in {_min:.0f} min", 10 <= _min <= 90, f"{_min:.1f}")

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
# hourly_income мемоизируется на секунду (иначе он звался по 15 раз за запрос);
# правка БД в обход API мемо не сбрасывает — в бою его сбрасывает full_state
gl.invalidate_income(UID)
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


# ================= платежи Stars: денежные пути =================

# --- премиум, купленный ПОСЛЕ смены сезона, переживает пакетный ролловер ---
# Сброс идёт порциями по 500, и строка игрока может ещё не быть обработана.
# Раньше безусловное bp_premium = bp_premium_next стирало свежую покупку.
db.update_user(UID, season_id=gl.current_season() - 1, bp_premium=0, bp_premium_next=0)
gl.grant_bp_premium(UID)
check("premium bought mid-rollover flags carry-over",
      db.get_user(UID)["bp_premium_next"] == 1)
gl.finalize_seasons()
check("premium survives the rollover",
      db.get_user(UID)["bp_premium"] == 1 and
      db.get_user(UID)["season_id"] == gl.current_season())
# а премиум ПРОШЛОГО сезона без флага переноса обязан сгореть
db.update_user(UID, season_id=gl.current_season() - 1, bp_premium=1, bp_premium_next=0)
gl.finalize_seasons()
check("old-season premium still expires", db.get_user(UID)["bp_premium"] == 0)

# --- повторная покупка постоянного товара блокируется на сервере ---
db.update_user(UID, bp_premium=1)
check("repeat bp_premium blocked", gl.purchase_blocked(UID, "bp_premium") == "err_owned")
db.update_user(UID, bp_premium=0)
check("first bp_premium allowed", gl.purchase_blocked(UID, "bp_premium") is None)

# --- оффлайн-кап: апгрейд по цене разницы ---
db.update_user(UID, offline_bonus_hours=0)
check("upgrade tier hidden without base",
      gl.purchase_blocked(UID, "offline_cap_12h_up") == "err_needs_base")
check("base tier sellable", gl.purchase_blocked(UID, "offline_cap_6h") is None)
db.update_user(UID, offline_bonus_hours=3)
check("upgrade tier sellable to 6h owner",
      gl.purchase_blocked(UID, "offline_cap_12h_up") is None)
check("base tier not sold twice",
      gl.purchase_blocked(UID, "offline_cap_6h") == "err_owned")
db.update_user(UID, offline_bonus_hours=9)
check("nothing sold at top tier",
      gl.purchase_blocked(UID, "offline_cap_12h_up") == "err_owned"
      and gl.purchase_blocked(UID, "offline_cap_12h") == "err_owned")
db.update_user(UID, offline_bonus_hours=0)

# --- платёж, не сошедшийся с конфигом, оставляет след ---
db.exec("DELETE FROM purchases WHERE user_id = ?", (UID,))
gl.record_unmatched_payment(UID, "ghost_item", 250, "charge-unmatched-1", "x:ghost_item")
row = db.q1("SELECT status, stars_amount FROM purchases WHERE tg_payment_id = ?",
            ("charge-unmatched-1",))
check("unmatched payment recorded",
      row and row["status"] == "unmatched" and row["stars_amount"] == 250, str(row))

# --- товар исчез из конфига: покупка помечается void, а не висит в paid ---
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'removed_item', 100, 'charge-void-1', 'paid', ?)",
        (UID, time.time()))
check("gone item is not fulfilled", gl.fulfill_charge("charge-void-1") is False)
check("gone item marked void",
      db.q1("SELECT status FROM purchases WHERE tg_payment_id = 'charge-void-1'"
            )["status"] == "void")

# --- возврат звёзд снимает выданное ---
db.update_user(UID, bp_premium=0, bp_premium_next=0)
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'bp_premium', 100, 'charge-ref-1', 'paid', ?)",
        (UID, time.time()))
check("premium fulfilled", gl.fulfill_charge("charge-ref-1") is True
      and db.get_user(UID)["bp_premium"] == 1)
check("refund processed", gl.revoke_charge("charge-ref-1") is True)
check("premium revoked on refund", db.get_user(UID)["bp_premium"] == 0)
check("refund is idempotent", gl.revoke_charge("charge-ref-1") is False)
db.exec("DELETE FROM boosts WHERE user_id = ?", (UID,))
db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
        "status, created_at) VALUES (?, 'boost_x2_1h', 50, 'charge-ref-2', 'paid', ?)",
        (UID, time.time()))
gl.fulfill_charge("charge-ref-2")
check("boost granted by purchase", "click_x2" in gl.active_boosts(UID))
gl.revoke_charge("charge-ref-2")
check("boost revoked on refund", "click_x2" not in gl.active_boosts(UID))

# --- клейм заказа не платит дважды ---
db.exec("DELETE FROM orders WHERE user_id = ?", (UID,))
db.update_user(UID, orders_day=None, orders_day_count=0, cookies=0)
db.exec("INSERT INTO orders (user_id, slot, template, metric, goal, progress, "
        "status, created_at) VALUES (?, 1, 'warmup', 'clicks', 10, 10, 'active', ?)",
        (UID, time.time()))
gl.claim_order(db.get_user(UID))
_paid_once = db.get_user(UID)["cookies"]
try:
    gl.claim_order(db.get_user(UID))
    check("order not claimable twice", False, "второй клейм прошёл")
except ValueError:
    check("order not claimable twice", db.get_user(UID)["cookies"] == _paid_once)


# ================= закваска и ивенты =================

# --- рецепт: рано / в окне / подгорело ---
db.update_user(UID, level=10, recipe_key=None, recipe_started_at=0)
check("no recipe by default", gl.recipe_status(db.get_user(UID))["state"] == "none")
gl.set_recipe(db.get_user(UID), "classic")
check("recipe starts rising", gl.recipe_status(db.get_user(UID))["state"] == "rising")
_r = cfg.RECIPES["classic"]
db.update_user(UID, recipe_started_at=time.time() - _r["hours"] * 3600 - 60)
_st = gl.recipe_status(db.get_user(UID))
check("recipe ready inside window",
      _st["state"] == "ready" and _st["mult"] == _r["mult"], str(_st))
db.update_user(UID, recipe_started_at=time.time()
               - _r["hours"] * _r["window"] * 3600 - 60)
check("recipe burns past the window",
      gl.recipe_status(db.get_user(UID))["state"] == "burnt")

# --- множитель применяется к оффлайн-доходу фермы ровно один раз ---
db.exec("DELETE FROM farm WHERE user_id = ?", (UID,))
db.exec("INSERT INTO farm (user_id, building_key, count) VALUES (?, 'cursor', 10)", (UID,))
db.update_user(UID, cookies=0, recipe_key="classic",
               recipe_started_at=time.time() - _r["hours"] * 3600 - 60,
               farm_collected_at=time.time() - 3600)
_got = gl.collect_farm(db.get_user(UID))
_plain = gl.farm_cps(UID) * 3600
check("ready recipe multiplies offline farm income",
      abs(_got - _plain * _r["mult"]) < _plain * 0.02, f"{_got:.0f} vs {_plain:.0f}")
check("recipe is consumed after collect",
      gl.recipe_status(db.get_user(UID))["state"] == "none")
# рано вернулся — множителя нет и закваска НЕ тратится
db.update_user(UID, recipe_key="classic", recipe_started_at=time.time() - 60,
               farm_collected_at=time.time() - 3600)
_got = gl.collect_farm(db.get_user(UID))
check("early return gives no bonus", abs(_got - _plain) < _plain * 0.02, f"{_got:.0f}")
check("early return keeps the dough",
      gl.recipe_status(db.get_user(UID))["state"] == "rising")
db.update_user(UID, recipe_key=None, recipe_started_at=0)

# --- рецепт по уровню ---
db.update_user(UID, level=1)
try:
    gl.set_recipe(db.get_user(UID), "festive")
    check("locked recipe rejected", False, "прошло без уровня")
except ValueError:
    check("locked recipe rejected", True)
db.update_user(UID, level=10)

# --- ивент детерминирован календарём ---
import datetime as _dt
_sat = _dt.datetime(2026, 7, 25, 12, tzinfo=_dt.timezone.utc).timestamp()
_wed = _dt.datetime(2026, 7, 22, 12, tzinfo=_dt.timezone.utc).timestamp()
check("event on weekend", gl.active_event(_sat) is not None)
check("no event midweek", gl.active_event(_wed) is None)
check("event multiplier follows", gl.event_multiplier(_wed) == 1.0)
_ev = gl.active_event(_sat)
check("event is stable for the same weekend",
      gl.active_event(_sat + 3600)["key"] == _ev["key"])
check("event window covers the weekend",
      _ev["started_at"] <= _sat <= _ev["ends_at"])


# ================= дуэли =================
from server import duels as _duels

_DA, _DB = UID + 5000, UID + 5001
for _u in (_DA, _DB):
    db.create_user(_u, f"d{_u}", f"D{_u}")
    db.update_user(_u, level=5, total_earned=1000, cookies=0)

# уровень ниже порога не пускает
db.update_user(_DA, level=1)
try:
    _duels.find(db.get_user(_DA))
    check("duel gated by level", False, "пустило на 1 уровне")
except ValueError:
    check("duel gated by level", True)
db.update_user(_DA, level=5)

# первый встаёт в очередь, второй подхватывает
check("first player queues",
      _duels.find(db.get_user(_DA))["duel"]["status"] == "waiting")
try:
    _duels.find(db.get_user(_DA))
    check("no second duel at once", False, "вторая дуэль прошла")
except ValueError:
    check("no second duel at once", True)
_st = _duels.find(db.get_user(_DB))
check("second player starts the duel", _st["duel"]["status"] == "active", str(_st))
check("opponent is visible", _st["duel"]["foe_name"] is not None)
check("duel never leaks user_id", "foe_id" not in _st["duel"])

# счёт считается ОТ старта дуэли, а не от накопленного за всю жизнь
db.update_user(_DA, total_earned=1000 + 7000)
db.update_user(_DB, total_earned=1000 + 3000)
_st = _duels.state(db.get_user(_DA))
check("score counts only duel earnings",
      abs(_st["duel"]["my_score"] - 7000) < 1 and abs(_st["duel"]["foe_score"] - 3000) < 1,
      str(_st["duel"]))

# дедлайн закрывает дуэль и назначает победителя
_row = db.q1("SELECT id FROM duels ORDER BY id DESC LIMIT 1")
db.exec("UPDATE duels SET ends_at = ? WHERE id = ?", (time.time() - 1, _row["id"]))
_st = _duels.state(db.get_user(_DA))
check("duel closes on deadline", _st["duel"]["status"] == "done")
check("leader wins", _st["duel"]["won"] is True)
check("loser sees the loss", _duels.state(db.get_user(_DB))["duel"]["won"] is False)

# приз видно ДО согласия на забег: сумму считает та же функция, что и выплату,
# поэтому показанное число не может разойтись с полученным
_shown = _duels.state(db.get_user(_DA))["prize"]
check("prize is shown up front", _shown >= cfg.DUEL_REWARD_MIN, str(_shown))

# приз получает только победитель и только один раз
_before = db.get_user(_DA)["cookies"]
_r = _duels.claim(db.get_user(_DA))
check("winner is paid", db.get_user(_DA)["cookies"] > _before and _r["reward"] > 0)
check("shown prize matches the payout", abs(_r["reward"] - _shown) < 1,
      f"показали {_shown}, выплатили {_r['reward']}")
try:
    _duels.claim(db.get_user(_DA))
    check("prize not paid twice", False, "второй клейм прошёл")
except ValueError:
    check("prize not paid twice", True)
_before_b = db.get_user(_DB)["cookies"]
_r = _duels.claim(db.get_user(_DB))
check("loser gets nothing",
      _r["reward"] == 0 and db.get_user(_DB)["cookies"] == _before_b)
check("duel clears after claim", _duels.state(db.get_user(_DA))["duel"] is None)

# заявку можно снять
_duels.find(db.get_user(_DA))
_duels.cancel(db.get_user(_DA))
_st = _duels.state(db.get_user(_DA))
check("search can be cancelled", _st["duel"] is None)
# приз есть и на экране приглашения, где дуэли ещё нет
check("prize is shown with no duel", _st.get("prize", 0) >= cfg.DUEL_REWARD_MIN, str(_st))

for _u in (_DA, _DB):
    db.exec("DELETE FROM users WHERE user_id = ?", (_u,))
db.exec("DELETE FROM duels WHERE user_a IN (?, ?) OR user_b IN (?, ?)",
        (_DA, _DB, _DA, _DB))

print(f"\n{ok} passed, {fail} failed")
if fail:
    raise SystemExit(1)
