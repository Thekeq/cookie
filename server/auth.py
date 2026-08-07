"""Вход в API: подпись Telegram WebApp initData (HMAC-SHA256) и короткая
серверная сессия поверх неё.

Почему двух способов два. initData Telegram выдаёт один раз при открытии Mini
App и внутри открытого приложения НЕ ОБНОВЛЯЕТ, поэтому её время жизни у нас
равно длине игровой сессии (AUTH_MAX_AGE, по умолчанию сутки). Всё это время
одна и та же строка ходит в каждом запросе как bearer: утёкшая из лога прокси,
из отчёта об ошибке или из истории браузера, она даёт полный доступ к аккаунту
на оставшиеся часы, и отозвать её нельзя — подпись чужая, наша только проверка.

Поэтому первый успешный вход по initData дополнительно выдаёт СВОЮ сессию:
короткий токен, подписанный отдельным ключом, с явным сроком и user_id внутри.
Он живёт SESSION_TTL (30 минут), продлевается на лету, пока игрок играет, и
после утечки протухает сам. initData при этом продолжает работать как раньше:
сборки Mini App живут в чатах вечно, и ни одна из них про сессии не знает.

Токен уезжает клиенту заголовком ответа (X-Session-Token), а не полем в теле:
ручку /api/auth это не трогает, значит и её схему менять не нужно, а забрать
токен может любой запрос — в том числе те сборки, которые появятся потом.
"""
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from fastapi import Header, HTTPException, Request, Response

from server import obs, settings

BOT_TOKEN = settings.BOT_TOKEN
ADMIN_ID = settings.ADMIN_ID
AUTH_MAX_AGE = settings.AUTH_MAX_AGE
MAX_INIT_DATA = 4096  # реальная initData ~300-600 байт

# Часы клиента и сервера расходятся всегда: NTP на телефоне отстаёт, на машине
# спешит. Поэтому запас вперёд есть, но маленький — секунда из будущего это
# перекос часов, час из будущего это подделка (см. проверку auth_date ниже).
AUTH_FUTURE_SKEW = 60

# secret_key зависит только от токена — считаем один раз, а не на каждый запрос
_SECRET_KEY = (hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
               if BOT_TOKEN else None)

# ---------- серверная сессия ----------

# Ключ подписи сессий выводится из того же секрета, но с ДРУГОЙ меткой:
# перепутать наш токен с телеграмным нельзя даже теоретически, потому что
# ключи разные. Отдельной переменной окружения намеренно нет — иначе её забыли
# бы задать, и сессии подписывались бы пустотой.
_SESSION_KEY = (hmac.new(b"CookieSession", BOT_TOKEN.encode(),
                         hashlib.sha256).digest() if BOT_TOKEN else None)

SESSION_VERSION = "1"
# 30 минут: достаточно, чтобы не дёргать переподпись у активного игрока, и
# достаточно мало, чтобы утёкший токен не пережил разбор инцидента.
SESSION_TTL = int(getattr(settings, "SESSION_TTL", 30 * 60))
# Продлеваем, когда прошла половина срока: у играющего сессия не кончается
# никогда, у закрывшего приложение — через SESSION_TTL после последнего запроса.
SESSION_RENEW_AFTER = SESSION_TTL // 2
# Токен ~90 символов; всё, что длиннее, даже не разбираем
MAX_SESSION_TOKEN = 256

SESSION_HEADER = "X-Session-Token"
SESSION_EXPIRES_HEADER = "X-Session-Expires"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _sign_session(payload: str) -> str:
    return _b64(hmac.new(_SESSION_KEY, payload.encode(), hashlib.sha256).digest())


def issue_session(user_id: int, lang: str = "en", now: float | None = None) -> tuple[str, int]:
    """(токен, момент истечения) для игрока, уже доказавшего свою личность.

    Формат намеренно плоский: `1.<uid>.<exp>.<lang>.<подпись>`. JWT здесь дал
    бы ровно те же поля, но принёс бы разбор чужого JSON до проверки подписи и
    выбор алгоритма из самого токена — две классические дыры ради формата,
    который читает только наш же сервер."""
    if not _SESSION_KEY:
        raise HTTPException(503, "Server auth is not configured")
    exp = int((now or time.time()) + SESSION_TTL)
    payload = f"{SESSION_VERSION}.{int(user_id)}.{exp}.{lang}"
    return f"{payload}.{_sign_session(payload)}", exp


def verify_session(token: str, now: float | None = None) -> dict:
    """Разбор своего токена. Кидает 401, если он чужой, битый или протух.

    Отдельный код `err_session_expired` — не косметика: клиент по нему знает,
    что нужно повторить запрос с initData, а не показывать игроку «войдите
    заново» на ручке, которая всего лишь пережила полчаса без активности."""
    if not _SESSION_KEY:
        raise HTTPException(503, "Server auth is not configured")
    if not token or len(token) > MAX_SESSION_TOKEN:
        raise HTTPException(401, "Bad session token")
    payload, _, sig = token.rpartition(".")
    if not payload or not sig:
        raise HTTPException(401, "Bad session token")
    # сравнение постоянного времени: обычное == выходит на первом несовпавшем
    # байте, и по времени ответа подпись подбирается посимвольно
    if not hmac.compare_digest(_sign_session(payload), sig):
        raise HTTPException(401, "Bad session token")
    parts = payload.split(".")
    if len(parts) != 4 or parts[0] != SESSION_VERSION:
        raise HTTPException(401, "Bad session token")
    try:
        uid, exp = int(parts[1]), int(parts[2])
    except ValueError:
        raise HTTPException(401, "Bad session token")
    if uid <= 0:
        raise HTTPException(401, "Bad session token")
    if (now or time.time()) > exp:
        raise HTTPException(401, "err_session_expired")
    lang = parts[3] if parts[3] in ("en", "uk", "ru") else "en"
    return {"id": uid, "exp": exp, "lang": lang}


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
    # Возраст проверяется В ОБЕ СТОРОНЫ. Раньше смотрели только «не слишком ли
    # старая», и дата из будущего проходила: подпись-то верна, а окно годности
    # такая initData получала не сутки, а сутки плюс сколько угодно вперёд —
    # достаточно один раз перевести часы на устройстве, с которого её сняли.
    # Вперёд оставляем только запас на расхождение часов.
    age = time.time() - auth_date
    if age > AUTH_MAX_AGE:
        raise HTTPException(401, "initData expired, reopen the app")
    if age < -AUTH_FUTURE_SKEW:
        raise HTTPException(401, "initData is from the future")

    if "user" in parsed:
        try:
            parsed["user"] = json.loads(parsed["user"])
        except (ValueError, TypeError):
            raise HTTPException(401, "Bad initData")
        if not isinstance(parsed["user"], dict):
            raise HTTPException(401, "Bad initData")
    return parsed


