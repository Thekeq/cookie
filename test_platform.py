"""Тесты платформы (Этап 2): конфиг, разделяемое состояние, роли процессов.

Отдельный файл, а не дописка в существующие: тут проверяется не игра, а
окружение, в котором она живёт. Такие тесты обязаны быть ГЕРМЕТИЧНЫМИ — они
перезагружают server.settings с подменённым os.environ, и локальный .env
разработчика на результат влиять не должен, иначе «у меня проходит» перестанет
что-либо означать. Поэтому load_dotenv на время проверок затыкается заглушкой.
"""
import importlib
import os
import sys
import tempfile

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
os.environ["DATABASE_PATH"] = os.path.join(tempfile.gettempdir(),
                                           "cookie_platform_test.db")

import dotenv

from server import settings as _settings

passed = failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK  {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def reload_settings(**env):
    """server.settings с подменённым окружением.

    Ключ со значением None удаляется — так проверяется «переменная не задана».
    load_dotenv подменяется на пустышку: иначе значение вернулось бы из .env
    рядом с кодом, и тест зависел бы от машины, на которой запущен."""
    saved = {k: os.environ.get(k) for k in env}
    real_load = dotenv.load_dotenv
    dotenv.load_dotenv = lambda *a, **k: False
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        return importlib.reload(_settings)
    finally:
        dotenv.load_dotenv = real_load
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def texts(module) -> str:
    return " | ".join(t for t, _ in module.problems())


def fatals(module) -> list[str]:
    return [t for t, f in module.problems() if f]


print("\n=== 1. Чтение и типы ===")
s = reload_settings(PORT="9001", WEB_CONCURRENCY="4", DEBUG="yes",
                    WEBAPP_URL="https://example.com/", ADMIN_ID="42")
check("1.1 int читается", s.PORT == 9001 and s.WEB_CONCURRENCY == 4)
check("1.2 bool понимает yes/on/1", s.DEBUG is True)
check("1.3 у WEBAPP_URL снимается хвостовой слеш",
      s.WEBAPP_URL == "https://example.com")
s = reload_settings(PORT=None, DEBUG="off", CHANNEL_USERNAME="@cookies",
                    BOT_USERNAME="@my_bot")
check("1.4 дефолт при пустом значении", s.PORT == 8000)
check("1.5 bool понимает off", s.DEBUG is False)
check("1.6 @ снимается с имён канала и бота",
      s.CHANNEL_USERNAME == "cookies" and s.BOT_USERNAME == "my_bot")

# кривое число — опечатка, а не ноль: молча взять дефолт значит запустить бота
# с чужими лимитами и узнать об этом от игроков
try:
    reload_settings(PORT="восемь")
    check("1.7 кривое число отменяет старт", False)
except SystemExit as e:
    check("1.7 кривое число отменяет старт", "PORT" in str(e))
reload_settings(PORT=None)

print("\n=== 2. Секреты не попадают в логи ===")
s = reload_settings(BOT_TOKEN="123456789:SECRETSECRETSECRETtail",
                    REDIS_URL="redis://user:hunter2@10.0.0.5:6379/0",
                    WEBAPP_URL="https://example.com")
line = s.summary()
check("2.1 токена в сводке нет", "SECRETSECRET" not in line)
check("2.2 но видно, что он задан, и его хвост", "…tail" in line)
check("2.3 пароля Redis в сводке нет", "hunter2" not in line)
check("2.4 факт наличия Redis видно", "redis=on" in line)
check("2.5 пустой секрет читается как «не задан»",
      s.redact("") == "<не задан>")
s = reload_settings(REDIS_URL=None)
check("2.6 без Redis сводка честно говорит про фолбэк",
      "redis=off (in-process)" in s.summary())

print("\n=== 3. Фатальные комбинации конфига ===")
s = reload_settings(BOT_TOKEN=None)
check("3.1 без BOT_TOKEN старт отменяется",
      any("BOT_TOKEN" in t for t in fatals(s)))

s = reload_settings(ADMIN_ID="0", BOT_TOKEN="x:y")
check("3.2 без ADMIN_ID предупреждение, но не отказ",
      "ADMIN_ID" in texts(s) and not any("ADMIN_ID" in t for t in fatals(s)))

# DEV_MODE на боевом домене = бот сам рассылает в чат готовую подписанную
# initData, то есть доступ к любому аккаунту по ссылке
s = reload_settings(DEV_MODE="1", WEBAPP_URL="https://prod.example.com")
check("3.3 DEV_MODE на https-домене фатален",
      any("DEV_MODE" in t for t in fatals(s)))
s = reload_settings(DEV_MODE="1", WEBAPP_URL="http://127.0.0.1:8000")
check("3.4 DEV_MODE локально разрешён",
      not any("DEV_MODE" in t for t in fatals(s)))

s = reload_settings(DEBUG="1", WEBAPP_URL="https://prod.example.com")
check("3.5 DEBUG на https-домене фатален (наружу открыты /docs)",
      any("DEBUG" in t for t in fatals(s)))

s = reload_settings(BOT_MODE="pollling", DEBUG="0")
check("3.6 опечатка в BOT_MODE фатальна",
      any("BOT_MODE" in t for t in fatals(s)))
s = reload_settings(ROLE="worker", BOT_MODE="polling")
check("3.7 неизвестная ROLE фатальна", any("ROLE" in t for t in fatals(s)))

print("\n=== 4. Мульти-воркер: конфиг ловит то, что ломает состояние ===")
# лимитер и владение фоновыми задачами живут в памяти процесса: второй воркер
# удваивает лимит и запускает ролловер сезона поверх первого
s = reload_settings(WEB_CONCURRENCY="4", REDIS_URL=None, BOT_MODE="webhook",
                    WEBAPP_URL="https://prod.example.com", DEBUG="0",
                    DEV_MODE="0", ROLE="all")
check("4.1 воркеры без Redis — отказ",
      any("REDIS_URL" in t for t in fatals(s)))
s = reload_settings(WEB_CONCURRENCY="4", REDIS_URL="redis://localhost:6379/0",
                    BOT_MODE="polling")
check("4.2 воркеры с поллингом — отказ",
      any("polling" in t for t in fatals(s)))
s = reload_settings(WEB_CONCURRENCY="4", BOT_MODE="webhook",
                    REDIS_URL="redis://localhost:6379/0", DATABASE_URL=None)
check("4.3 воркеры на SQLite — предупреждение, а не отказ",
      "SQLite" in texts(s) and not any("SQLite" in t for t in fatals(s)))
s = reload_settings(WEB_CONCURRENCY="4", BOT_MODE="webhook", ROLE="api",
                    REDIS_URL="redis://localhost:6379/0",
                    DATABASE_URL="postgresql://u:p@localhost/cookie",
                    WEBAPP_URL="https://prod.example.com", ADMIN_ID="42",
                    DEBUG="0", DEV_MODE="0", BOT_TOKEN="123:AAA")
check(f"4.4 воркеры + Postgres + webhook + Redis — чисто ({texts(s) or 'нет'})",
      not s.problems())
check("4.5 в сводке видно движок базы, но не пароль",
      "db=postgres" in s.summary() and "p@localhost" not in s.summary())

print("\n=== 5. Webhook ===")
s = reload_settings(BOT_MODE="webhook", WEBAPP_URL="http://127.0.0.1:8000",
                    WEB_CONCURRENCY="1", WEBHOOK_BASE=None)
check("5.1 webhook на http — отказ (Telegram не примет setWebhook)",
      any("setWebhook" in t for t in fatals(s)))
s = reload_settings(BOT_MODE="webhook", WEBAPP_URL="http://127.0.0.1:8000",
                    WEBHOOK_BASE="https://tunnel.example.com")
check("5.2 WEBHOOK_BASE перебивает WEBAPP_URL", not fatals(s))
# лишняя обязательная переменная — это лишний шанс уехать в прод без неё,
# поэтому секрет по умолчанию выводится из токена
s = reload_settings(BOT_TOKEN="123:AAA", WEBHOOK_SECRET=None)
first = s.WEBHOOK_SECRET
s = reload_settings(BOT_TOKEN="123:AAA", WEBHOOK_SECRET=None)
check("5.3 секрет выводится из токена и стабилен",
      first and first == s.WEBHOOK_SECRET)
s2 = reload_settings(BOT_TOKEN="123:BBB", WEBHOOK_SECRET=None)
check("5.4 другой токен — другой секрет", s2.WEBHOOK_SECRET != first)
s = reload_settings(BOT_TOKEN="123:AAA", WEBHOOK_SECRET="explicit-secret")
check("5.5 явный секрет уважается", s.WEBHOOK_SECRET == "explicit-secret")
check("5.6 секрет в сводку не попадает",
      "explicit-secret" not in s.summary())

print("\n=== 6. Один источник правды ===")
# смысл шага: ни одного os.getenv вне settings. Иначе у ключа снова окажется
# два значения, а прочитан ли .env — снова будет зависеть от порядка импортов
import pathlib

leaks = []
for path in pathlib.Path(".").rglob("*.py"):
    parts = set(path.parts)
    if "__pycache__" in parts or path.name.startswith(("test_", "temp_")):
        continue
    if path.name in ("settings.py", "simplay.py", "balance_sim.py"):
        continue
    text = path.read_text(encoding="utf-8")
    for i, ln in enumerate(text.splitlines(), 1):
        if "os.getenv" in ln or "os.environ.get" in ln:
            # DATABASE_PATH — единственное исключение: тесты и симуляторы
            # подменяют его после импорта, снимок настроек был бы для них
            # путём к боевой базе
            if "DATABASE_PATH" in ln:
                continue
            leaks.append(f"{path}:{i}")
check(f"6.1 os.getenv только в settings.py (утечки: {leaks or 'нет'})", not leaks)
check("6.2 dotenv загружается ровно в одном месте",
      sum("load_dotenv(" in p.read_text(encoding="utf-8")
          for p in pathlib.Path(".").rglob("*.py")
          if "__pycache__" not in p.parts and not p.name.startswith("test_")) == 1)

# возвращаем модуль к настоящему окружению: следующие тесты в этом процессе
# должны видеть его, а не остатки подмены
importlib.reload(_settings)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
