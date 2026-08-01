"""Приём апдейтов Telegram по webhook'у — тем же процессом, что раздаёт API.

Зачем это нужно. Поллинг физически ведёт ОДИН процесс: `getUpdates` отдаёт
апдейт одному из подписчиков, поэтому второй воркер не удваивает пропускную
способность бота, а начинает воровать апдейты у первого — половина игроков не
получит ответ на /start, а половина платежей не подтвердится. Пока прод был
одним процессом, поллинг был честным выбором; как только воркеров становится
несколько, апдейты обязаны приходить push'ем.

Почему не `aiogram.webhook.aiohttp_server`. Он поднимает отдельное
aiohttp-приложение — второй HTTP-сервер и второй порт под тем же доменом.
FastAPI здесь уже слушает и уже стоит за нужным https-адресом (это тот же
домен, с которого раздаётся Mini App), так что весь webhook — это один
маршрут: разобрать тело в `Update` и отдать диспетчеру.

Защита. Неугадываемый путь — не защита: адрес утекает в логи прокси и в
Referer. Telegram присылает секрет заголовком `X-Telegram-Bot-Api-Secret-Token`,
и мы сверяем его ДО разбора тела, сравнением с постоянным временем. Без этого
любой желающий подделывал бы апдейты — в том числе successful_payment.

Ответ всегда 200, даже когда хендлер упал. Ненулевой код заставляет Telegram
присылать тот же апдейт снова, и на падении в БД мы получили бы не одну ошибку,
а бесконечный поток повторов одного платежа. Повтор здесь безопасен только
благодаря идемпотентности (`economy_ops`), и тратить его на ретраи по кругу
незачем: настоящая причина всё равно уже в логе.
"""
import hmac
import logging

from aiogram.types import Update
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from bot.handlers import payments, start
from bot.loader import bot, dp
from server import settings

log = logging.getLogger(__name__)

_routers_ready = False


def setup_dispatcher():
    """Подключить хендлеры к диспетчеру ровно один раз.

    Раньше это делал `run_bot()` перед поллингом. Теперь точек входа две
    (поллинг и webhook), а повторный `include_router` в aiogram — ошибка,
    поэтому регистрация живёт здесь и идемпотентна."""
    global _routers_ready
    if _routers_ready:
        return
    dp.include_router(start.router)
    dp.include_router(payments.router)
    _routers_ready = True


def url() -> str:
    """Внешний адрес, на который Telegram должен присылать апдейты."""
    base = settings.WEBHOOK_BASE.rstrip("/")
    return base + settings.WEBHOOK_PATH if base else ""


async def handle_update(request: Request) -> Response:
    """Один апдейт от Telegram."""
    secret = settings.WEBHOOK_SECRET
    if not secret:
        # Пустой секрет означал бы, что compare_digest("", "") пропускает
        # ЛЮБОЙ запрос. Лучше не принимать апдейты вовсе, чем принимать чужие.
        log.error("webhook: WEBHOOK_SECRET пуст — апдейты не принимаются")
        return JSONResponse({"detail": "webhook not configured"},
                            status_code=503)
    got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(got, secret):
        # Ни намёка на то, чем именно не подошёл секрет: подбирать по ответам
        # никто не должен. В лог тоже не пишем — по этому пути стучат постоянно.
        return JSONResponse({"detail": "forbidden"}, status_code=403)

    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": bot})
    except Exception as e:
        # Тело не разобралось — присылать его снова смысла нет, поэтому 200.
        # Само тело в лог не кладём: в апдейте лежат личные сообщения игроков.
        log.warning("webhook: апдейт не разобрался: %s", e)
        return JSONResponse({"ok": True})

    try:
        # Ждём хендлер, а не отправляем в фон: ответ на pre_checkout_query
        # Telegram ждёт не дольше 10 секунд, и «приняли, разберёмся позже»
        # означало бы сорванную оплату.
        await dp.feed_update(bot, update)
    except Exception:
        log.exception("webhook: хендлер упал на апдейте %s", update.update_id)
    return JSONResponse({"ok": True})


def install(app) -> bool:
    """Повесить маршрут webhook'а на приложение. True — повесили.

    В режиме поллинга маршрута нет вовсе: открытая ручка, кормящая диспетчер,
    не нужна там, где апдейты и так забираются самим ботом.

    Вызывать ДО монтирования статики в корень: Starlette проверяет маршруты по
    порядку регистрации, а `Mount("/")` совпадает с любым путём."""
    if settings.BOT_MODE != "webhook":
        return False
    setup_dispatcher()
    # include_in_schema=False: в /docs (DEBUG) адрес webhook'а не нужен
    app.add_api_route(settings.WEBHOOK_PATH, handle_update, methods=["POST"],
                      include_in_schema=False)
    log.info("webhook принимает апдейты на %s", settings.WEBHOOK_PATH)
    return True


async def ensure_registered(b=None) -> str:
    """Сказать Telegram, куда присылать апдейты. Возвращает, что сделали.

    Сначала спрашиваем `getWebhookInfo` и молчим, если адрес уже тот: воркеров
    может быть несколько, и каждый стартующий процесс переустанавливал бы
    webhook заново.

    Замком единственного владельца это НЕ обёрнуто намеренно. `setWebhook`
    идемпотентен, так что защищать нечего, а замок здесь fail-closed: при
    недоступном Redis он бы просто не дал зарегистрировать webhook — то есть
    сделал бы бота глухим ровно в тот момент, когда всё и так плохо.

    Зовётся не только на старте, но и по расписанию (`webhook_check`): адрес
    сбивается со стороны, а не только у нас. Достаточно кому-нибудь запустить
    копию бота на поллинге — `delete_webhook` снимет боевой, и прод замолчит
    без единой ошибки в своих логах."""
    b = b or bot
    want = url()
    if not want:
        log.error("webhook: не задан ни WEBHOOK_BASE, ни WEBAPP_URL")
        return "нет адреса"
    info = await b.get_webhook_info()
    if info.url == want:
        return "уже настроен"
    setup_dispatcher()
    await b.set_webhook(
        want,
        secret_token=settings.WEBHOOK_SECRET,
        # Типы апдейтов берём у самого диспетчера, а не списком в коде: список
        # пришлось бы править руками при каждом новом хендлере, и забытая
        # правка ломает его МОЛЧА — Telegram просто не присылает такие апдейты.
        allowed_updates=dp.resolve_used_update_types(),
        # Не сбрасываем накопленное: за время деплоя игрок мог нажать /start
        # или оплатить, и терять это нельзя.
        drop_pending_updates=False,
    )
    log.info("webhook установлен: %s (был %r)", want, info.url or "")
    return "установлен"
