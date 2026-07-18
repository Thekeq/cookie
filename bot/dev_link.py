"""Дев-хелпер: собирает initData с валидной HMAC-подписью (как у Telegram),
чтобы открывать Mini App в обычном браузере на 127.0.0.1 без туннеля.

Подпись идентична телеграмовской, поэтому server/auth.py принимает её как настоящую.
Работает только когда DEV_MODE=1 в .env — на проде выключить!
"""
import hashlib
import hmac
import json
import time
from urllib.parse import quote, urlencode

from bot.loader import BOT_TOKEN


def build_init_data(user_id: int, username: str, first_name: str,
                    start_param: str = "") -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": f"dev{user_id}",
        "user": json.dumps(
            {"id": user_id, "username": username or "", "first_name": first_name or ""},
            separators=(",", ":"), ensure_ascii=False),
    }
    if start_param:
        fields["start_param"] = start_param

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def build_dev_url(base_url: str, user_id: int, username: str, first_name: str,
                  start_param: str = "") -> str:
    """Ссылка вида http://127.0.0.1:8000/#tgWebAppData=<initData> —
    официальный telegram-web-app.js сам парсит initData из этого фрагмента."""
    init_data = build_init_data(user_id, username, first_name, start_param)
    return f"{base_url}/#tgWebAppData={quote(init_data)}"
