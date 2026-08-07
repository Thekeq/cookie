"""Фоновые задачи процесса-планировщика и ОТПРАВКА пушей из очереди.

Уведомления разделены на две половины, и это главное, что здесь происходит:

  * планировщик (`server.notifications.plan_pass`, задача `notify_plan`) только
    КЛАДЁТ задачи в `notification_queue`. Он не ходит в Telegram вовсе, поэтому
    его проход занимает секунды при любом размере базы;
  * воркер (`run_sender` ниже) разбирает очередь партиями и шлёт. Воркеров
    может быть сколько угодно: партию забирает один атомарный UPDATE.

Раньше это была одна функция `_notify_pass`: полный проход по таблице игроков
раз в 15 минут с отправкой прямо внутри. На 100 тысячах пушей при 25 msg/s она
не укладывалась в собственный интервал, а всё, до чего не дошла, теряла молча.
Она осталась ниже как аварийный путь (`NOTIFY_QUEUE=0`) — на случай, если с
очередью что-то не так, а писать игрокам надо прямо сейчас.

Правила, чтобы не превратиться в спам и не ловить блокировки, теперь живут в
одном месте (server/notifications.py): тихие часы по поясу игрока, частотные
лимиты общий и по категории, проверка актуальности события перед самой
отправкой. Заблокировавших бота помечаем notify_blocked и больше не трогаем.
"""
import asyncio
import logging
import time

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bot import webhook
from server import backup
from server import economy
from server import game_config as cfg
from server import game_logic as gl
from server import notifications as notif
from server import obs
from server import scheduler
from server import settings
from server.game_logic import db
from server.i18n import tr

CHECK_INTERVAL = 15 * 60  # проверяем всех раз в 15 минут
SENDER_IDLE_S = 20        # как часто воркер заглядывает в пустую очередь

# Аварийный выключатель очереди: с NOTIFY_QUEUE=0 процесс возвращается к
# старому прямому проходу. Живёт в settings вместе с остальным окружением —
# читать env отсюда напрасно: settings разбирается при импорте, и перезапуск
# процесса перечитывает его ровно так же, как перечитал бы os.environ.
NOTIFY_QUEUE = settings.NOTIFY_QUEUE

log = logging.getLogger(__name__)


def _pick_notification(user: dict, now: float) -> str | None:
    """Возвращает текст пуша или None. Приоритет: стрик > ферма > энергия.

    Сами правила переехали в server.notifications: их спрашивает и планировщик
    (кого ставить в очередь), и воркер (актуально ли это ещё в момент
    отправки). Две копии одного правила означали бы сообщения, которые
    отменяют сами себя."""
    lang = user.get("lang") or "en"
    if notif.streak_due(user, now):
        return tr(lang, "notif_streak", days=user["daily_streak"])
    if notif.farm_idle(user, now):
        return tr(lang, "notif_farm")
    if notif.energy_full(user, now):
        return tr(lang, "notif_energy")
    return None


async def _notify_pass(bot):
    """Прямой проход по игрокам БЕЗ очереди — аварийный путь (NOTIFY_QUEUE=0).

    Здесь нет ни тихих часов, ни частотных лимитов, ни повторов: это ровно то,
    чем уведомления были до очереди, и годится оно только как разовая замена."""
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
            obs.inc("notifications_total", result="sent")
        except TelegramForbiddenError:
            db.exec("UPDATE users SET notify_blocked = 1 WHERE user_id = ?",
                    (user["user_id"],))
            obs.inc("notifications_total", result="blocked")
        except Exception as e:
            obs.inc("notifications_total", result="failed")
            log.warning("notify %s failed: %s", user["user_id"], e)
        await asyncio.sleep(0.05)


def _plan_notifications():
    """Задача планировщика: разложить события по очереди. Ни одной отправки."""
    notif.plan_pass()


def _prune_notifications():
    notif.prune()


def _deep_link_kb(kind: str, notification_id: int, lang: str):
    """Кнопка сразу на нужную вкладку.

    Пуш без кнопки — это просьба «зайди и найди сам»: игрок открывает игру на
    кликере, а сообщение было про заказ в пекарне. Диплинк приносит вкладку
    (и сегмент) плюс номер уведомления — по нему /auth отмечает открытие,
    и только это превращает пуши из догадок в измеримый канал."""
    mode, url = notif.deep_link(kind, notification_id)
    if not mode:
        return None
    text = notif.button_text(kind, lang)
    btn = (InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))
           if mode == "webapp" else InlineKeyboardButton(text=text, url=url))
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


