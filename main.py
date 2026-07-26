"""Один процесс: FastAPI (API + раздача Mini App) + aiogram-бот на polling."""
import asyncio
import logging
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from bot.loader import bot, dp
from bot.handlers import start, payments
from bot.notifier import run_notifier
from server.routers import game, meta, admin, farm

# Логи: раньше был только basicConfig(WARNING) в stdout, то есть про поломку
# владелец узнавал от игроков. Теперь INFO с ротацией в файл — журнал платежей,
# ролловеров и бэкапов сохраняется между рестартами.
LOG_FILE = os.getenv("LOG_FILE", "cookie.log")
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
DEBUG = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
app = FastAPI(title="Cookie Merge API",
              docs_url="/docs" if DEBUG else None,
              redoc_url=None,
              openapi_url="/openapi.json" if DEBUG else None)

# Источники ограничиваем WEBAPP_URL: allow_origins=["*"] позволял любому сайту
# дёргать API из браузера жертвы. Сама initData при этом остаётся защитой от
# подделки запроса, но светить API всему интернету незачем.
_origin = os.getenv("WEBAPP_URL", "").rstrip("/")
ALLOWED_ORIGINS = [_origin] if _origin and not DEBUG else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Lang"],
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


app.include_router(meta.router)
app.include_router(game.router)
app.include_router(admin.router)
app.include_router(farm.router)


@app.get("/healthz")
async def healthz():
    """Живость процесса + доступность БД (для мониторинга/оркестрации)."""
    import time as _time
    from server.game_logic import db
    db.q1("SELECT 1 AS ok")
    return {"ok": True, "ts": _time.time()}


# собранный фронт (webapp/dist) раздаём как статику с корня
DIST = os.path.join(os.path.dirname(__file__), "webapp", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="webapp")


async def run_bot():
    dp.include_router(start.router)
    dp.include_router(payments.router)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot)


async def run_api():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")),
                            log_level="warning")
    await uvicorn.Server(config).serve()


def preflight():
    """Проверка окружения ДО старта: молчаливо кривой конфиг обходится дороже
    падения. Раньше отсутствующий BOT_TOKEN превращался в 500 на каждый
    запрос, а незаданный ADMIN_ID молча закрывал админку от самого владельца."""
    problems = []
    if not os.getenv("BOT_TOKEN"):
        problems.append("BOT_TOKEN не задан — подпись initData проверить нечем")
    if not int(os.getenv("ADMIN_ID", "0") or 0):
        problems.append("ADMIN_ID не задан — админка будет закрыта для всех")
    webapp_url = os.getenv("WEBAPP_URL", "")
    if webapp_url.startswith("https://") and os.getenv("DEV_MODE", "") == "1":
        problems.append("DEV_MODE=1 на боевом домене: бот публикует в чат "
                        "рабочую ссылку с подписанной initData")
    if not os.path.isdir(DIST):
        problems.append(f"нет собранного фронта в {DIST} — "
                        f"выполни: cd webapp && npm run build")
    for p in problems:
        logging.error("КОНФИГ: %s", p)
    fatal = [p for p in problems if p.startswith(("BOT_TOKEN", "DEV_MODE"))]
    if fatal:
        raise SystemExit("Старт отменён:\n  " + "\n  ".join(fatal))


async def main():
    preflight()
    print("🚀 Cookie Merge: bot + API starting...")
    # без return_exceptions: падение любой из трёх задач роняет процесс,
    # и systemd (Restart=always) поднимает его заново. Продолжать жить с
    # мёртвым поллингом хуже — снаружи это выглядит как рабочий сервис
    await asyncio.gather(run_bot(), run_api(), run_notifier(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")
