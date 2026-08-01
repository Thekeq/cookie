"""Экземпляры Bot и Dispatcher. Конфиг — только через server.settings."""
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from server import settings

# Реэкспорт для хендлеров: они писались до появления settings и обращаются к
# этим именам напрямую. Значения теперь одни на весь процесс.
BOT_TOKEN = settings.BOT_TOKEN
ADMIN_ID = settings.ADMIN_ID
WEBAPP_URL = settings.WEBAPP_URL

if not BOT_TOKEN:
    # Проверка остаётся здесь, а не только в preflight: без токена Bot() всё
    # равно не создать, и понятное сообщение лучше трейсбека из aiogram
    raise RuntimeError("BOT_TOKEN not set")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
# MemoryStorage безопасен даже при нескольких воркерах: FSM не использует ни
# один хендлер, так что состояние диалога нам просто негде потерять.
dp = Dispatcher(storage=MemoryStorage())
