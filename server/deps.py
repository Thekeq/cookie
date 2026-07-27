"""Проверка версии состояния: действие применяется к тому экрану, с которого
его отправили.

Зачем. Перетаскивание на доске адресуется НОМЕРАМИ клеток, а не самими
печеньками. Игрок, у которого приложение открыто и на телефоне, и на десктопе
(в Telegram это одно нажатие), отправляет «слей 3 и 4» с раскладки, которой
уже нет: в клетках 3 и 4 лежит другое, и слияние происходит — просто не то,
которое он видел. Ни один условный UPDATE это не ловит: запрос корректен,
неверна картинка, из которой он родился.

Как. Каждый ответ full_state несёт `revision: {user, board}`; клиент возвращает
увиденное значение заголовком, и расхождение превращается в 409 со свежим
состоянием вместо молча применённого чужого хода.

Заголовка нет — проверки нет. Старые сборки Mini App живут в чатах вечно и
обязаны продолжать работать; их поведение не меняется ни на байт.

Про X-User-Revision. Механика поддержана симметрично, но НАШ клиент его не
шлёт, и это не недоделка: user_revision двигает каждый батч кликов и каждый
сбор дохода фермы, то есть он меняется под игроком сам, без его участия.
Отбивать по нему покупки означало бы 409 посреди нормальной игры. Баланс и без
того защищён условными списаниями (`spend_cookies`, `WHERE cookies >= ?`) —
там проверять нечего. Заголовок остаётся для сервис-клиентов и отладки.
"""
from fastapi import Depends, Header

from server.auth import tg_user
from server.economy import ConflictError
from server.game_logic import db

# заголовок -> колонка в users
REVISION_COLUMNS = {"user": "user_revision", "board": "board_revision"}


def _wanted(raw: str) -> int | None:
    """Версия из заголовка или None, если её нет.

    Мусор («abc», пустая строка, отрицательное) трактуется как «не прислали»:
    сломанный или подменённый заголовок не должен превращаться в неубиваемый
    409 — тогда испорченный клиент терял бы возможность играть совсем."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


async def require_revision(
        tg: dict = Depends(tg_user),
        x_user_revision: str = Header(default="", alias="X-User-Revision"),
        x_board_revision: str = Header(default="", alias="X-Board-Revision"),
) -> dict:
    """Тот же tg_user, но с проверкой версии. Возвращает tg, поэтому в ручке
    достаточно заменить Depends(tg_user) на Depends(require_revision)."""
    wanted = {col: v for col, v in
              (("user_revision", _wanted(x_user_revision)),
               ("board_revision", _wanted(x_board_revision))) if v is not None}
    if not wanted:
        return tg
    # имена колонок — из константы выше, не из запроса: в f-string попадает
    # только то, что написано в этом файле
    row = db.q1(f"SELECT {', '.join(wanted)} FROM users WHERE user_id = ?", (tg["id"],))
    if row is None:
        return tg                    # игрока нет — пусть ручка отдаст свой 404
    if any(row[col] != v for col, v in wanted.items()):
        raise ConflictError(tg["id"])
    return tg
