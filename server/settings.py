"""Единственная точка чтения окружения.

Раньше `os.getenv` был рассыпан по восьми файлам, и у одного и того же ключа
жило по два значения: `bot/loader.py` падал без `BOT_TOKEN`, а `server/auth.py`
на том же отсутствующем токене отвечал 503; `WEBAPP_URL` читался в трёх местах
с разной нормализацией. Хуже другое: `load_dotenv()` вызывался только в
`bot/loader.py`, поэтому «прочитан ли .env» зависело от ПОРЯДКА ИМПОРТОВ —
любой скрипт, который трогал `db.py` раньше загрузчика бота, видел пустое
окружение и молча брал дефолты.

Здесь env читается один раз при импорте, приводится к типам и проверяется.
Модуль намеренно ничего не импортирует из `server/` — его тянут все остальные,
включая `db.py`, и кольцевой импорт убил бы старт.

Секреты (`BOT_TOKEN`, `DATABASE_URL`, `REDIS_URL`, `WEBHOOK_SECRET`) НИКОГДА
не печатаются целиком: для логов есть `redact()` и `summary()`.
"""
import hashlib
import os

from dotenv import load_dotenv

# override=False: уже выставленная переменная окружения сильнее файла. На это
# опираются тесты (os.environ["DATABASE_PATH"] до импорта) и systemd, где
# Environment= в unit-файле должен побеждать забытый .env рядом с кодом.
load_dotenv(override=False)


