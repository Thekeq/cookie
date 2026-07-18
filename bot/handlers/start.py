import os

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)

from bot.loader import WEBAPP_URL

router = Router()

DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_URL = os.getenv("DEV_URL", "http://127.0.0.1:8000")


@router.message(CommandStart())
async def cmd_start(message: Message):
    # ref_/src_ параметры из t.me/bot?start=... тоже пробрасываем в Mini App —
    # регистрация и учёт источников происходят на /api/auth по start_param
    payload = ""
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        payload = parts[1].strip()

    url = WEBAPP_URL
    if payload and url:
        url = f"{url}?tgWebAppStartParam={payload}"

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🍪 Играть!", web_app=WebAppInfo(url=url))
    ]]) if url else None

    text = (
        "🍪 <b>Cookie Merge</b>\n\n"
        "Соединяй печеньки, качай кликер, зови друзей и получай награды!\n\n"
        + ("Жми кнопку и погнали 👇" if kb else "")
    )

    if DEV_MODE:
        from bot.dev_link import build_dev_url
        u = message.from_user
        dev_url = build_dev_url(DEV_URL, u.id, u.username or "", u.first_name or "",
                                start_param=payload)
        text += (
            "\n\n🛠 <b>Dev-ссылка</b> (открой в браузере на этой машине):\n"
            f"<code>{dev_url}</code>"
        )

    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
