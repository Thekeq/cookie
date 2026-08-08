"""Точка входа: FastAPI (API + раздача Mini App) и aiogram-бот.

Что именно поднимает процесс, решают две переменные: ROLE (api / scheduler /
all) и BOT_MODE (polling / webhook). Дефолт остался прежним — всё в одном
процессе на поллинге."""
import asyncio
import hmac
import logging
import mimetypes
import os
import socket
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from bot import webhook
from bot.loader import bot, dp
from bot.notifier import run_notifier
from server import auth, obs, settings
from server.deps import rate_limit
from server.economy import ConflictError
from server.routers import game, meta, admin, farm, legal

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
              # Лимитер вешается на приложение, а не на отдельные ручки: так
              # новую ручку физически нельзя забыть прикрыть. Точечные лимиты
              # внутри роутеров остаются — они знают цену конкретной работы.
              dependencies=[Depends(rate_limit)],
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
    # X-Request-Id обязателен в списке: клиент ставит его на КАЖДЫЙ запрос
    # (webapp/src/net.ts), а заголовок, которого нет в allow_headers, роняет
    # предзапрос целиком — не сам заголовок, а весь запрос вместе с ним.
    allow_headers=["Authorization", "Content-Type", "X-Lang", "X-Request-Id",
                   "X-User-Revision", "X-Board-Revision", "X-Op-Id",
                   # платформа Mini App: единственный источник «ios/android/
                   # tdesktop/web», сервер её ниоткуда больше не узнаёт
                   "X-Tg-Platform"],
    # Без expose_headers браузер не отдаёт эти заголовки коду Mini App вовсе:
    # серверная сессия приезжает в ответе, и прочитать её должно быть можно.
    expose_headers=["X-Request-Id", auth.SESSION_HEADER,
                    auth.SESSION_EXPIRES_HEADER],
)


MAX_BODY_BYTES = 64 * 1024
# методы без тела не трогаем вовсе: лишний цикл чтения на каждом GET — это
# накладные расходы на самом частом запросе игры
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    """Отказ на большом теле — по ФАКТИЧЕСКИ ПРОЧИТАННЫМ байтам.

    Раньше здесь смотрели только заголовок Content-Length, и обходилось это
    одной строчкой: `Transfer-Encoding: chunked` (а в HTTP/2 длина не
    обязательна вовсе) — заголовка нет, проверять нечего, тело любого размера
    доезжало до парсера JSON. SQLite синхронный и делит процесс с ботом,
    поэтому такой запрос — это не «много памяти у одного», а остановка игры
    для всех, кто в этот момент нажимает на печеньку.

    Поэтому тело читается здесь и целиком (64 КБ — не та величина, ради
    которой стоит городить потоковую обработку), а дальше отдаётся приложению
    уже готовым куском. Ровно то же самое делает Starlette при `await
    request.json()`, только без верхней границы.

    Обычным `@app.middleware("http")` это не сделать: BaseHTTPMiddleware не
    даёт подменить `receive`, а бросать исключение из середины чтения поздно —
    ответ к тому моменту уже начат, и наружу вместо 413 уходит 500.
    """

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in _BODY_METHODS:
            return await self.app(scope, receive, send)

        # Content-Length, если он есть, отбиваем ДО чтения: незачем принимать
        # мегабайты, про которые отправитель сам сказал, что их мегабайты
        for name, value in scope.get("headers", ()):
            if name == b"content-length" and value.isdigit():
                if int(value) > self.max_bytes:
                    return await self._too_large(scope, receive, send)
                break

        body, more = b"", True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                # клиент отвалился на середине — отвечать некому
                return await self.app(scope, _replay(body, closed=True), send)
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                return await self._too_large(scope, receive, send)
            more = message.get("more_body", False)
        return await self.app(scope, _replay(body), send)

    async def _too_large(self, scope, receive, send):
        response = JSONResponse({"detail": "Payload too large"}, status_code=413)
        await response(scope, receive, send)


def _replay(body: bytes, closed: bool = False):
    """Уже прочитанное тело в виде обычного ASGI-receive для приложения."""
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)

# Host из запроса попадает в ссылки и в редиректы, а до приложения он доезжает
# ровно таким, каким его прислал клиент. Список хостов закрывает подстановку
# чужого домена; подключаем ТОЛЬКО когда список непустой — middleware с ["*"]
# выглядит как защита и ничего не проверяет.
if settings.ALLOWED_HOSTS:
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=settings.ALLOWED_HOSTS)


