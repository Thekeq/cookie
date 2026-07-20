"""Оплата Telegram Stars: строгий pre_checkout + атомарное начисление.

Жизненный цикл покупки: создана (invoice) -> 'paid' (деньги получены, коммит
сразу) -> 'fulfilled' (товар выдан). Выдача (gl.fulfill_charge) перечитывает
статус внутри транзакции, поэтому повтор successful_payment и параллельные
worker'ы безопасны; зависшие 'paid' довыдаются на /auth (gl.fulfill_pending).
"""
import time

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db
from server.i18n import tr

router = Router()


def _parse_payload(payload: str) -> tuple[int | None, str]:
    user_id_str, _, item_key = (payload or "").partition(":")
    try:
        return int(user_id_str), item_key
    except ValueError:
        return None, item_key


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Пропускаем оплату только если товар существует, валюта XTR, цена
    совпадает с конфигом и платит тот, чей id зашит в payload."""
    user_id, item_key = _parse_payload(query.invoice_payload)
    item = cfg.SHOP_ITEMS.get(item_key)
    ok = (
        item is not None
        and user_id == query.from_user.id
        and query.currency == "XTR"
        and query.total_amount == item[2]
    )
    await query.answer(ok=ok, error_message=None if ok else "Invalid purchase")


@router.message(F.successful_payment)
async def on_paid(message: Message):
    sp = message.successful_payment
    user_id, item_key = _parse_payload(sp.invoice_payload)
    item = cfg.SHOP_ITEMS.get(item_key)
    if (item is None or user_id is None or not db.get_user(user_id)
            or user_id != message.from_user.id
            or sp.currency != "XTR" or sp.total_amount != item[2]):
        return

    charge_id = sp.telegram_payment_charge_id
    # факт оплаты фиксируем СРАЗУ отдельным коммитом: даже если выдача упадёт,
    # запись 'paid' переживёт сбой и покупка будет довыдана (retry или /auth)
    db.exec(
        "INSERT OR IGNORE INTO purchases (user_id, item_key, stars_amount, "
        "tg_payment_id, status, created_at) VALUES (?, ?, ?, ?, 'paid', ?)",
        (user_id, item_key, sp.total_amount, charge_id, time.time()))

    # выдача атомарна и идемпотентна: статус перечитывается внутри транзакции
    gl.fulfill_charge(charge_id)

    lang = (db.get_user(user_id) or {}).get("lang") or "en"
    await message.answer(tr(lang, "pay_ok", title=tr(lang, f"shop_{item_key}_t")))