async def _send_queued(bot, row) -> bool:
    """Одна задача из очереди. True — сообщение действительно ушло.

    Порядок проверок здесь не случаен: сначала дешёвые и локальные (жив ли
    игрок, не спит ли он, не перебрали ли лимит), и только потом та, которая
    ходит в базу за состоянием события. Отложенное сообщение возвращается в
    очередь со своим новым временем — событие никуда не делось."""
    now = time.time()
    user = db.get_user(row["user_id"])
    if not user or user.get("notify_blocked"):
        notif.cancel(row, "no user or blocked")
        return False

    quiet = notif.quiet_until(user, now)
    if quiet:
        notif.defer(row, quiet, "quiet_hours")
        return False
    delay = notif.cap_delay(row["user_id"], row["category"], now)
    if delay:
        notif.defer(row, now + delay, "freq_cap")
        return False
    if not notif.still_relevant(row, user, now):
        notif.cancel(row, "stale")
        return False

    lang = user.get("lang") or "en"
    text = notif.render(row["kind"], lang, notif.payload_of(row))
    try:
        await bot.send_message(row["user_id"], text,
                               reply_markup=_deep_link_kb(row["kind"], row["id"],
                                                          lang))
    except TelegramForbiddenError:
        notif.mark_blocked(row)
        return False
    except TelegramRetryAfter as e:
        # это не ошибка отправки, а условие: до истечения retry_after ответ
        # будет ровно таким же. Ждём и мы сами — иначе следующая строка партии
        # получит тот же отказ и сожжёт ещё одну попытку
        notif.mark_failed(row, f"retry_after {e.retry_after}",
                          retry_after=e.retry_after)
        await asyncio.sleep(min(float(e.retry_after), 60.0))
        return False
    except Exception as e:                          # noqa: BLE001
        notif.mark_failed(row, f"{type(e).__name__}: {e}")
        log.warning("пуш %s игроку %s не ушёл: %s", row["kind"],
                    row["user_id"], e)
        return False
    notif.mark_sent(row, now)
    return True


async def _send_pass(bot) -> int:
    """Одна партия из очереди. Возвращает, сколько сообщений реально ушло."""
    rows = notif.claim(notif.BATCH)
    sent = 0
    for row in rows:
        try:
            ok = await _send_queued(bot, row)
        except Exception:                           # noqa: BLE001
            # падение на одной строке не имеет права остановить партию: раньше
            # одно исключение в середине прохода оставляло без пушей всех, кто
            # шёл после него
            log.exception("пуш %s не обработан", row.get("id"))
            continue
        if ok:
            sent += 1
            await asyncio.sleep(1.0 / notif.RATE_PER_SEC)
    return sent


