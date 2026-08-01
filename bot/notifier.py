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

from bot import webhook
from server import economy
from server import game_config as cfg
from server import game_logic as gl
from server import scheduler
from server import settings
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


def _prune_events():
    """TTL аналитики: таблица events росла без ограничений (по событию 'session'
    на каждое открытие приложения). Админка смотрит окна максимум 7 дней."""
    db.exec("DELETE FROM events WHERE created_at < ?",
            (time.time() - cfg.EVENTS_TTL_DAYS * 86400,))


def _backup_db():
    """Ежедневный горячий снимок базы. Бэкапов не было вообще: единственная
    копия создавалась один раз перед dedup-миграцией, и любое повреждение
    файла означало потерю всего прогресса всех игроков.

    Периодичность больше не считается здесь: отметка последнего снимка жила в
    модульной переменной, то есть в памяти процесса, и цикл перезапусков снимал
    полную копию базы на каждом старте. Теперь этим занят scheduler (отметка в
    БД), а тут остаётся сама работа."""
    if settings.DATABASE_URL:
        # sqlite3.backup умеет только SQLite; у Postgres за снимки отвечает
        # pg_dump/провайдер, и делать вид, что бэкап есть, — хуже, чем не делать
        return
    path = db.snapshot(keep=cfg.BACKUP_KEEP)
    if path:
        log.info("бэкап базы: %s", path)


def _prune_ops():
    """TTL токенов идемпотентности: строка с сохранённым ответом пишется на
    каждый клейм и каждую покупку, а нужна ровно на время ретраев."""
    economy.prune_ops(cfg.OPS_TTL_DAYS)


def _prune_boosts():
    """Истёкшие бусты не удалялись никогда, а строка добавляется на каждую
    золотую печеньку. active_boosts читается из click_multiplier на КАЖДЫЙ
    батч кликов — таблица росла бесконечно прямо под самой горячей ручкой."""
    db.exec("DELETE FROM boosts WHERE expires_at < ?", (time.time() - 86400,))


def _rollover_seasons():
    """Ролловер сезона по таймеру, а не из горячего пути запросов.

    finalize_seasons зовётся из четырёх ручек, и в момент смены сезона каждый
    входящий запрос запускал пакетный UPDATE на 500 юзеров — все они дрались
    за один write-lock. Здесь он идёт спокойно и до конца."""
    for _ in range(200):          # 200 x 500 = до 100k юзеров за проход
        left = db.q1("SELECT COUNT(*) c FROM users WHERE season_id < ?",
                     (gl.current_season(),))["c"]
        if not left:
            return
        gl.finalize_seasons()
        if db.q1("SELECT COUNT(*) c FROM users WHERE season_id < ?",
                 (gl.current_season(),))["c"] >= left:
            return                # не двигается — дальше крутиться бессмысленно


# Расписание: (ключ, период, ttl замка, работа). Порядок важен — сначала
# домалываем сезон, потом всё остальное: иначе игрок придёт по уведомлению и
# увидит несброшенный сезонный прогресс.
#
# Чистилки переехали с «каждый тик» на раз в час. Ходят они по всей таблице, а
# смысла удалять только что созданные строки шестнадцать раз за час нет: под
# сотней тысяч игроков это ровно тот фоновый писатель, который мешает горячим
# ручкам. TTL замка — запас на самую долгую работу, а не на среднюю.
JOBS: tuple[tuple[str, float, float, object], ...] = (
    ("season_rollover", CHECK_INTERVAL, 30 * 60, _rollover_seasons),
    ("events_prune", 3600, 15 * 60, _prune_events),
    ("ops_prune", 3600, 15 * 60, _prune_ops),
    ("boosts_prune", 3600, 15 * 60, _prune_boosts),
    ("db_backup", cfg.BACKUP_INTERVAL_H * 3600, 60 * 60, _backup_db),
)

# Пуш-проход отдельно: он async и он самый долгий. 0.05 с на игрока — это час
# на 72 тысячи пушей, поэтому ttl взят с запасом на порядок больше остальных.
NOTIFY_TTL = 4 * 3600

# Проверка webhook'а — тоже async (запрос к Telegram) и только в webhook-режиме.
# Раз в час: адрес сбивается снаружи. Достаточно кому-то поднять копию бота на
# поллинге — delete_webhook снимет боевой, и прод замолчит, не написав в свой
# лог ни строчки. Такое молчание находится только по жалобам игроков.
WEBHOOK_CHECK_INTERVAL = 3600


async def run_notifier(bot):
    """Фоновые задачи процесса. Владелец каждой — один на кластер (scheduler).

    Раньше здесь не было ни владельца, ни расписания: цикл будил все задачи
    каждые 15 минут, и второй процесс просто делал ту же работу второй раз."""
    log.info("планировщик запущен: role=%s bot=%s owner=%s задач=%d",
             settings.ROLE, settings.BOT_MODE, scheduler.OWNER,
             len(JOBS) + (2 if settings.BOT_MODE == "webhook" else 1))
    while True:
        for key, interval, ttl, work in JOBS:
            try:
                with scheduler.job(key, interval, ttl) as mine:
                    if mine:
                        work()
            except Exception:
                log.exception("%s failed", key)
        if settings.BOT_MODE == "webhook":
            try:
                with scheduler.job("webhook_check",
                                   WEBHOOK_CHECK_INTERVAL, 300) as mine:
                    if mine:
                        log.info("webhook: %s",
                                 await webhook.ensure_registered(bot))
            except Exception:
                log.exception("webhook check failed")
        try:
            with scheduler.job("notify_pass", CHECK_INTERVAL, NOTIFY_TTL) as mine:
                if mine:
                    await _notify_pass(bot)
        except Exception:
            log.exception("notifier pass failed")
        await asyncio.sleep(CHECK_INTERVAL)
