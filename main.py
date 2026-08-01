"""Точка входа: FastAPI (API + раздача Mini App) и aiogram-бот.

Что именно поднимает процесс, решают две переменные: ROLE (api / scheduler /
all) и BOT_MODE (polling / webhook). Дефолт остался прежним — всё в одном
процессе на поллинге."""
import asyncio
import logging
import os
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from bot import webhook
from bot.loader import bot, dp
from bot.notifier import run_notifier
from server import settings
from server.economy import ConflictError
from server.routers import game, meta, admin, farm

# Логи: раньше был только basicConfig(WARNING) в stdout, то есть про поломку
# владелец узнавал от игроков. Теперь INFO с ротацией в файл — журнал платежей,
# ролловеров и бэкапов сохраняется между рестартами.
LOG_FILE = settings.LOG_FILE
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler
    _handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=5_000_000,
                                         backupCount=3, encoding="utf-8"))
except OSError:
    pass  # нет прав на запись — работаем только в stdout, но не падаем
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=_handlers,
)
# aiogram/uvicorn на INFO слишком болтливы — оставляем им WARNING
for noisy in ("aiogram.event", "aiogram.dispatcher", "uvicorn.access", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# В проде docs/openapi не нужны: схема выдаёт наружу все ручки, включая
# /api/admin/*, вместе с формой тел запросов — удобная карта для перебора
DEBUG = settings.DEBUG


@asynccontextmanager
async def lifespan(_app):
    """Регистрация webhook'а — после того, как сервер начал слушать.

    Порядок важен: `setWebhook` разрешает Telegram присылать апдейты немедленно,
    и сделай мы это до старта uvicorn — первые апдейты пришли бы в закрытый
    порт. Здесь же процесс уже принимает запросы.

    На выходе webhook НЕ снимаем. Рестарт — это норма (деплой, systemd), а
    снятый webhook означает, что за время перезапуска бот молчит, вместо того
    чтобы получить накопленное сразу после подъёма."""
    if settings.BOT_MODE == "webhook":
        try:
            logging.info("webhook: %s", await webhook.ensure_registered(bot))
        except Exception:
            # Не роняем API: Mini App отдаётся и без бота, а Telegram мы
            # переспросим по расписанию (job webhook_check)
            logging.exception("webhook: зарегистрировать не удалось")
    yield


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
    import time as _time
    from server import cache, scheduler
    from server.game_logic import db
    db.q1("SELECT 1 AS ok")
    return {"ok": True, "ts": _time.time(), "role": settings.ROLE,
            "cache": cache.health(), "scheduler": scheduler.health()}


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
    config = uvicorn.Config(app, host=settings.HOST, port=settings.PORT,
                            log_level="warning")
    await uvicorn.Server(config).serve()


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
    preflight()
    print(f"🚀 Cookie Merge starting (role={settings.ROLE})...")
    # без return_exceptions: падение любой из задач роняет процесс,
    # и systemd (Restart=always) поднимает его заново. Продолжать жить с
    # мёртвым поллингом хуже — снаружи это выглядит как рабочий сервис
    await asyncio.gather(*tasks_for_role())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")