# Content-Security-Policy собирается из того, что РЕАЛЬНО грузит собранный
# webapp/dist, а не из общих соображений: политика, которая ломает приложение,
# живёт до первой жалобы и снимается целиком.
#   script-src   — свои бандлы (/assets/*.js) и telegram-web-app.js. Инлайновых
#                  скриптов в сборке нет, поэтому 'unsafe-inline' здесь не
#                  нужен — а это главное, ради чего CSP вообще ставят.
#   style-src    — 'unsafe-inline': в коде 172 места со style={{...}}, а
#                  инлайновый АТРИБУТ стиля CSP режет так же, как тег <style>.
#                  Убрать это можно только переписав вёрстку. Доменов Google
#                  здесь больше нет: шрифты переехали в webapp/public/fonts,
#                  и разрешение, которое ничему не нужно, — это просто дырка.
#   frame-ancestors — Telegram открывает Mini App в iframe со своего домена.
#                  По той же причине здесь нет X-Frame-Options: DENY — он
#                  убил бы веб-версию Telegram целиком.
CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' https://telegram.org",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "img-src 'self' data: blob:",
    "media-src 'self'",
    "connect-src 'self'",
    "frame-ancestors https://telegram.org https://*.telegram.org",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'",
))
# 180 дней: год ставят, когда домен точно останется за приложением навсегда,
# и ошибка здесь не откатывается — браузер помнит запрет ходить по http
# столько, сколько ему сказали, независимо от того, что мы отдадим потом.
HSTS = "max-age=15552000; includeSubDomains"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Заголовки безопасности на КАЖДЫЙ ответ, включая ошибки и статику.

    Единственное место, где они ставятся: перед приложением стоит Cloudflare, а
    не свой веб-сервер, и своего конфига, куда их можно было бы положить, нет
    вовсе. Отсюда и место в цепочке: регистрируется ПОСЛЕ CORS, лимита тела и
    TrustedHost и потому оказывается снаружи них (Starlette кладёт новое
    middleware в начало цепочки). Заголовки получают в том числе 400 от
    TrustedHost, 413 от лимита тела и предзапросы CORS — иначе голым уходил бы
    ровно тот ответ, который сгенерирован не роутером."""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Content-Security-Policy", CSP)
    # HSTS имеет смысл только на https и только там его и ставим: на локальном
    # http браузер его игнорирует, а вот забытый на дев-домене max-age
    # запоминается всерьёз. Заголовок ставит Cloudflare (сам процесс слушает
    # обычный http); подделать его может и клиент, но HSTS, приехавший по
    # незащищённому соединению, браузер обязан выбросить — подделка бесполезна.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        response.headers.setdefault("Strict-Transport-Security", HSTS)
    return response


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
    # Платформа приходит от Mini App (Telegram.WebApp.platform), страна — от
    # обратного прокси/CDN: сам сервер её не знает, а geoip по IP в приложении
    # означал бы хранить IP, чего политика приватности не обещает. Обе метки
    # уезжают в аналитику через контекст запроса; чужому значению здесь верят
    # ровно настолько, насколько его чистит obs.clean_client_tag.
    tokens = obs.bind_request(
        req_id,
        platform=request.headers.get("x-tg-platform", ""),
        country=(request.headers.get("cf-ipcountry")
                 or request.headers.get("x-country", "")))
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
# admin_audit — журнал административного доступа. Вешается здесь, а не в
# admin.py, чтобы новую админскую ручку нельзя было завести мимо журнала
app.include_router(admin.router, dependencies=[Depends(legal.admin_audit)])
app.include_router(farm.router)
# /privacy, /terms, экспорт и удаление данных — ДО монтирования статики в корень
app.include_router(legal.router)
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
    from server import backup, cache, scheduler
    from server.game_logic import db
    db.q1("SELECT 1 AS ok")
    return {"ok": True, "ts": time.time(), "role": settings.ROLE,
            "cache": cache.health(), "scheduler": scheduler.health(),
            "backup": backup.status()}


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


# Кеш на год + immutable. Безопасен ровно потому, что имя файла содержит хеш
# содержимого (assets/, см. webapp/vite.config.ts) или версию семейства
# (fonts/rubik-v31-…): изменилось содержимое — изменился URL, и старый ответ в
# кеше никому не мешает. Файлу со СТАБИЛЬНЫМ именем такое ставить нельзя —
# отозвать год из чужого браузера нечем.
IMMUTABLE = "public, max-age=31536000, immutable"
_IMMUTABLE_DIRS = frozenset({"assets", "fonts"})

# Тип содержимого для шрифтов: таблицу mime брал веб-сервер из своей mime.types,
# а StaticFiles берёт из mimetypes, и там woff2 есть не везде (на Windows тип
# приходит из реестра и оказывается text/plain). Вместе с nosniff это ровно тот
# случай, ради которого nosniff и ставят, — поэтому тип задаём явно.
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("font/woff", ".woff")


class MiniAppStatic(StaticFiles):
    """Собранный фронт + политика кеша на него.

    Раньше статику раздавал веб-сервер перед приложением, и Cache-Control
    ставил он. Его нет: игрок ходит через Cloudflare прямо в uvicorn, то есть
    файлы отдаёт вот этот класс. Без заголовка браузер решает срок кеша сам, по
    догадке из Last-Modified, и хешированный бандл, который не изменится
    никогда, перекачивается заново.

    index.html — наоборот, `no-cache`: в нём лежат ссылки на хешированные
    бандлы. Закешированный старый index указывает на файлы, которых после
    выкладки уже нет, и это не «устаревшая версия», а белый экран. no-cache не
    запрещает хранить — он требует переспросить, и ETag превращает переспрос в
    304 без тела.

    Остальное (sfx/*.mp3) остаётся как есть: имена стабильные, содержимое тоже,
    и год кеша тут был бы обещанием, которое нечем взять назад."""

    def file_response(self, full_path, *args, **kwargs):
        response = super().file_response(full_path, *args, **kwargs)
        name = os.path.basename(str(full_path))
        if os.path.basename(os.path.dirname(str(full_path))) in _IMMUTABLE_DIRS:
            response.headers["Cache-Control"] = IMMUTABLE
        elif name.endswith(".html"):
            response.headers["Cache-Control"] = "no-cache"
        return response


# собранный фронт (webapp/dist) раздаём как статику с корня
DIST = os.path.join(os.path.dirname(__file__), "webapp", "dist")
if os.path.isdir(DIST):
    app.mount("/", MiniAppStatic(directory=DIST, html=True), name="webapp")


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


def inherited_socket() -> socket.socket | None:
    """Слушающий сокет, открытый systemd, а не нами (socket activation).

    Зачем. Порт держит юнит cookie-api.socket, и он переживает рестарт
    сервиса: соединения, пришедшие в окно деплоя, копятся в очереди ядра, а не
    получают отказ. Игрок ждёт лишние пару секунд вместо 502 — своей страницы
    «идут работы» показать всё равно некому, веб-сервера перед uvicorn в
    боевой схеме нет, а Cloudflare отдаёт свою.

    LISTEN_PID сверяется со своим pid намеренно: переменные наследуются
    потомками, и без проверки чужой процесс принял бы за свой посторонний
    дескриптор. После чтения переменные убираются — так же делает
    sd_listen_fds.

    Первый переданный дескриптор всегда 3 (SD_LISTEN_FDS_START). Семейство и
    тип берутся из самого дескриптора: назови мы их сами и ошибись — адрес
    разбирался бы не тем разбором, а не упал бы честно.

    Без systemd (локальный запуск, Windows) возвращается None, и порт
    открывается как раньше, по HOST/PORT."""
    if os.environ.pop("LISTEN_PID", None) != str(os.getpid()):
        return None
    count = int(os.environ.pop("LISTEN_FDS", "0") or 0)
    if count != 1:
        raise SystemExit(f"systemd передал сокетов: {count}, ожидался один")
    sock = socket.socket(fileno=3)
    # воркерам сокет достаётся через fork/spawn — иначе дети остались бы без него
    sock.set_inheritable(True)
    check_listen_address(sock)
    logging.info("Слушающий сокет получен от systemd: %s", sock.getsockname())
    return sock


def check_listen_address(sock: socket.socket) -> None:
    """Сверка того, что слушает systemd, с тем, что записано в .env.

    Под socket activation адрес задан в ListenStream, а HOST/PORT из .env на
    него больше не влияют — и это ловушка. `settings.problems()` ругается на
    TRUSTED_PROXY=1 при HOST=0.0.0.0, но с сокет-юнитом можно написать в .env
    HOST=127.0.0.1, оставить в юните 0.0.0.0 и получить открытый наружу порт
    при полной тишине проверок: с доверием к CF-Connecting-IP это значит, что
    лимит по IP выбирает себе сам отправитель.

    Порт сверяется отдельно: по settings.PORT ходят проверки состояния и
    канарейка, и разъехавшийся порт выглядит как «сервис лежит».

    Это предупреждения, а не отказ, ровно по той же причине, что и в
    settings.problems(): закрыт ли порт файрволом, из процесса не видно, а
    ронять единственную рабочую конфигурацию по догадке нельзя."""
    try:
        host, port = sock.getsockname()[:2]
    except (OSError, ValueError):  # не IP-сокет (unix) — сверять нечего
        return
    if port != settings.PORT:
        logging.warning("КОНФИГ: systemd слушает порт %s, а в .env PORT=%s — "
                        "проверки состояния и канарейка ходят по PORT",
                        port, settings.PORT)
    loopback = host in ("127.0.0.1", "::1")
    if settings.TRUSTED_PROXY and not loopback:
        logging.warning("КОНФИГ: systemd слушает %s при TRUSTED_PROXY=1 — если "
                        "порт доступен мимо Cloudflare, любой подделает себе "
                        "IP заголовком и обойдёт лимит; в ListenStream нужен "
                        "127.0.0.1 либо закрытый файрволом порт", host)


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
    sock = inherited_socket()
    await uvicorn.Server(config).serve(sockets=[sock] if sock else None)


def serve_api_workers():
    """Мастер-процесс: N воркеров uvicorn на ОДНОМ слушающем сокете.

    Сокет открывает мастер и передаёт детям, поэтому порт занят один раз, а
    ядро само раскладывает соединения по воркерам — ни балансировщика, ни
    отдельных портов не нужно. Под systemd сокет открыт ещё раньше, самим
    systemd, и мастер только принимает его (см. inherited_socket).

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
    sock = inherited_socket() or config.bind_socket()
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
