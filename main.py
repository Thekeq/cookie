"""Один процесс: FastAPI (API + раздача Mini App) + aiogram-бот на polling."""
import asyncio
import logging
import os

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from bot.loader import bot, dp
from bot.handlers import start, payments
from bot.notifier import run_notifier
from server.routers import game, meta, admin, farm

logging.basicConfig(level=logging.WARNING)

app = FastAPI(title="Cookie Merge API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # дев-режим; в проде сузить до WEBAPP_URL
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(meta.router)
app.include_router(game.router)
app.include_router(admin.router)
app.include_router(farm.router)

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


async def main():
    print("🚀 Cookie Merge: bot + API starting...")
    await asyncio.gather(run_bot(), run_api(), run_notifier(bot))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped")
