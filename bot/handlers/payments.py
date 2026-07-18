"""Оплата Telegram Stars: pre_checkout + начисление после successful_payment."""
import time

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db

router = Router()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    payload = query.invoice_payload or ""
    ok = ":" in payload and payload.split(":", 1)[1] in cfg.SHOP_ITEMS
    await query.answer(ok=ok, error_message=None if ok else "Товар не найден")


@router.message(F.successful_payment)
async def on_paid(message: Message):
    sp = message.successful_payment
    user_id_str, _, item_key = (sp.invoice_payload or "").partition(":")
    try:
        user_id = int(user_id_str)
    except ValueError:
        return
    if item_key not in cfg.SHOP_ITEMS or not db.get_user(user_id):
        return

    # идемпотентность: один charge_id — одно начисление
    if db.q1("SELECT id FROM purchases WHERE tg_payment_id = ?",
             (sp.telegram_payment_charge_id,)):
        return
    db.exec("INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
            "status, created_at) VALUES (?, ?, ?, ?, 'paid', ?)",
            (user_id, item_key, sp.total_amount, sp.telegram_payment_charge_id, time.time()))

    title, _desc, _stars, effect = cfg.SHOP_ITEMS[item_key]
    if effect["type"] == "cookies":
        gl.add_cookies(user_id, effect["amount"], count_earned=False)
    elif effect["type"] == "energy_full":
        user = db.get_user(user_id)
        db.update_user(user_id, energy=cfg.max_energy(user["level"]),
                       energy_updated_at=time.time())
    elif effect["type"] == "boost":
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                (user_id, effect["key"], time.time() + effect["hours"] * 3600))
    elif effect["type"] == "bp_premium":
        db.update_user(user_id, bp_premium=1)

    await message.answer(f"✅ Покупка <b>{title}</b> активирована! Спасибо 🍪")
