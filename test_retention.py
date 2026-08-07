"""Смоук-тесты механик удержания: золотая печенька, комбо, престиж, энергия."""
import hashlib
import hmac
import json
import os
import sys
import time
from urllib.parse import urlencode

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# тесты живут во ВРЕМЕННОЙ базе — рабочая data.db не трогается. Файл сносится
# на старте: имя завязано на PID, а система переиспользует PID'ы, и строки от
# прошлого запуска давали «раз в N запусков» падения на пустом месте
import tempfile
DB_PATH = os.path.join(tempfile.gettempdir(), f"cookie_test_{os.getpid()}.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        os.remove(DB_PATH + _suffix)
os.environ["DATABASE_PATH"] = DB_PATH

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
check("energy base 400", s["user"]["max_energy"] >= 400, str(s["user"]["max_energy"]))
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

# два тапа ОДНИМ И ТЕМ ЖЕ словарём — по золотой печеньке тапают именно так, и
# оба запроса видят её живой. Гасит печеньку условный UPDATE, он же решает, кому
# платить; эффект фиксируем на chain, чтобы награда была деньгами, а не бустом
db.update_user(UID, golden_expires_at=time.time() + 60, golden_effect="chain")
_g_user = db.get_user(UID)
_g_before = _g_user["cookies"]
_g_paid = gl.claim_golden(_g_user)["bonus"]
check("stale golden paid", _g_paid > 0 and
      db.get_user(UID)["cookies"] - _g_before > 0, str(_g_paid))
try:
    gl.claim_golden(_g_user)
    check("stale golden refused", False, "второй тап прошёл")
except ValueError as e:
    check("stale golden refused", str(e) == "err_golden_gone", str(e))
check("stale golden paid once",
      abs(db.get_user(UID)["cookies"] - _g_before - _g_paid) < 1e-6,
      f"{db.get_user(UID)['cookies'] - _g_before} != {_g_paid}")

# --- комбо ---
db.update_user(UID, energy=2000, combo_mult=1, combo_last_at=0)
r = c.post("/api/click", json={"clicks": 10, "batch_id": "auto-ret-1"}, headers=H)
check("first batch combo=1", r.json().get("combo") == 1.0, str(r.json().get("combo")))
time.sleep(1.2)  # 10 кликов за 1.2с = ~8 cps > COMBO_MIN_CPS, окно не истекло
r = c.post("/api/click", json={"clicks": 10, "batch_id": "auto-ret-2"}, headers=H)
check("combo grows", r.json().get("combo", 0) > 1.0, str(r.json().get("combo")))
# пауза дольше окна — сброс
db.update_user(UID, combo_last_at=time.time() - cfg.COMBO_WINDOW - 2)
r = c.post("/api/click", json={"clicks": 10, "batch_id": "auto-ret-3"}, headers=H)
check("combo resets after pause", r.json().get("combo") == 1.0, str(r.json().get("combo")))

# --- дедупликация клик-батчей по batch_id ---
db.update_user(UID, energy=2000)
time.sleep(1.0)  # даём CPS-окну накопить allowance
r = c.post("/api/click", json={"clicks": 5, "batch_id": "batch-A"}, headers=H)
check("batch accepted", r.json()["accepted"] == 5, r.text[:120])
cookies_after = db.get_user(UID)["cookies"]
r = c.post("/api/click", json={"clicks": 5, "batch_id": "batch-A"}, headers=H)  # ретрай
check("duplicate batch not credited",
      r.json().get("duplicate") is True and r.json()["accepted"] == 0, r.text[:120])
check("cookies unchanged on dup", db.get_user(UID)["cookies"] == cookies_after)
time.sleep(0.7)
r = c.post("/api/click", json={"clicks": 3, "batch_id": "batch-B"}, headers=H)  # другой батч
check("new batch credited", r.json()["accepted"] == 3, r.text[:120])

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
_lvl_before = db.get_user(UID)["level"]
r = c.post("/api/prestige", headers=H)
check("prestige done", r.status_code == 200, r.text[:200])
s = r.json()
u = db.get_user(UID)
# престиж сохраняет часть уровня: полный откат на 1-й делал перерождение
# бессмысленным (заново все req_level, а множитель этого не ускорял)
_kept = gl.prestige_kept_level(_lvl_before)
check("progress reset", u["cookies"] == 0 and u["click_level"] == 1
      and u["level"] == _kept, f"lvl {u['level']} != {_kept}")
check("prestige keeps part of the level", _kept >= 1 and _kept <= _lvl_before)
check("farm wiped", gl.farm_counts(UID) == {})
check("skins kept", db.q1("SELECT id FROM skins WHERE user_id = ? AND skin_key = 'donut'",
                          (UID,)) is not None)
check("points saved", u["prestige_points"] == 5 and u["prestige_count"] == 1)
# ивент выходных тоже множит клик — учитываем, иначе тест падает по субботам
_expect_mult = (1 + 5 * cfg.PRESTIGE_MULT_PER_POINT) * gl.event_multiplier()
check("multiplier applied", abs(s["user"]["click_power"] - _expect_mult) < 0.001,
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
check("bp lvl1 costs 420", cfg.bp_xp_for_level(1) == 420)
check("bp lvl30 costs 12600", cfg.bp_xp_for_level(30) == 12600)
check("bp cumulative consistent",
      cfg.bp_total_xp(30) == sum(cfg.bp_xp_for_level(l) for l in range(1, 31)))
check("bp_level_for_xp", cfg.bp_level_for_xp(cfg.bp_total_xp(5)) == 5
      and cfg.bp_level_for_xp(cfg.bp_total_xp(5) - 1) == 4)

# дневной кап XP кликов: после 10k кликов XP режется вчетверо
# ниже потолка уровней: на MAX_LEVEL XP переливается в батл-пасс, и проверять
# кап кликового XP на профильном xp уже нельзя
db.update_user(UID, energy=200000, clicks_day=gl._utc_day(time.time()),
               clicks_day_count=cfg.CLICK_XP_SOFT_CAP, combo_last_at=0,
               level=min(db.get_user(UID)["level"], cfg.MAX_LEVEL - 1))
xp_before = db.get_user(UID)["xp"]
db.exec("DELETE FROM daily_quests WHERE user_id = ?", (UID,))
db.update_user(UID, cps_ts=0, cps_allowance=0)  # сброс CPS-окна (теперь в БД)
r = c.post("/api/click", json={"clicks": 40, "batch_id": "auto-ret-4"}, headers=H)
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

# --- очередь уведомлений: планировщик кладёт, воркер отправляет ---
# Проверяем не «функция что-то вернула», а свойства, ради которых очередь и
# заводилась: одно событие — одно сообщение, партию забирает один воркер,
# протухшее событие не отправляется, ночью не пишем, ретрай уважает Telegram.
import asyncio
import datetime

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

import bot.notifier as notifier
from server import notifications as notif
from server import settings as srv_settings
from server.i18n import tr

NOW = time.time()


def notify_user(uid: int, **fields):
    """Игрок, которому можно писать прямо сейчас: пояс подобран так, чтобы у
    него был полдень, иначе тест ломался бы по ночам (и это правильно —
    ровно так же он ломается у игрока)."""
    db.create_user(uid, f"nq{uid}", "NQ")
    utc = datetime.datetime.fromtimestamp(NOW, datetime.timezone.utc)
    db.update_user(uid, **{"tz_offset_min": (12 - utc.hour) * 60 - utc.minute,
                           "lang": "ru", "notify_blocked": 0,
                           "last_notified_at": 0,
                           "last_seen_at": NOW - 5 * 3600, **fields})
    return db.get_user(uid)


NQ = UID + 1
notify_user(NQ)

# 1. Дедупликация: проход планировщика идёт каждые 15 минут и видит одно и то
# же готовое событие до самого его закрытия
check("nq: enqueue ok", notif.enqueue(NQ, "comeback", {"days": 2, "seen": 1},
                                      dedup_key=f"t:cb:{NQ}") is True)
check("nq: same event enqueued once",
      notif.enqueue(NQ, "comeback", {"days": 2, "seen": 1},
                    dedup_key=f"t:cb:{NQ}") is False
      and db.q1("SELECT COUNT(*) c FROM notification_queue WHERE user_id = ?",
                (NQ,))["c"] == 1)

# 2. Взятие партии: строка достаётся ровно одному воркеру, будущее не берётся
notif.enqueue(NQ, "energy_full", {}, dedup_key=f"t:later:{NQ}",
              scheduled_at=NOW + 3600)
_claimed = notif.claim(10)
check("nq: batch claim marks rows sending",
      len(_claimed) == 1 and _claimed[0]["status"] == "sending"
      and _claimed[0]["attempts"] == 1, str(len(_claimed)))
check("nq: second worker gets nothing", notif.claim(10) == [])
check("nq: future task is not claimed",
      db.q1("SELECT status FROM notification_queue WHERE dedup_key = ?",
            (f"t:later:{NQ}",))["status"] == "scheduled")
# заявка убитого воркера возвращается в очередь, а не висит вечно
db.exec("UPDATE notification_queue SET claimed_at = ? WHERE status = 'sending'",
        (NOW - notif.LEASE_S - 60,))
check("nq: stale claim returns to queue", notif.requeue_stale(NOW) == 1)

# 3. Тихие часы считаются по поясу игрока, а не по UTC
_night = dict(db.get_user(NQ), tz_offset_min=180)      # UTC+3
_msk_3am = datetime.datetime(2026, 6, 1, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
_msk_noon = _msk_3am + 9 * 3600
check("nq: 03:00 local is quiet", notif.quiet_until(_night, _msk_3am) > _msk_3am)
check("nq: quiet ends at local morning",
      abs(notif.quiet_until(_night, _msk_3am)
          - (_msk_3am + (notif.QUIET_END_H - 3) * 3600)) < 1)
check("nq: noon is not quiet", notif.quiet_until(_night, _msk_noon) == 0.0)
check("nq: the same instant differs by timezone",
      notif.quiet_until(dict(_night, tz_offset_min=-300), _msk_3am) == 0.0)

# 4. Частотные лимиты: общий, по категории и минимальный промежуток
db.exec("DELETE FROM notification_queue WHERE user_id = ?", (NQ,))
for i in range(notif.CATEGORY_CAP["duel"]):
    db.exec("INSERT INTO notification_queue (user_id, kind, category, payload, "
            "scheduled_at, status, dedup_key, attempts, sent_at, created_at) "
            "VALUES (?, 'duel_result', 'duel', '{}', ?, 'sent', ?, 1, ?, ?)",
            (NQ, NOW, f"t:cap:{NQ}:{i}", NOW - 2 * 3600 - i, NOW))
check("nq: category cap blocks the next duel push",
      notif.cap_delay(NQ, "duel", NOW) > 0)
check("nq: another category still goes through",
      notif.cap_delay(NQ, "recipe", NOW) == 0)
db.exec("UPDATE notification_queue SET sent_at = ? WHERE user_id = ?",
        (NOW - 60, NQ))
check("nq: min gap between any two pushes",
      notif.cap_delay(NQ, "recipe", NOW) >= notif.MIN_GAP_S - 61)
db.exec("DELETE FROM notification_queue WHERE user_id = ?", (NQ,))

# 5. Диплинк: кнопка ведёт на конкретную вкладку и приносит номер сообщения
check("nq: deep link carries tab, segment and id",
      notif.parse_start_param(notif.start_param("bp_unclaimed", 42))
      == {"tab": "progress", "segment": "bp", "notification_id": 42})
check("nq: tab-less param is not treated as a deep link",
      notif.parse_start_param("src_blog") == {}
      and notif.parse_start_param("") == {})
check("nq: recipe push points at the farm",
      notif.parse_start_param(notif.start_param("recipe_ready"))["tab"] == "farm")
_saved_url, _saved_user = srv_settings.WEBAPP_URL, srv_settings.BOT_USERNAME
srv_settings.WEBAPP_URL = "https://example.test"
_mode, _url = notif.deep_link("order_waiting", 7)
check("nq: webapp button when WEBAPP_URL is set",
      _mode == "webapp" and _url.endswith("?tgWebAppStartParam=tab-bakery-n7"), _url)
srv_settings.WEBAPP_URL, srv_settings.BOT_USERNAME = "", "cookiebot"
_mode2, _url2 = notif.deep_link("order_waiting", 7)
check("nq: t.me deep link without WEBAPP_URL",
      _mode2 == "url" and _url2 == "https://t.me/cookiebot/app?startapp=tab-bakery-n7",
      _url2)

# 6. Тексты есть на всех трёх языках и подставляются
_missing = [k for k, v in notif.TEXTS.items() if set(v) != {"en", "uk", "ru"}]
check("nq: all texts have en/uk/ru", not _missing, str(_missing))
check("nq: text renders with payload",
      "x1.45" in notif.render("recipe_ready", "ru", {"mult": 1.45}))
check("nq: legacy kinds reuse the shared i18n dictionary",
      notif.render("energy_full", "ru", {}) == tr("ru", "notif_energy"))
check("nq: missing payload does not break rendering",
      notif.render("season_end", "en", {}) != "")

# 7. Актуальность события проверяется ПЕРЕД отправкой
notify_user(NQ, recipe_key="classic", recipe_started_at=NOW - 5 * 3600, level=5)
_u = db.get_user(NQ)
_row = {"id": 0, "kind": "recipe_ready", "created_at": NOW,
        "payload": json.dumps({"started": int(_u["recipe_started_at"])})}
check("nq: ready recipe is still relevant", notif.still_relevant(_row, _u, NOW))
db.update_user(NQ, recipe_key=None, recipe_started_at=0)     # тесто забрали
check("nq: collected recipe is not announced",
      notif.still_relevant(_row, db.get_user(NQ), NOW) is False)
check("nq: event older than its ttl is not announced",
      notif.still_relevant(dict(_row, created_at=NOW - 30 * 3600), _u, NOW) is False)

# 8. Планировщик: одно событие — одна строка, второй проход не плодит копий
db.exec("DELETE FROM notification_queue")
_pl = UID + 2
notify_user(_pl, recipe_key="classic", recipe_started_at=NOW - 5 * 3600, level=5,
            last_seen_at=NOW - 5 * 3600)
_made = notif.plan_pass(NOW)
check("nq: planner queues the ready recipe", _made["recipes"] >= 1, str(_made))
_q = db.q1("SELECT * FROM notification_queue WHERE user_id = ? AND kind = "
           "'recipe_ready'", (_pl,))
check("nq: dedup key carries the event itself",
      _q and _q["dedup_key"].endswith(str(int(NOW - 5 * 3600))), str(_q))
_before = db.q1("SELECT COUNT(*) c FROM notification_queue")["c"]
notif.plan_pass(NOW)
check("nq: second planner pass adds nothing",
      db.q1("SELECT COUNT(*) c FROM notification_queue")["c"] == _before)
# игрок, которого не было неделю, получает comeback ровно один раз
_cb = UID + 3
notify_user(_cb, last_seen_at=NOW - 7.2 * 86400)
notif.plan_pass(NOW)
_cbrow = db.q1("SELECT payload FROM notification_queue WHERE user_id = ? "
               "AND kind = 'comeback'", (_cb,))
check("nq: comeback on day 7", _cbrow and json.loads(_cbrow["payload"])["days"] == 7,
      str(_cbrow))


class FakeBot:
    """Telegram, который отвечает так, как скажут."""

    def __init__(self, error=None):
        self.error, self.sent = error, []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        if self.error:
            raise self.error
        self.sent.append((chat_id, text, reply_markup))


# 9. Отправка целиком: воркер берёт партию, шлёт с кнопкой и закрывает строку
srv_settings.WEBAPP_URL = "https://example.test"
db.exec("DELETE FROM notification_queue")
_send = UID + 4
notify_user(_send)
# «seen» — отметка последнего визита: по ней воркер решит, не вернулся ли
# игрок сам, пока сообщение лежало в очереди
notif.enqueue(_send, "comeback",
              {"days": 2, "seen": db.get_user(_send)["last_seen_at"]},
              dedup_key=f"t:send:{_send}")
_bot = FakeBot()
_sent_n = asyncio.run(notifier._send_pass(_bot))
_row9 = db.q1("SELECT * FROM notification_queue WHERE user_id = ?", (_send,))
check("nq: worker sends the queued push", _sent_n == 1 and len(_bot.sent) == 1)
check("nq: status becomes sent and time is recorded",
      _row9["status"] == "sent" and _row9["sent_at"] > 0, str(_row9["status"]))
check("nq: deep-link button is attached",
      _bot.sent[0][2] is not None
      and "tab-clicker" in _bot.sent[0][2].inline_keyboard[0][0].web_app.url)
check("nq: global gap mark is kept in users",
      db.get_user(_send)["last_notified_at"] > 0)
check("nq: opening the app is recorded",
      notif.mark_opened(_row9["id"], _send) is True
      and db.q1("SELECT status FROM notification_queue WHERE id = ?",
                (_row9["id"],))["status"] == "opened")
check("nq: someone else cannot mark my push opened",
      notif.mark_opened(_row9["id"], _send + 999) is False)

# 10. Заблокировавший бота: гасим и строку, и всё, что ему было запланировано
db.exec("DELETE FROM notification_queue")
_blk = UID + 5
notify_user(_blk)
notif.enqueue(_blk, "comeback",
              {"days": 2, "seen": db.get_user(_blk)["last_seen_at"]},
              dedup_key=f"t:blk:{_blk}")
notif.enqueue(_blk, "energy_full", {}, dedup_key=f"t:blk2:{_blk}",
              scheduled_at=NOW + 7200)
asyncio.run(notifier._send_pass(
    FakeBot(TelegramForbiddenError(method=None, message="bot was blocked"))))
check("nq: blocked user is marked in users",
      db.get_user(_blk)["notify_blocked"] == 1)
_statuses = {r["kind"]: r["status"] for r in db.q(
    "SELECT kind, status FROM notification_queue WHERE user_id = ?", (_blk,))}
check("nq: blocked push and the rest of the queue are closed",
      _statuses == {"comeback": "blocked", "energy_full": "cancelled"},
      str(_statuses))

# 11. Ретраи уважают retry_after от Telegram
db.exec("DELETE FROM notification_queue")
_rty = UID + 6
notify_user(_rty)
notif.enqueue(_rty, "comeback",
              {"days": 2, "seen": db.get_user(_rty)["last_seen_at"]},
              dedup_key=f"t:rty:{_rty}")
_t0 = time.time()
asyncio.run(notifier._send_pass(
    FakeBot(TelegramRetryAfter(method=None, message="flood", retry_after=30))))
_row11 = db.q1("SELECT * FROM notification_queue WHERE user_id = ?", (_rty,))
check("nq: flood control returns the push to the queue",
      _row11["status"] == "scheduled", str(_row11["status"]))
check("nq: retry_after is respected to the second",
      29 <= _row11["scheduled_at"] - _t0 <= 62,
      str(_row11["scheduled_at"] - _t0))
# попытки не бесконечны: строка, которая не уходит, становится failed
db.exec("UPDATE notification_queue SET attempts = ?, status = 'sending' "
        "WHERE id = ?", (notif.MAX_ATTEMPTS, _row11["id"]))
notif.mark_failed(dict(_row11, attempts=notif.MAX_ATTEMPTS), "boom")
check("nq: retries are not endless",
      db.q1("SELECT status FROM notification_queue WHERE id = ?",
            (_row11["id"],))["status"] == "failed")

# 12. Тихие часы и лимиты не теряют сообщение, а переносят его
db.exec("DELETE FROM notification_queue")
_qt = UID + 7
notify_user(_qt)
db.exec("UPDATE users SET tz_offset_min = NULL WHERE user_id = ?", (_qt,))
notif.enqueue(_qt, "comeback",
              {"days": 2, "seen": db.get_user(_qt)["last_seen_at"]},
              dedup_key=f"t:qt:{_qt}")
_night_row = db.q1("SELECT * FROM notification_queue WHERE user_id = ?", (_qt,))
_u12 = db.get_user(_qt)
check("nq: unknown timezone falls back to the language",
      notif.tz_offset_min(dict(_u12, lang="ru")) == notif.LANG_TZ_MIN["ru"])
notif.defer(_night_row, NOW + 3600, "quiet_hours")
_row12 = db.q1("SELECT * FROM notification_queue WHERE id = ?", (_night_row["id"],))
check("nq: deferred push stays in the queue with a new time",
      _row12["status"] == "scheduled" and _row12["scheduled_at"] == NOW + 3600)
check("nq: deferral does not burn a retry attempt", _row12["attempts"] == 0)

# 13. Чистилка: разобранные строки живут TTL, незакрытые остаются
db.exec("DELETE FROM notification_queue")
db.exec("INSERT INTO notification_queue (user_id, kind, category, payload, "
        "scheduled_at, status, dedup_key, attempts, created_at) VALUES "
        "(?, 'comeback', 'comeback', '{}', ?, 'sent', 't:old', 1, ?)",
        (NQ, NOW, NOW - (notif.TTL_DAYS + 1) * 86400))
db.exec("INSERT INTO notification_queue (user_id, kind, category, payload, "
        "scheduled_at, status, dedup_key, attempts, created_at) VALUES "
        "(?, 'comeback', 'comeback', '{}', ?, 'scheduled', 't:old2', 0, ?)",
        (NQ, NOW, NOW - (notif.TTL_DAYS + 1) * 86400))
check("nq: prune drops only finished rows",
      notif.prune(NOW) == 1
      and db.q1("SELECT COUNT(*) c FROM notification_queue")["c"] == 1)

srv_settings.WEBAPP_URL, srv_settings.BOT_USERNAME = _saved_url, _saved_user
db.exec("DELETE FROM notification_queue")
for _u in (NQ, _pl, _cb, _send, _blk, _rty, _qt):
    db.exec("DELETE FROM users WHERE user_id = ?", (_u,))
db.exec("DELETE FROM orders WHERE user_id > ?", (UID,))

# cleanup
for t in ("users", "board", "farm", "upgrades", "skins", "daily_quests",
          "ref_claims", "achievements", "boosts", "purchases"):
    db.exec(f"DELETE FROM {t} WHERE user_id = ?", (UID,))
db.exec("DELETE FROM season_results WHERE user_id = ?", (UID,))

print(f"\n{ok} passed, {fail} failed")
sys.exit(1 if fail else 0)
