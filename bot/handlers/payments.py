"""Оплата Telegram Stars: строгий pre_checkout + атомарное начисление.

Жизненный цикл покупки: создана (invoice) -> 'paid' (деньги получены) ->
'fulfilled' (товар выдан). Выдача идёт в одной транзакции со сменой статуса,
поэтому повтор successful_payment безопасен: fulfilled — дубль, paid — довыдача.
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


def _fulfill(user_id: int, item_key: str):
    """Выдаёт товар. Вызывается внутри db.tx()."""
    effect = cfg.SHOP_ITEMS[item_key][3]
    if effect["type"] == "cookies":
        # пачка масштабируется под доход покупателя (часы дохода, с минимумом)
        if "income_hours" in effect:
            amount = max(effect["min_amount"],
                         gl.hourly_income(user_id) * effect["income_hours"])
        else:
            amount = effect["amount"]
        gl.add_cookies(user_id, amount, count_earned=False)
    elif effect["type"] == "energy_full":
        user = db.get_user(user_id)
        db.update_user(user_id, energy=cfg.max_energy(user["level"]),
                       energy_updated_at=time.time())
    elif effect["type"] == "boost":
        db.exec("INSERT INTO boosts (user_id, boost_key, expires_at) VALUES (?, ?, ?)",
                (user_id, effect["key"], time.time() + effect["hours"] * 3600))
    elif effect["type"] == "bp_premium":
        db.update_user(user_id, bp_premium=1)


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
    existing = db.q1("SELECT id, status FROM purchases WHERE tg_payment_id = ?",
                     (charge_id,))
    if existing and existing["status"] == "fulfilled":
        return  # полный дубль — уже выдано

    # факт оплаты фиксируем СРАЗУ отдельным коммитом: даже если выдача упадёт,
    # запись 'paid' переживёт сбой и покупка будет довыдана при повторе
    if not existing:
        db.exec(
            "INSERT OR IGNORE INTO purchases (user_id, item_key, stars_amount, "
            "tg_payment_id, status, created_at) VALUES (?, ?, ?, ?, 'paid', ?)",
            (user_id, item_key, sp.total_amount, charge_id, time.time()))

    # выдача товара + статус 'fulfilled' — атомарно: упали посреди —
    # покупка осталась 'paid', следующий successful_payment довыдаст
    with db.tx():
        _fulfill(user_id, item_key)
        db.exec("UPDATE purchases SET status = 'fulfilled' WHERE tg_payment_id = ?",
                (charge_id,))

    lang = (db.get_user(user_id) or {}).get("lang") or "en"
    await message.answer(tr(lang, "pay_ok", title=tr(lang, f"shop_{item_key}_t")))
