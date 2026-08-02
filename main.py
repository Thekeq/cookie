"""Точка входа: FastAPI (API + раздача Mini App) и aiogram-бот.

Что именно поднимает процесс, решают две переменные: ROLE (api / scheduler /
all) и BOT_MODE (polling / webhook). Дефолт остался прежним — всё в одном
процессе на поллинге."""
import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bot import webhook
from bot.loader import bot, dp
from bot.notifier import run_notifier
from server import obs, settings
from server.economy import ConflictError
from server.routers import game, meta, admin, farm

# Логи: раньше был только basicConfig(WARNING) в stdout, то есть про поломку
# владелец узнавал от игроков. Теперь INFO с ротацией в файл — журнал платежей,
# ролловеров и бэкапов сохраняется между рестартами.
LOG_FILE = settings.LOG_FILE
if settings.WEB_CONCURRENCY > 1:
    # Ротация — это rename + создание нового файла. Когда в один и тот же файл
    # пишут N процессов, каждый ротирует его сам и по своему счётчику: часть
    # воркеров продолжает писать в уже переименованный файл, а cookie.log.1
    # перезаписывается следующим. Своё имя на процесс дороже при чтении, но
    # логи хотя бы не теряются.
    root, ext = os.path.splitext(LOG_FILE)
    LOG_FILE = f"{root}.{os.getpid()}{ext}"
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler
    _handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5_000_000,
                                         backupCount=3, encoding="utf-8"))
except OSError:
    pass  # нет прав на запись — работаем только в stdout, но не падаем
# Идентификатор запроса подставляется в КАЖДУЮ строку — фильтром на handler'е, а
# не полем в вызовах log.info: строки пишут и модули, которые про HTTP не знают.
# В текстовом формате он идёт префиксом [req], в JSON — отдельным полем.
_LOG_FORMAT = ("%(asctime)s %(levelname)s %(name)s [%(req_id)s]: %(message)s"
               if not settings.LOG_JSON else "")
for _h in _handlers:
    _h.addFilter(obs.ContextFilter())
    _h.setFormatter(obs.JsonFormatter() if settings.LOG_JSON
                    else logging.Formatter(_LOG_FORMAT))
logging.basicConfig(level=logging.INFO, handlers=_handlers, force=True)
# aiogram/uvicorn на INFO слишком болтливы — оставляем им WARNING
for noisy in ("aiogram.event", "aiogram.dispatcher", "uvicorn.access", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# В проде docs/openapi не нужны: схема выдаёт наружу все ручки, включая
# /api/admin/*, вместе с формой тел запросов — удобная карта для перебора
DEBUG = settings.DEBUG


METRICS_FLUSH_S = 10.0


async def _metrics_flusher():
    """Досылать метрики в Redis раз в METRICS_FLUSH_S секунд.

    Почему не на каждый запрос: это лишний сетевой вызов в горячем пути ради
    чисел, которые всё равно читают раз в 15 секунд. Почему вообще фоном, а не
    только перед выгрузкой: scrape приходит в ОДИН случайный воркер, и без
    периодической досылки метрики остальных попадали бы в сумму только когда
    очередь дойдёт до них.

    Redis не настроен — задача сама уходит: метрики остаются в памяти, и при
    одном процессе этого достаточно."""
    if not settings.REDIS_URL:
        return
    while True:
        await asyncio.sleep(METRICS_FLUSH_S)
        # to_thread: клиент Redis синхронный, и вызов из цикла событий подвесил
        # бы на время сетевой операции все запросы этого воркера
        await asyncio.to_thread(obs.flush)


@asynccontextmanager
async def lifespan(_app):
    """Регистрация webhook'а — после того, как сервер начал слушать.

    Порядок важен: `setWebhook` разрешает Telegram присылать апдейты немедленно,
    и сделай мы это до старта uvicorn — первые апдейты пришли бы в закрытый
    порт. Здесь же процесс уже принимает запросы.

    На выходе webhook НЕ снимаем. Рестарт — это норма (деплой, systemd), а
    снятый webhook означает, что за время перезапуска бот молчит, вместо того
    чтобы получить накопленное сразу после подъёма."""
    obs.init_sentry()
    flusher = asyncio.create_task(_metrics_flusher())
    if settings.BOT_MODE == "webhook":
        try:
            logging.info("webhook: %s", await webhook.ensure_registered(bot))
        except Exception:
            # Не роняем API: Mini App отдаётся и без бота, а Telegram мы
            # переспросим по расписанию (job webhook_check)
            logging.exception("webhook: зарегистрировать не удалось")
    try:
        yield
    finally:
        # Остановка: сначала снимаем фоновую досылку, потом досылаем сами.
        # Без последнего flush метрики последних METRICS_FLUSH_S секунд перед
        # деплоем терялись бы — а это ровно те секунды, на которые смотрят,
        # когда деплой пошёл не так.
        flusher.cancel()
        try:
            await flusher
        except asyncio.CancelledError:
            pass
        await asyncio.to_thread(obs.flush)
        logging.info("остановка: role=%s pid=%s", settings.ROLE, os.getpid())


app = FastAPI(title="Cookie Merge API",
              docs_url="/docs" if DEBUG else None,
              redoc_url=None,
              openapi_url="/openapi.json" if DEBUG else None,
              lifespan=lifespan)

# Источники ограничиваем WEBAPP_URL: allow_origins=["*"] позволял любому сайту
# дёргать API из браузера жертвы. Сама initData при этом остаётся защитой от
# подделки запроса, но светить API всему интернету незачем.
ALLOWED_ORIGINS = ([settings.WEBAPP_URL]
                   if settings.WEBAPP_URL and not DEBUG else ["*"])
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Lang",
                   "X-User-Revision", "X-Board-Revision", "X-Op-Id"],
)


