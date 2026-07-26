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

# initData внутри открытого Mini App не обновляется, поэтому TTL должен
# покрывать долгую сессию — сутки. Уменьшать нельзя: игрок с открытым
# приложением начнёт получать 401 посреди игры.
AUTH_MAX_AGE = int(os.getenv("AUTH_MAX_AGE", str(60 * 60 * 24)))
MAX_INIT_DATA = 4096  # реальная initData ~300-600 байт

# secret_key зависит только от токена — считаем один раз, а не на каждый запрос
_SECRET_KEY = (hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
               if BOT_TOKEN else None)


def validate_init_data(init_data: str) -> dict:
    """Проверяет подпись initData, возвращает распарсенный словарь.

    Алгоритм из доков TG: secret_key = HMAC_SHA256("WebAppData", bot_token),
    hash = HMAC_SHA256(secret_key, data_check_string).
    """
    if not _SECRET_KEY:
        # без токена подпись проверить нельзя. Раньше тут падал AttributeError
        # на None.encode() и превращался в 500: снаружи это выглядело как
        # «сервер сломался», а по факту любой запрос уходил в трейсбек
        raise HTTPException(503, "Server auth is not configured")
    # длину режем ДО парсинга: parse_qsl на многомегабайтной строке съедает
    # память и процессорное время ещё до любой проверки подписи
    if not init_data or len(init_data) > MAX_INIT_DATA:
        raise HTTPException(401, "Bad initData")

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        raise HTTPException(401, "Bad initData")

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "No hash in initData")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    calc_hash = hmac.new(_SECRET_KEY, data_check_string.encode(),
                         hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calc_hash, received_hash):
        raise HTTPException(401, "initData signature mismatch")

    # auth_date и user приходят подписанными, но битыми быть всё равно могут:
    # int()/json.loads() на мусоре давали 500 вместо честного 401
    try:
        auth_date = int(parsed.get("auth_date", "0"))
    except ValueError:
        raise HTTPException(401, "Bad initData")
    if time.time() - auth_date > AUTH_MAX_AGE:
        raise HTTPException(401, "initData expired, reopen the app")

    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except (ValueError, TypeError):
            raise HTTPException(401, "Bad initData")
        if not isinstance(parsed["user"], dict):
            raise HTTPException(401, "Bad initData")
    return parsed


async def tg_user(authorization: str = Header(default=""),
                  x_lang: str = Header(default="en", alias="X-Lang")) -> dict:
    """FastAPI dependency: заголовок Authorization: tma <initData>.

    Возвращает {"id", "username", "first_name", "start_param", "lang"};
    lang приходит из Mini App заголовком X-Lang (en/uk/ru).
    """
    if not authorization.startswith("tma "):
        raise HTTPException(401, "Use 'Authorization: tma <initData>'")
    data = validate_init_data(authorization[4:])
    user = data.get("user")
    if not user or "id" not in user:
        raise HTTPException(401, "No user in initData")
    try:
        uid = int(user["id"])
    except (TypeError, ValueError):
        raise HTTPException(401, "Bad initData")
    lang = x_lang if x_lang in ("en", "uk", "ru") else "en"
    return {
        "id": uid,
        # обрезаем: имя и username идут в БД и в текст сообщений бота,
        # а Telegram не гарантирует их длину так строго, как хочется
        "username": str(user.get("username", ""))[:64],
        "first_name": str(user.get("first_name", ""))[:64],
        "start_param": str(data.get("start_param", ""))[:64],
        "lang": lang,
    }


async def tg_admin(authorization: str = Header(default=""),
                   x_lang: str = Header(default="en", alias="X-Lang")) -> dict:
    """Dependency для админ-роутов: тот же tma-заголовок + проверка ADMIN_ID."""
    user = await tg_user(authorization, x_lang)
    if user["id"] != ADMIN_ID:
        raise HTTPException(403, "Admins only")
    return user