def _s(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _i(key: str, default: int = 0) -> int:
    raw = _s(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        # кривое число в конфиге — это опечатка, а не «ноль». Молча взять
        # дефолт значит запустить бота с чужими лимитами
        raise SystemExit(f"КОНФИГ: {key}={raw!r} — ожидалось целое число")


def _b(key: str, default: bool = False) -> bool:
    raw = _s(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ---------- Telegram ----------
BOT_TOKEN = _s("BOT_TOKEN")
BOT_USERNAME = _s("BOT_USERNAME").lstrip("@")
ADMIN_ID = _i("ADMIN_ID")
CHANNEL_USERNAME = _s("CHANNEL_USERNAME").lstrip("@")

# ---------- Mini App / HTTP ----------
WEBAPP_URL = _s("WEBAPP_URL").rstrip("/")
HOST = _s("HOST", "0.0.0.0")
PORT = _i("PORT", 8000)
DEBUG = _b("DEBUG")
# TTL initData: внутри открытого Mini App она не обновляется, поэтому должен
# покрывать долгую сессию. Уменьшать нельзя — игрок начнёт получать 401 посреди
# игры, ничего не сделав
AUTH_MAX_AGE = _i("AUTH_MAX_AGE", 60 * 60 * 24)

# ---------- Дев-режим ----------
DEV_MODE = _b("DEV_MODE")
DEV_URL = _s("DEV_URL", "http://127.0.0.1:8000")

# ---------- Хранилища ----------
# SQLite-файл (текущий прод) и PostgreSQL-строка (переезд). Заполнен
# DATABASE_URL — значит работаем на нём, DATABASE_PATH при этом игнорируется.
DATABASE_PATH = _s("DATABASE_PATH", "data.db")
DATABASE_URL = _s("DATABASE_URL")
# Redis нужен, когда воркеров больше одного: лимитер и владение фоновыми
# задачами обязаны быть общими. Пусто — работаем на in-process фолбэке.
REDIS_URL = _s("REDIS_URL")

# ---------- Процессы ----------
# polling — один процесс, дев и текущий прод. webhook — обязателен, как только
# воркеров больше одного: поллинг физически возможен только из одного места.
BOT_MODE = _s("BOT_MODE", "polling").lower()
# Путь webhook'а держим неугадываемым: адрес утекает в любой лог прокси, и по
# нему шлют мусор. Секрет проверяется отдельно заголовком.
WEBHOOK_PATH = _s("WEBHOOK_PATH", "/tg/webhook")
# Внешний адрес для setWebhook. Пусто — берём WEBAPP_URL: обычно это один домен.
WEBHOOK_BASE = _s("WEBHOOK_BASE") or WEBAPP_URL
# Секрет заголовка X-Telegram-Bot-Api-Secret-Token. Не задан — выводим из
# токена: у кого есть токен, тому webhook-секрет уже не нужен, поэтому лишняя
# обязательная переменная здесь только создавала бы шанс запустить прод без неё.
WEBHOOK_SECRET = _s("WEBHOOK_SECRET") or (
    hashlib.sha256(f"webhook:{BOT_TOKEN}".encode()).hexdigest()[:32]
    if BOT_TOKEN else "")

# Роль процесса: api — только HTTP, scheduler — только фоновые задачи,
# all — и то и то (одиночный запуск, как сейчас).
ROLE = _s("ROLE", "all").lower()
# Сколько процессов поднимать под API.
WEB_CONCURRENCY = _i("WEB_CONCURRENCY", 1)

# ---------- Логи ----------
LOG_FILE = _s("LOG_FILE", "cookie.log")

_SECRET_KEYS = ("BOT_TOKEN", "DATABASE_URL", "REDIS_URL", "WEBHOOK_SECRET")


def redact(value: str) -> str:
    """Значение секрета в виде, пригодном для лога.

    Показываем длину и хвост: этого хватает, чтобы отличить «не задан» от
    «задан не тот», и не хватает, чтобы воспользоваться."""
    if not value:
        return "<не задан>"
    return f"<{len(value)} симв., …{value[-4:]}>"


def summary() -> str:
    """Строка для лога на старте: с каким конфигом мы поднялись.

    Половина инцидентов «на проде ведёт себя иначе» — это незамеченное
    расхождение конфига, и разбирать его по логам нечем, если конфиг нигде не
    записан."""
    parts = [
        f"role={ROLE}", f"bot={BOT_MODE}", f"workers={WEB_CONCURRENCY}",
        f"port={PORT}", f"debug={int(DEBUG)}", f"dev={int(DEV_MODE)}",
        f"db={'postgres' if DATABASE_URL else DATABASE_PATH}",
        f"redis={'on' if REDIS_URL else 'off (in-process)'}",
        f"webapp={WEBAPP_URL or '<не задан>'}",
        f"admin={ADMIN_ID or '<не задан>'}",
        f"token={redact(BOT_TOKEN)}",
    ]
    return "конфиг: " + " ".join(parts)


# Проблемы, при которых запускаться нельзя: молчаливо кривой конфиг обходится
# дороже падения. Ключ — префикс сообщения, значение — фатально ли.
def problems() -> list[tuple[str, bool]]:
    """[(текст, фатально)] — что не так с окружением.

    Отдельной функцией, а не проверкой при импорте: тесты и скрипты имеют право
    работать без BOT_TOKEN, а вот боевой процесс — нет."""
    out: list[tuple[str, bool]] = []
    if not BOT_TOKEN:
        out.append(("BOT_TOKEN не задан — подпись initData проверить нечем", True))
    if not ADMIN_ID:
        out.append(("ADMIN_ID не задан — админка будет закрыта для всех", False))
    if WEBAPP_URL.startswith("https://") and DEV_MODE:
        out.append(("DEV_MODE=1 на боевом домене: бот публикует в чат рабочую "
                    "ссылку с подписанной initData", True))
    if DEBUG and WEBAPP_URL.startswith("https://"):
        out.append(("DEBUG=1 на боевом домене: наружу открыты /docs и "
                    "openapi.json со всеми ручками, включая админские", True))
    if BOT_MODE not in ("polling", "webhook"):
        out.append((f"BOT_MODE={BOT_MODE!r} — допустимо polling или webhook", True))
    if ROLE not in ("all", "api", "scheduler"):
        out.append((f"ROLE={ROLE!r} — допустимо all, api или scheduler", True))
    if ROLE in ("api", "scheduler") and BOT_MODE == "polling":
        # поллинг ведёт только процесс с ROLE=all: при разделении ролей ни один
        # из них его не поднимает, и апдейты молча остаются непрочитанными
        out.append((f"ROLE={ROLE} при BOT_MODE=polling: апдейты не будет тянуть "
                    "никто — для разделённых ролей нужен BOT_MODE=webhook", True))
    if BOT_MODE == "webhook" and not WEBHOOK_PATH.startswith("/"):
        # маршрут регистрируется при импорте main: без слеша это падение на
        # старте с трейсбеком из Starlette вместо понятной причины
        out.append((f"WEBHOOK_PATH={WEBHOOK_PATH!r} — путь должен начинаться "
                    "со слеша", True))
    if BOT_MODE == "webhook" and not WEBHOOK_BASE.startswith("https://"):
        # Telegram шлёт апдейты только на https и только на публичный адрес
        out.append(("BOT_MODE=webhook, но WEBHOOK_BASE/WEBAPP_URL не https-адрес "
                    "— Telegram не примет setWebhook", True))
    # Вот это и есть цена «одного процесса»: лимитер и владение фоновыми
    # задачами живут в памяти. Второй воркер удваивает лимиты и запускает
    # ролловер сезона вторым потоком поверх первого.
    if WEB_CONCURRENCY > 1 and not REDIS_URL:
        out.append(("WEB_CONCURRENCY > 1 без REDIS_URL: лимит запросов станет "
                    f"{WEB_CONCURRENCY}-кратным, фоновые задачи запустятся в "
                    "каждом воркере", True))
    if WEB_CONCURRENCY > 1 and BOT_MODE == "polling":
        out.append(("WEB_CONCURRENCY > 1 при BOT_MODE=polling: апдейты будет "
                    "тянуть каждый воркер, Telegram отдаст их случайному", True))
    if DATABASE_URL and not DATABASE_URL.startswith(
            ("postgresql://", "postgres://")):
        # опечатка вроде sqlite:///data.db в этом ключе не «включит SQLite», а
        # уедет в драйвер PostgreSQL с невнятной ошибкой соединения
        out.append(("DATABASE_URL заполнен, но это не postgresql://-строка — "
                    "для файловой базы есть DATABASE_PATH", True))
    if WEB_CONCURRENCY > 1 and DATABASE_URL == "":
        out.append(("WEB_CONCURRENCY > 1 на SQLite: писатель в файле один, "
                    "воркеры будут ждать друг друга на блокировке", False))
    return out