MAX_BODY_BYTES = 64 * 1024


@app.middleware("http")
async def limit_body_size(request, call_next):
    """Ранний отказ на больших телах: SQLite синхронный и делит процесс с
    ботом, поэтому многомегабайтный JSON парсится в ущерб всем остальным."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "Payload too large"}, status_code=413)
    return await call_next(request)


# Медленным считаем ответ дольше двух секунд: игрок в этот момент смотрит на
# крутилку, и такой запрос обязан оставить след поимённо, а не только в
# гистограмме.
SLOW_REQUEST_S = 2.0


@app.middleware("http")
async def observe_request(request: Request, call_next):
    """Идентификатор запроса, метрики и след для медленных и упавших.

    Регистрируется ПОСЛЕ limit_body_size и потому оказывается снаружи него
    (Starlette кладёт новое middleware в начало цепочки): 413 — такой же ответ,
    как остальные, и в метриках он должен быть виден.

    Метка маршрута берётся из ШАБЛОНА (`/api/user/{uid}`), а не из пути. С
    сырым путём каждая новая ссылка заводила бы отдельный ряд метрик, и на
    сотне тысяч игроков Prometheus сложился бы от кардинальности — это самый
    типичный способ уронить мониторинг собственными руками."""
    req_id = obs.new_request_id(request.headers.get("x-request-id", ""))
    tokens = obs.bind_request(req_id)
    obs.add_gauge("http_requests_in_flight", 1)
    started = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-Id"] = req_id
        return response
    finally:
        took = time.perf_counter() - started
        route = request.scope.get("route")
        # маршрута нет у 404 и у статики — иначе меткой стал бы путь к файлу
        path = getattr(route, "path", None) or "other"
        obs.add_gauge("http_requests_in_flight", -1)
        obs.inc("http_requests_total", method=request.method, path=path,
                status=status)
        obs.observe("http_request_duration_seconds", took,
                    method=request.method, path=path)
        if status >= 500:
            logging.warning("%s %s -> %s за %.3f с", request.method, path,
                            status, took)
        elif took > SLOW_REQUEST_S:
            logging.info("медленный ответ: %s %s -> %s за %.3f с",
                         request.method, path, status, took)
        obs.reset_request(tokens)


@app.exception_handler(ConflictError)
async def state_conflict(request, exc: ConflictError):
    """409 + свежее состояние одним ответом.

    Клиенту нельзя просто сказать «не получилось»: он не знает, что именно
    разошлось, а слепой ретрай отправил бы тот же устаревший ход ещё раз.
    Отдаём состояние прямо здесь — игрок видит обновлённый экран сразу, а не
    после лишнего круга запросов. `detail` держим потому, что весь фронт
    читает ошибки из него."""
    from server import game_logic as gl
    # у ошибки драйвера (см. ниже) user_id нет — тогда состояние не прикладываем
    user_id = getattr(exc, "user_id", None)
    try:
        state = gl.full_state(user_id) if user_id else None
    except Exception:            # игрока могло не быть вовсе — 409 важнее
        logging.exception("full_state failed while building 409 for %s", user_id)
        state = None
    return JSONResponse(status_code=409,
                        content={"detail": "err_state_conflict",
                                 "error": "state_conflict", "state": state})


# На PostgreSQL проигравший в гонке транзакции получает не rowcount 0, а
# SerializationFailure. Для игрока это ровно тот же случай «состояние уехало»,
# и отвечать надо так же 409, а не 500. Модуля здесь пока нет — обработчик
# регистрируется сам, когда появится драйвер.
try:                                                   # pragma: no cover
    from psycopg import errors as _pg_errors

    app.add_exception_handler(_pg_errors.SerializationFailure, state_conflict)
except ImportError:
    pass


app.include_router(meta.router)
app.include_router(game.router)
app.include_router(admin.router)
app.include_router(farm.router)
# webhook — до монтирования статики в корень (Mount("/") совпадает с любым
# путём) и только при BOT_MODE=webhook; сам маршрут живёт в bot/webhook.py
webhook.install(app)


@app.get("/healthz")
async def healthz():
    """Живость процесса + доступность БД и общего состояния.

    Про Redis отвечаем 200 даже когда он лежит: лимитер уходит на фолбэк и игра
    продолжает работать, а вот планировщик — нет. Оркестратору незачем
    перезапускать по такому поводу рабочий процесс, но в ответе это видно, и
    мониторинг может отдельно смотреть на поле cache.

    Планировщик показан отдельным полем потому, что при ROLE=api он живёт в
    ДРУГОМ процессе: у отвечающего на /healthz фоновых задач нет вовсе, и без
    этой строки «бэкапов нет уже неделю» выглядело бы снаружи как здоровье."""
    from server import cache, scheduler
    from server.game_logic import db
    db.q1("SELECT 1 AS ok")
    return {"ok": True, "ts": time.time(), "role": settings.ROLE,
            "cache": cache.health(), "scheduler": scheduler.health()}


@app.get("/livez")
async def livez():
    """Живость: процесс отвечает. Ни базы, ни Redis здесь нет намеренно.

    По этой ручке ПЕРЕЗАПУСКАЮТ. Проверь она базу — упавшая на минуту база
    перезапустила бы разом все воркеры: базу это не чинит, а принятые запросы
    и прогретые соединения теряет. Живость отвечает ровно на один вопрос —
    не завис ли цикл событий."""
    return {"ok": True, "role": settings.ROLE, "pid": os.getpid()}


@app.get("/readyz")
async def readyz():
    """Готовность: можно ли давать этому процессу трафик.

    По этой ручке ВЫВОДЯТ ИЗ БАЛАНСИРОВКИ, поэтому здесь и проверяется то, без
    чего запрос игрока всё равно закончится пятисоткой, — база. Redis в отказ
    не превращается: без него лимитер уходит на фолбэк, а игра работает.

    Про 503: он обязан быть именно кодом, а не полем в теле. Балансировщик
    читает код, и «200 {ok: false}» для него — здоровый процесс."""
    from server import cache
    from server.game_logic import db
    try:
        db.q1("SELECT 1 AS ok")
    except Exception as e:
        logging.warning("readyz: база недоступна: %s", e)
        return JSONResponse(status_code=503,
                            content={"ok": False, "db": f"down: {e}"[:200]})
    return {"ok": True, "role": settings.ROLE, "db": "up",
            "cache": cache.health()}


@app.get("/metrics")
async def metrics(request: Request):
    """Выгрузка для Prometheus. Закрыта токеном, без токена ручки нет вовсе.

    404, а не 401, когда METRICS_TOKEN пуст: отвечать «сюда нужен пароль» —
    значит подтвердить, что здесь есть что смотреть. А смотреть есть что:
    список маршрутов, обороты валюты и состояние фоновых задач.

    Сравнение токена постоянное по времени (compare_digest): обычное ==
    выходит на первом несовпавшем байте, и по времени ответа токен
    подбирается посимвольно."""
    token = settings.METRICS_TOKEN
    if not token:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    sent = request.headers.get("authorization", "")
    sent = sent[7:] if sent.lower().startswith("bearer ") else sent
    if not hmac.compare_digest(sent, token):
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    obs.refresh_gauges()
    return PlainTextResponse(obs.render(),
                             media_type="text/plain; version=0.0.4")


# собранный фронт (webapp/dist) раздаём как статику с корня
DIST = os.path.join(os.path.dirname(__file__), "webapp", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="webapp")


async def run_bot():
    """Поллинг. Поднимается только при BOT_MODE=polling — см. tasks_for_role.

    delete_webhook обязателен: с зарегистрированным webhook'ом Telegram не
    отдаёт апдейты через getUpdates вовсе, и бот молчал бы без единой ошибки.
    Обратная сторона того же — запуск локальной копии на поллинге снимает
    БОЕВОЙ webhook, поэтому в webhook-режиме есть задача, которая его
    возвращает (webhook_check)."""
    webhook.setup_dispatcher()
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


async def run_api():
    """HTTP-сервер этого процесса.

    Один воркер — обычный asyncio-сервер рядом с остальными задачами процесса.
    Несколько воркеров — это уже не задача, а мастер-процесс (см. serve_api_
    workers), и сюда управление не доходит: main() разводит эти два случая до
    создания цикла событий.

    timeout_graceful_shutdown: по SIGTERM сервер перестаёт принимать новые
    соединения, но уже принятым даёт доработать. Без этого деплой обрывал бы
    запросы на середине — а половина из них денежные, и игрок увидел бы не
    ошибку сети, а пропавшую награду."""
    config = uvicorn.Config(app, host=settings.HOST, port=settings.PORT,
                            log_level="warning",
                            timeout_graceful_shutdown=settings.GRACEFUL_TIMEOUT)
    await uvicorn.Server(config).serve()


def serve_api_workers():
    """Мастер-процесс: N воркеров uvicorn на ОДНОМ слушающем сокете.

    Сокет открывает мастер и передаёт детям, поэтому порт занят один раз, а
    ядро само раскладывает соединения по воркерам — ни балансировщика, ни
    отдельных портов не нужно.

    Приложение передаётся СТРОКОЙ "main:app", а не объектом: дети создаются
    через fork/spawn и на Windows (spawn) объект приложения не переживает
    сериализацию. Строку каждый ребёнок импортирует у себя сам.

    Функция блокирующая и своего цикла событий не заводит — asyncio живёт
    внутри каждого ребёнка. Фоновых задач здесь нет вовсе: их несёт отдельный
    процесс с ROLE=scheduler, и settings.problems() не даст запуститься с
    несколькими воркерами в любой другой роли."""
    from uvicorn.supervisors import Multiprocess

    config = uvicorn.Config("main:app", host=settings.HOST, port=settings.PORT,
                            workers=settings.WEB_CONCURRENCY,
                            log_level="warning",
                            timeout_graceful_shutdown=settings.GRACEFUL_TIMEOUT)
    sock = config.bind_socket()
    try:
        Multiprocess(config, sockets=[sock]).run()
    finally:
        sock.close()


def preflight():
    """Проверка окружения ДО старта: молчаливо кривой конфиг обходится дороже
    падения. Раньше отсутствующий BOT_TOKEN превращался в 500 на каждый
    запрос, а незаданный ADMIN_ID молча закрывал админку от самого владельца.

    Сами правила живут в server.settings.problems() — там же, где значения.
    Здесь остаётся только то, что про этот конкретный процесс: собранный фронт
    он раздаёт сам, и без webapp/dist игрок увидит пустую страницу."""
    found = settings.problems()
    if not os.path.isdir(DIST):
        found.append((f"нет собранного фронта в {DIST} — "
                      f"выполни: cd webapp && npm run build", False))
    for text, _ in found:
        logging.error("КОНФИГ: %s", text)
    fatal = [text for text, is_fatal in found if is_fatal]
    if fatal:
        raise SystemExit("Старт отменён:\n  " + "\n  ".join(fatal))
    logging.info("%s", settings.summary())


def tasks_for_role() -> list:
    """Что именно поднимает ЭТОТ процесс.

    ROLE=all — как было: бот, API и фоновые задачи в одном процессе. Разделение
    нужно, когда воркеров становится несколько: API масштабируется копиями, а
    планировщик — нет, ему нужен ровно один процесс (владельца задач всё равно
    сторожит scheduler, но платить N процессами за одну работу незачем).

    Поллинг тянет только ROLE=all: физически его может вести один процесс, и
    settings.problems() отказывается стартовать с ROLE=api/scheduler на
    поллинге, чтобы апдейты не остались без читателя молча.

    В webhook-режиме отдельной задачи под бота нет: апдейты приходят обычными
    HTTP-запросами в run_api. Раньше run_bot поднимался всегда и внутри всё
    равно вызывал delete_webhook — то есть BOT_MODE=webhook в конфиге ничего не
    менял, кроме проверок при старте."""
    jobs = []
    if settings.ROLE in ("all", "api"):
        jobs.append(run_api())
    if settings.ROLE == "all" and settings.BOT_MODE == "polling":
        jobs.append(run_bot())
    if settings.ROLE in ("all", "scheduler"):
        jobs.append(run_notifier(bot))
    return jobs


async def main():
    print(f"🚀 Cookie Merge starting (role={settings.ROLE})...")
    # без return_exceptions: падение любой из задач роняет процесс,
    # и systemd (Restart=always) поднимает его заново. Продолжать жить с
    # мёртвым поллингом хуже — снаружи это выглядит как рабочий сервис
    await asyncio.gather(*tasks_for_role())


def run():
    """Развилка «один процесс» / «мастер с воркерами».

    Она здесь, а не внутри main(), потому что мастер-процесс НЕ асинхронный:
    Multiprocess вешает обработчики сигналов и ждёт детей в главном потоке, и
    заворачивать его в задачу asyncio значило бы ставить блокирующий цикл
    рядом с событийным."""
    preflight()
    if settings.WEB_CONCURRENCY > 1:
        print(f"🚀 Cookie Merge starting (role={settings.ROLE}, "
              f"воркеров {settings.WEB_CONCURRENCY})...")
        serve_api_workers()
        return
    asyncio.run(main())


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("Stopped")