async def run_sender(bot):
    """Воркер отправки: крутится непрерывно, а не раз в 15 минут.

    Интервал у отправки не нужен вовсе — нужен предел скорости. Пустую очередь
    опрашиваем раз в SENDER_IDLE_S, непустую разбираем без пауз: пока в ней
    что-то есть, единственный ограничитель — RATE_PER_SEC."""
    log.info("воркер пушей запущен: партия=%d, %.0f msg/s",
             notif.BATCH, notif.RATE_PER_SEC)
    while True:
        try:
            sent = await _send_pass(bot)
        except Exception:                           # noqa: BLE001
            log.exception("партия пушей провалилась")
            sent = 0
        await asyncio.sleep(0.5 if sent else SENDER_IDLE_S)


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
    БД), а тут остаётся сама работа.

    Движок здесь больше не проверяется: снимок умеет оба (sqlite backup API и
    pg_dump), выбор делает db.snapshot.

    Сама работа уехала в server/backup.py: снимок — только первый шаг, за ним
    контрольная сумма, шифрование и отправка с машины. Копия, лежащая на том
    же диске, что и база, переживает лишь ошибочный запрос, а не потерю диска."""
    try:
        info = backup.run()
    except backup.BackupError as e:
        obs.inc("backup_total", result="fail")
        # наружу пробрасываем: scheduler посчитает падение, и это увидит
        # алерт по job_fails_total{job="db_backup"}
        raise RuntimeError(f"бэкап не сделан: {e}") from e
    log.info("бэкап базы: %s (%.1f МБ, шифрование=%s, отправлен=%s, %s с)",
             info["path"], info["size"] / 1048576, info["encrypted"],
             info["uploaded"], info["seconds"])


def _backup_drill():
    """Проверка восстановлением: раз в сутки поднять последний снимок.

    Бэкап, который ни разу не разворачивали, — это предположение. Ломается всё
    молча: файл побился на диске, ключ шифрования в конфиге сменили, pg_dump
    записал пустышку. Каждая из этих поломок обнаруживается либо здесь, либо в
    час аварии."""
    try:
        info = backup.drill()
    except backup.BackupError as e:
        obs.inc("backup_drill_total", result="fail")
        raise RuntimeError(f"учения провалены: {e}") from e
    log.info("учения по восстановлению: %s", info)


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
    # Планировщик пушей — обычная синхронная задача, потому что он и есть
    # обычная задача: пачка запросов и пачка INSERT'ов. В Telegram он не ходит,
    # поэтому ttl замка ему нужен небольшой, в отличие от старого прохода.
    ("notify_plan", CHECK_INTERVAL, 10 * 60, _plan_notifications),
    ("notify_prune", 3600, 15 * 60, _prune_notifications),
    ("events_prune", 3600, 15 * 60, _prune_events),
    ("ops_prune", 3600, 15 * 60, _prune_ops),
    ("boosts_prune", 3600, 15 * 60, _prune_boosts),
    ("db_backup", cfg.BACKUP_INTERVAL_H * 3600, 60 * 60, _backup_db),
)

# Учения идут отдельным ключом, а не внутри бэкапа: у них своя периодичность и
# свой смысл отказа. «Снимок сделан» и «снимок разворачивается» — разные
# новости, и алерт должен уметь отличить одну от другой.
if settings.BACKUP_DRILL_INTERVAL_H > 0:
    JOBS += (("backup_drill", settings.BACKUP_DRILL_INTERVAL_H * 3600,
              30 * 60, _backup_drill),)

# Аварийный прямой проход — единственная async-задача в расписании и самая
# долгая: 0.05 с на игрока это час на 72 тысячи пушей, поэтому ttl взят с
# запасом на порядок больше остальных. С включённой очередью он не запускается
# вовсе: там нет ни владельца, ни интервала — есть воркер и предел скорости.
NOTIFY_TTL = 4 * 3600

# Проверка webhook'а — тоже async (запрос к Telegram) и только в webhook-режиме.
# Раз в час: адрес сбивается снаружи. Достаточно кому-то поднять копию бота на
# поллинге — delete_webhook снимет боевой, и прод замолчит, не написав в свой
# лог ни строчки. Такое молчание находится только по жалобам игроков.
WEBHOOK_CHECK_INTERVAL = 3600


async def run_notifier(bot):
    """Фоновые задачи процесса. Владелец каждой — один на кластер (scheduler).

    Раньше здесь не было ни владельца, ни расписания: цикл будил все задачи
    каждые 15 минут, и второй процесс просто делал ту же работу второй раз.

    Воркер отправки — единственное исключение из этого правила, и намеренное:
    владельца у него НЕТ. Замок ограничил бы отправку одним процессом, то есть
    держал бы её на 25 msg/s навсегда; очередь же безопасна к любому числу
    воркеров — строку забирает один UPDATE. Растёт очередь — добавляют
    процессы, а не ждут."""
    log.info("планировщик запущен: role=%s bot=%s owner=%s задач=%d очередь=%s",
             settings.ROLE, settings.BOT_MODE, scheduler.OWNER,
             len(JOBS) + (2 if settings.BOT_MODE == "webhook" else 1),
             "да" if NOTIFY_QUEUE else "нет (прямой проход)")
    sender = asyncio.create_task(run_sender(bot)) if NOTIFY_QUEUE else None
    try:
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
            if not NOTIFY_QUEUE:
                try:
                    with scheduler.job("notify_pass", CHECK_INTERVAL,
                                       NOTIFY_TTL) as mine:
                        if mine:
                            await _notify_pass(bot)
                except Exception:
                    log.exception("notifier pass failed")
            # Метрики этого процесса — в общую копилку. Своего /metrics у него
            # нет (HTTP он не поднимает вовсе), и без досылки пуши и бэкапы не
            # были бы видны в мониторинге ни одной цифрой.
            await asyncio.to_thread(obs.flush)
            await asyncio.sleep(CHECK_INTERVAL)
    finally:
        if sender is not None:
            sender.cancel()
