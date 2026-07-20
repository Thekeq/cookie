"""Фоновые пуш-уведомления: энергия полная / ферма забита / стрик сгорает.

Правила, чтобы не превратиться в спам и не ловить блокировки:
- не чаще одного пуша в NOTIFY_MIN_INTERVAL_H часов на юзера;
- не пушим тем, кто был онлайн последние NOTIFY_SKIP_ACTIVE_H часов
  (они и так в игре — им это покажет интерфейс);
- юзеров, заблокировавших бота, помечаем notify_blocked и больше не трогаем.
"""
import asyncio
import datetime
import logging
import time

from aiogram.exceptions import TelegramForbiddenError

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db
from server.i18n import tr

CHECK_INTERVAL = 15 * 60  # проверяем всех раз в 15 минут

log = logging.getLogger(__name__)


def _pick_notification(user: dict, now: float) -> str | None:
    """Возвращает текст пуша или None. Приоритет: стрик > ферма > энергия."""
    lang = user.get("lang") or "en"
    # 1) стрик сгорает: забирал вчера, сегодня ещё нет, до полуночи UTC < 4 часов
    if user["daily_streak"] >= 2 and user["daily_claimed_at"]:
        dt = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
        seconds_left = ((24 - dt.hour) * 3600) - dt.minute * 60 - dt.second
        last_day = datetime.datetime.fromtimestamp(
            user["daily_claimed_at"], datetime.timezone.utc).strftime("%Y-%m-%d")
        yesterday = (dt - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if last_day == yesterday and seconds_left < 4 * 3600:
            return tr(lang, "notif_streak", days=user["daily_streak"])

    # 2) ферма упёрлась в оффлайн-кап — доход простаивает
    if user["farm_collected_at"]:
        idle_h = (now - user["farm_collected_at"]) / 3600
        if idle_h >= gl.farm_offline_cap_hours(user) and gl.farm_cps(user["user_id"]) > 0:
            return tr(lang, "notif_farm")

    # 3) энергия полная
    eff = gl.upgrade_effects(user["user_id"])
    cap = gl.energy_cap(user, eff)
    regen = cfg.ENERGY_REGEN_PER_SEC + eff["energy_regen"]
    energy_now = min(cap, user["energy"] + (now - (user["energy_updated_at"] or now)) * regen)
    if energy_now >= cap:
        return tr(lang, "notif_energy")

    return None


async def _notify_pass(bot):
    now = time.time()
    users = db.q(
        "SELECT * FROM users WHERE notify_blocked = 0 "
        "AND last_notified_at < ? AND last_seen_at < ? AND last_seen_at > 0",
        (now - cfg.NOTIFY_MIN_INTERVAL_H * 3600,
         now - cfg.NOTIFY_SKIP_ACTIVE_H * 3600))
    for user in users:
        text = _pick_notification(user, now)
        if not text:
            continue
        try:
            await bot.send_message(user["user_id"], text)
            db.update_user(user["user_id"], last_notified_at=now)
        except TelegramForbiddenError:
            db.exec("UPDATE users SET notify_blocked = 1 WHERE user_id = ?",
                    (user["user_id"],))
        except Exception as e:
            log.warning("notify %s failed: %s", user["user_id"], e)
        await asyncio.sleep(0.05)


async def run_notifier(bot):
    while True:
        try:
            await _notify_pass(bot)
        except Exception:
            log.exception("notifier pass failed")
        await asyncio.sleep(CHECK_INTERVAL)
