"""Валидация Telegram WebApp initData (HMAC-SHA256) + dependency для роутеров."""
import hashlib
import hmac
import json
import time
import os
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

AUTH_MAX_AGE = 60 * 60 * 24  # initData старше суток не принимаем


def validate_init_data(init_data: str) -> dict:
    """Проверяет подпись initData, возвращает распарсенный словарь.

    Алгоритм из доков TG: secret_key = HMAC_SHA256("WebAppData", bot_token),
    hash = HMAC_SHA256(secret_key, data_check_string).
    """
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(401, "Bad initData")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "No hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(401, "initData signature mismatch")

    auth_date = int(parsed.get("auth_date", "0"))
    if time.time() - auth_date > AUTH_MAX_AGE:
        raise HTTPException(401, "initData expired, reopen the app")

    if "user" in parsed:
        parsed["user"] = json.loads(parsed["user"])
    return parsed


async def tg_user(authorization: str = Header(default="")) -> dict:
    """FastAPI dependency: заголовок Authorization: tma <initData>.

    Возвращает {"id": ..., "username": ..., "first_name": ..., "start_param": ...}
    """
    if not authorization.startswith("tma "):
        raise HTTPException(401, "Use 'Authorization: tma <initData>'")
    data = validate_init_data(authorization[4:])
    user = data.get("user")
    if not user or "id" not in user:
        raise HTTPException(401, "No user in initData")
    return {
        "id": user["id"],
        "username": user.get("username", ""),
        "first_name": user.get("first_name", ""),
        "start_param": data.get("start_param", ""),
    }


async def tg_admin(authorization: str = Header(default="")) -> dict:
    """Dependency для админ-роутов: тот же tma-заголовок + проверка ADMIN_ID."""
    user = await tg_user(authorization)
    if user["id"] != ADMIN_ID:
        raise HTTPException(403, "Admins only")
    return user
