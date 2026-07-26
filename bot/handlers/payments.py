"""Оплата Telegram Stars: строгий pre_checkout + атомарное начисление.

Жизненный цикл покупки: создана (invoice) -> 'paid' (деньги получены, коммит
сразу) -> 'fulfilled' (товар выдан). Выдача (gl.fulfill_charge) перечитывает
статус внутри транзакции, поэтому повтор successful_payment и параллельные
worker'ы безопасны; зависшие 'paid' довыдаются на /auth (gl.fulfill_pending).

Тупиковые статусы (их разбирает человек, см. /api/admin/payments):
  'unmatched' — деньги пришли, но платёж не сходится с конфигом;
  'void'      — товар выдать нельзя (исчез из магазина / уже куплен навсегда);
  'refunded'  — Telegram вернул звёзды; prior_status хранит, из какого
                состояния платёж туда уехал, то есть было ли что откатывать.
"""
import logging
import time

from aiogram import F, Router
from aiogram.types import Message, PreCheckoutQuery

from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db
from server.i18n import tr

router = Router()
log = logging.getLogger(__name__)


def _parse_payload(payload: str) -> tuple[int | None, str]:
    user_id_str, _, item_key = (payload or "").partition(":")
    try:
        return int(user_id_str), item_key
    except ValueError:
        return None, item_key


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    """Пропускаем оплату только если товар существует, валюта XTR, цена
    совпадает с конфигом, платит тот, чей id зашит в payload, И товар ему
    ещё можно выдать.

    Последняя проверка обязательна здесь, а не после оплаты: invoice-ссылка
    многоразовая и остаётся в чате навсегда. Без неё игрок мог оплатить старую
    ссылку на Premium Пасс с уже активным премиумом (100⭐ в никуда) или
    купить оффлайн-кап 6ч поверх 12ч, где эффект применяется как max()."""
    user_id, item_key = _parse_payload(query.invoice_payload)
    item = cfg.SHOP_ITEMS.get(item_key)
    ok = (
        item is not None
        and user_id == query.from_user.id
        and query.currency == "XTR"
        and query.total_amount == item[2]
        and gl.purchase_blocked(user_id, item_key) is None
    )
    await query.answer(
        ok=ok, error_message=None if ok else "You already own this / invalid purchase")


@router.message(F.successful_payment)
async def on_paid(message: Message):
    sp = message.successful_payment
    user_id, item_key = _parse_payload(sp.invoice_payload)
    item = cfg.SHOP_ITEMS.get(item_key)
    charge_id = sp.telegram_payment_charge_id or ""

    # Платёж, который не сходится с конфигом (переименовали товар, поменяли
    # цену и передеплоили между pre_checkout и successful_payment), раньше
    # молча уходил в `return` — деньги списаны, товара нет, следов нет.
    # Теперь он всегда оседает строкой в purchases и в логе.
    mismatch = (item is None or user_id is None or not db.get_user(user_id)
                or user_id != message.from_user.id
                or sp.currency != "XTR" or sp.total_amount != item[2])
    if mismatch or not charge_id:
        log.error("unmatched Stars payment: charge=%r payload=%r from=%s amount=%s %s",
                  charge_id, sp.invoice_payload, message.from_user.id,
                  sp.total_amount, sp.currency)
        gl.record_unmatched_payment(
            user_id if user_id is not None else message.from_user.id,
            item_key, sp.total_amount, charge_id, sp.invoice_payload)
        lang = (db.get_user(message.from_user.id) or {}).get("lang") or "en"
        await message.answer(tr(lang, "pay_problem"))
        return

    # факт оплаты фиксируем СРАЗУ отдельным коммитом: даже если выдача упадёт,
    # запись 'paid' переживёт сбой и покупка будет довыдана (retry или /auth)
    db.exec(
        "INSERT INTO purchases (user_id, item_key, stars_amount, "
        "tg_payment_id, status, created_at) VALUES (?, ?, ?, ?, 'paid', ?) "
        # арбитр — частичный уникальный индекс по непустому charge_id; сюда мы
        # доходим только с charge_id на руках, так что NULL-строки не мешают
        "ON CONFLICT (tg_payment_id) WHERE tg_payment_id IS NOT NULL DO NOTHING",
        (user_id, item_key, sp.total_amount, charge_id, time.time()))

    # выдача атомарна и идемпотентна: статус перечитывается внутри транзакции
    gl.fulfill_charge(charge_id)

    lang = (db.get_user(user_id) or {}).get("lang") or "en"
    status = (db.q1("SELECT status FROM purchases WHERE tg_payment_id = ?",
                    (charge_id,)) or {}).get("status")
    if status == "void":
        # выдать нечего — деньги уже у нас, зовём разбираться руками
        await message.answer(tr(lang, "pay_problem"))
    else:
        await message.answer(tr(lang, "pay_ok", title=tr(lang, f"shop_{item_key}_t")))


@router.message(F.refunded_payment)
async def on_refunded(message: Message):
    """Возврат звёзд (поддержка Telegram / refundStarPayment).

    Без этого обработчика игрок оставлял себе премиум, часы оффлайн-капа,
    буст и печеньки при нулевой оплате — единственный способ получить товар
    бесплатно."""
    rp = message.refunded_payment
    charge_id = getattr(rp, "telegram_payment_charge_id", "") or ""
    if not charge_id:
        return
    outcome = gl.revoke_charge(charge_id, getattr(rp, "total_amount", None))
    if outcome in ("already_refunded", "no_row"):
        log.warning("Stars refund ignored (%s): charge=%s user=%s",
                    outcome, charge_id, message.from_user.id)
        return
    log.warning("Stars refund processed (%s): charge=%s user=%s",
                outcome, charge_id, message.from_user.id)
    lang = (db.get_user(message.from_user.id) or {}).get("lang") or "en"
    # «бонус снят» — только если он и правда был выдан. По платежу, застрявшему
    # в 'paid'/'void', снимать нечего, и об этом надо сказать прямо
    await message.answer(tr(lang, "pay_refunded" if outcome == "revoked"
                            else "pay_refunded_only"))
