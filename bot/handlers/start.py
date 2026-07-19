import os

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import (InlineKeyboardButton, InlineKeyboardMarkup, Message,
                           WebAppInfo)

from bot.loader import WEBAPP_URL
from server.game_logic import db
from server.i18n import norm_lang, tr

router = Router()

DEV_MODE = os.getenv("DEV_MODE", "0") == "1"
DEV_URL = os.getenv("DEV_URL", "http://127.0.0.1:8000")


def user_lang(message: Message) -> str:
    """Язык юзера: выбранный в Mini App, иначе language_code из Telegram."""
    user = db.get_user(message.from_user.id)
    if user and user.get("lang"):
        return user["lang"]
    return norm_lang(message.from_user.language_code)


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

    lang = user_lang(message)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=tr(lang, "start_play"), web_app=WebAppInfo(url=url))
    ]]) if url else None

    text = tr(lang, "start_text") + (tr(lang, "start_go") if kb else "")

    if DEV_MODE:
        from bot.dev_link import build_dev_url
        u = message.from_user
        dev_url = build_dev_url(DEV_URL, u.id, u.username or "", u.first_name or "",
                                start_param=payload)
        text += f"\n\n{tr(lang, 'dev_link')}\n<code>{dev_url}</code>"

    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)