def _from_init_data(init_data: str, lang: str) -> dict:
    """Игрок из initData: полный набор полей, включая имя и start_param."""
    data = validate_init_data(init_data)
    user = data.get("user")
    if not user or "id" not in user:
        raise HTTPException(401, "No user in initData")
    try:
        uid = int(user["id"])
    except (TypeError, ValueError):
        raise HTTPException(401, "Bad initData")
    return {
        "id": uid,
        # обрезаем: имя и username идут в БД и в текст сообщений бота,
        # а Telegram не гарантирует их длину так строго, как хочется
        "username": str(user.get("username", ""))[:64],
        "first_name": str(user.get("first_name", ""))[:64],
        "start_param": str(data.get("start_param", ""))[:64],
        "lang": lang,
        "auth": "initdata",
    }


def _from_session(token: str, lang: str) -> dict:
    """Игрок из своей сессии.

    Имени и start_param здесь нет и быть не может: в токене лежит только то,
    что нужно для авторизации. Регистрация (`/api/auth` для НОВОГО игрока) на
    этом пути и не оказывается — сессию неоткуда взять, пока initData не
    сходила хотя бы раз."""
    sess = verify_session(token)
    return {
        "id": sess["id"],
        "username": "",
        "first_name": "",
        "start_param": "",
        # язык берём из заголовка запроса, а не из токена: игрок мог сменить
        # язык Telegram, а токен ему за это перевыпускать никто не будет
        "lang": lang,
        "auth": "session",
        "session_exp": sess["exp"],
    }


def _authenticate(authorization: str, lang: str, response: Response | None) -> dict:
    """Разбор заголовка Authorization в игрока + выдача/продление сессии."""
    if authorization.startswith("tma "):
        user = _from_init_data(authorization[4:], lang)
        _attach_session(response, user["id"], lang)
        return user
    if authorization[:7].lower() == "bearer ":
        user = _from_session(authorization[7:].strip(), lang)
        # продление на лету: у играющего сессия не кончается посреди игры, а
        # переподписываем мы её не чаще раза в SESSION_RENEW_AFTER секунд
        if user["session_exp"] - time.time() < SESSION_RENEW_AFTER:
            _attach_session(response, user["id"], lang)
        return user
    raise HTTPException(401, "Use 'Authorization: tma <initData>' or "
                             "'Authorization: Bearer <session token>'")


def _attach_session(response: Response | None, uid: int, lang: str):
    """Положить свежий токен в заголовки ответа. Клиент волен его игнорировать."""
    if response is None:
        return
    token, exp = issue_session(uid, lang)
    response.headers[SESSION_HEADER] = token
    response.headers[SESSION_EXPIRES_HEADER] = str(exp)


async def tg_user(request: Request, response: Response,
                  authorization: str = Header(default=""),
                  x_lang: str = Header(default="en", alias="X-Lang")) -> dict:
    """FastAPI dependency: `Authorization: tma <initData>` либо `Bearer <токен>`.

    Возвращает {"id", "username", "first_name", "start_param", "lang", "auth"};
    lang приходит из Mini App заголовком X-Lang (en/uk/ru).

    Результат запоминается на request.state — и успех, и отказ. Своя память, а
    не встроенный кэш зависимостей FastAPI: лимитер (server.deps.rate_limit)
    висит на приложении целиком и зовёт эту функцию ЧЕРЕЗ ОБЁРТКУ, поэтому по
    ключу кэша FastAPI это уже другая зависимость, и подпись проверялась бы по
    два раза на запрос.
    """
    cached = getattr(request.state, "_tg_auth", None) if request else None
    if cached is not None:
        kind, value = cached
        if kind == "err":
            raise value
        return value

    lang = x_lang if x_lang in ("en", "uk", "ru") else "en"
    try:
        user = _authenticate(authorization or "", lang, response)
    except HTTPException as e:
        if request:
            request.state._tg_auth = ("err", e)
        raise
    # С этого места все строки лога этого запроса несут user_id. Раньше по
    # жалобе «пропали печеньки» найти в логе именно этого игрока было нечем:
    # ошибка пишется в одном модуле, а кто её вызвал — известно только здесь.
    obs.bind_user(user["id"])
    if request:
        request.state._tg_auth = ("ok", user)
    return user


async def tg_admin(request: Request, response: Response,
                   authorization: str = Header(default=""),
                   x_lang: str = Header(default="en", alias="X-Lang")) -> dict:
    """Dependency для админ-роутов: обычный вход + проверка ADMIN_ID."""
    user = await tg_user(request, response, authorization, x_lang)
    if user["id"] != ADMIN_ID:
        raise HTTPException(403, "Admins only")
    return user
