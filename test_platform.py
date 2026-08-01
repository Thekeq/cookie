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
import time

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
DB_PATH = os.path.join(tempfile.gettempdir(),
                       f"cookie_platform_test_{os.getpid()}.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        os.remove(DB_PATH + _suffix)
os.environ["DATABASE_PATH"] = DB_PATH

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
# разделение ролей и поллинг несовместимы: поллинг поднимает только ROLE=all,
# иначе апдейты остаются без читателя, и снаружи это выглядит как «бот молчит»
s = reload_settings(ROLE="api", BOT_MODE="polling", BOT_TOKEN="123:AAA")
check("4.6 ROLE=api на поллинге — отказ (апдейты не тянет никто)",
      any("polling" in t for t in fatals(s)))

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

# возвращаем модуль к настоящему окружению: дальше работаем с ним
importlib.reload(_settings)

# ======================================================================
# Общее состояние: лимитер и владение задачей
# ======================================================================
import threading

from server import cache

# Один и тот же набор проверок прогоняется ДВА раза: на фолбэке в памяти и на
# Redis. Смысл слоя именно в том, что снаружи он ведёт себя одинаково — если
# поведение расходится, то на проде включение Redis тихо поменяет правила игры.
try:
    import fakeredis
except ImportError:
    fakeredis = None


def use_fallback():
    cache._reset_for_tests()
    _settings.REDIS_URL = ""
    cache._connect = cache.__dict__["_connect"]


def use_fake_redis():
    """Redis-путь на эмуляторе в процессе.

    Настоящего сервера в CI может не быть, а проверять надо именно то, что
    отправляется в Redis: порядок команд в пайплайне, семантику zcard и сверку
    токена при снятии замка."""
    cache._reset_for_tests()
    _settings.REDIS_URL = "redis://fake"
    fake = fakeredis.FakeRedis(decode_responses=True)
    cache._connect = lambda: fake
    return fake


def limiter_suite(tag: str):
    cache.reset_all_windows()
    key = f"t:{tag}"
    got = [cache.incr_window(key, 3, 60) for _ in range(5)]
    check(f"7.{tag}.1 первые три вызова разрешены",
          [a for a, _ in got[:3]] == [True, True, True])
    check(f"7.{tag}.2 четвёртый отбит", got[3][0] is False)
    # свой вызов считается даже за лимитом: иначе тот, кто продолжает долбить
    # ручку, освобождал бы себе окно, просто получая 429
    check(f"7.{tag}.3 счётчик растёт и после отказа", got[4][1] == 5)

    # окно скользящее: через window всё забывается
    cache.reset_window(key)
    check(f"7.{tag}.4 сброс окна возвращает право на запрос",
          cache.incr_window(key, 3, 60)[0] is True)

    # короткое окно реально истекает
    short = f"t:{tag}:short"
    cache.reset_window(short)
    cache.incr_window(short, 1, 0.3)
    check(f"7.{tag}.5 второй вызов в окне отбит",
          cache.incr_window(short, 1, 0.3)[0] is False)
    time.sleep(0.35)
    check(f"7.{tag}.6 после истечения окна снова можно",
          cache.incr_window(short, 1, 0.3)[0] is True)

    # ключи не путаются между игроками
    cache.reset_all_windows()
    cache.incr_window(f"state:{tag}:1", 1, 60)
    check(f"7.{tag}.7 окна разных ключей независимы",
          cache.incr_window(f"state:{tag}:2", 1, 60)[0] is True)


def lock_suite(tag: str):
    name = f"job:{tag}"
    with cache.lock(name, 60) as first:
        with cache.lock(name, 60) as second:
            check(f"8.{tag}.1 замок берёт только один", first and not second)
    with cache.lock(name, 60) as after:
        check(f"8.{tag}.2 после выхода замок свободен", after is True)

    # ttl — страховка от смерти владельца: убитый процесс не должен заблокировать
    # задачу навсегда. Входим/выходим вручную, потому что проверяется как раз
    # ПОРЯДОК: старый владелец выходит из блока уже ПОСЛЕ того, как замок забрал
    # новый, и не должен утащить чужой замок за собой
    holder = f"job:{tag}:ttl"
    a = cache.lock(holder, 1)
    a_mine = a.__enter__()
    time.sleep(1.15)                      # ttl истёк, владелец «умер»
    b = cache.lock(holder, 60)
    b_mine = b.__enter__()
    check(f"8.{tag}.3 просроченный замок переходит другому", a_mine and b_mine)
    a.__exit__(None, None, None)          # опоздавший выход прежнего владельца
    with cache.lock(holder, 60) as third:
        check(f"8.{tag}.4 чужой замок не снят прежним владельцем",
              third is False)
    b.__exit__(None, None, None)
    with cache.lock(holder, 60) as fourth:
        check(f"8.{tag}.5 после выхода настоящего владельца замок свободен",
              fourth is True)

    # параллельная гонка: ровно один победитель
    winners = []
    barrier = threading.Barrier(8)
    race_name = f"job:{tag}:race"

    def try_take():
        barrier.wait()
        with cache.lock(race_name, 30) as mine:
            if mine:
                winners.append(1)
                time.sleep(0.05)

    threads = [threading.Thread(target=try_take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check(f"8.{tag}.6 из восьми потоков задачу берёт один", len(winners) == 1)


print("\n=== 7. Лимитер (фолбэк в памяти) ===")
use_fallback()
check("7.0 без REDIS_URL работаем на памяти процесса", not cache.enabled())
check("7.0b health честно говорит про фолбэк",
      cache.health() == {"backend": "in-process"})
limiter_suite("mem")

print("\n=== 8. Владение задачей (фолбэк в памяти) ===")
lock_suite("mem")

if fakeredis is None:
    print("\n=== 7b/8b. Redis-путь ПРОПУЩЕН: нет fakeredis "
          "(pip install fakeredis) ===")
else:
    print("\n=== 7b. Лимитер (Redis) ===")
    use_fake_redis()
    check("7b.0 с REDIS_URL работаем на общем состоянии", cache.enabled())
    check("7b.0b health показывает redis", cache.health()["backend"] == "redis")
    limiter_suite("redis")
    print("\n=== 8b. Владение задачей (Redis) ===")
    lock_suite("redis")

    print("\n=== 9. Деградация Redis ===")

    class Broken:
        """Redis, который отвечает ошибкой на всё."""

        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionError("redis is down")
            return boom

    cache._reset_for_tests()
    _settings.REDIS_URL = "redis://fake"
    cache._connect = lambda: Broken()
    # лимитер — fail open: моргнувший Redis не должен закрыть игру ВСЕМ
    # игрокам сразу. Пропустить лишний запрос дешевле массового 429
    allowed, _ = cache.incr_window("t:down", 5, 60)
    check("9.1 упавший Redis не блокирует игру (fail open)", allowed is True)
    # ...но лимит при этом продолжает работать на памяти процесса, а не
    # исчезает вовсе
    for _ in range(5):
        last = cache.incr_window("t:down", 5, 60)
    check("9.2 на фолбэке лимит всё равно считается", last[0] is False)
    # замок — fail closed: единственность владельца проверить нечем, а
    # ролловер сезона, запущенный дважды, стоит пересчёта всем игрокам
    with cache.lock("job:down", 60) as mine:
        check("9.3 упавший Redis не выдаёт замок (fail closed)", mine is False)
    check("9.4 health показывает, что Redis лежит",
          cache.health().get("redis", "").startswith("down"))

    print("\n=== 10. Redis без EVAL ===")

    class NoEval(fakeredis.FakeRedis):
        """Managed-Redis с урезанным набором команд: EVAL запрещён."""

        def eval(self, *a, **k):
            raise Exception("ERR unknown command 'EVAL'")

    cache._reset_for_tests()
    _settings.REDIS_URL = "redis://fake"
    cache._connect = lambda: NoEval(decode_responses=True)
    with cache.lock("job:noeval", 60) as mine:
        check("10.1 замок берётся", mine is True)
    with cache.lock("job:noeval", 60) as again:
        check("10.2 и снимается без EVAL (иначе он не снялся бы никогда)",
              again is True)

# ======================================================================
# Планировщик: у каждой фоновой задачи один владелец и общее расписание
# ======================================================================
print("\n=== 11. Планировщик: расписание в БД, владелец один ===")
use_fallback()

import db as db_module

from server import scheduler

scheduler.reset()
took = []
for _ in range(3):
    with scheduler.job("t:job", 0.4, 60) as mine:
        took.append(mine)
check("11.1 первый запуск сразу, внутри интервала — отказ",
      took == [True, False, False])
time.sleep(0.45)
with scheduler.job("t:job", 0.4, 60) as mine:
    check("11.2 после интервала работа снова выдаётся", mine is True)

# Главный случай, ради которого нужен замок, а не только отметка в БД:
# пуш-проход спит 0.05 с на игрока, и на сотне тысяч аккаунтов один проход
# переживает свой же интервал. Интервал уже прошёл, но работа ещё идёт —
# начинать вторую нельзя, иначе игрок получит два сообщения.
scheduler.reset()
with scheduler.job("t:job:slow", 0.1, 60) as outer:
    time.sleep(0.15)
    with scheduler.job("t:job:slow", 0.1, 60) as inner:
        check("11.3 пока работа идёт, вторую не начинают",
              outer is True and inner is False)

# Отметка живёт в БД, а не в памяти: у бэкапа она была модульной переменной, и
# цикл перезапусков снимал полную копию базы на каждом старте
scheduler.reset()
with scheduler.job("t:job:restart", 60, 60) as mine:
    check("11.4 задача отработала", mine is True)
cache._reset_for_tests()          # «рестарт»: память процесса чистая
use_fallback()
with scheduler.job("t:job:restart", 60, 60) as mine:
    check("11.5 расписание переживает рестарт", mine is False)

# Падение задачи не должно выглядеть как успех: вызывающий обязан узнать, а в
# кластере лог мог остаться в уже сменившемся процессе — поэтому ещё и в БД
scheduler.reset()
try:
    with scheduler.job("t:job:boom", 60, 60) as mine:
        if mine:
            raise RuntimeError("сломалось")
    check("11.6 исключение из задачи не глотается", False)
except RuntimeError:
    check("11.6 исключение из задачи не глотается", True)
_row = db_module.shared().q1(
    "SELECT fails, last_error, last_ok_at FROM job_runs WHERE job_key = ?",
    ("t:job:boom",))
check("11.7 падение записано в журнал задач",
      _row["fails"] == 1 and "сломалось" in _row["last_error"]
      and _row["last_ok_at"] == 0)

# Реальное расписание нотификатора, один проход целиком: юнит-проверки
# примитива не видят опечатку в самом списке задач (ключ, период, функция), а
# заметить её на проде можно только по тому, что работа не делается.
from bot import notifier

scheduler.reset()
_done = []
for _key, _interval, _ttl, _work in notifier.JOBS:
    with scheduler.job(_key, _interval, _ttl) as mine:
        if mine:
            _work()
            _done.append(_key)
check(f"11.8 весь список задач нотификатора проходит тик ({len(_done)} шт.)",
      _done == [k for k, *_ in notifier.JOBS])
check("11.9 после тика ни одна задача не в ошибке",
      "failing" not in scheduler.health())
# бэкап реально снял копию временной базы — уносим её за собой
_backups = os.path.join(os.path.dirname(DB_PATH), "backups")
for _name in os.listdir(_backups) if os.path.isdir(_backups) else []:
    if _name.startswith(os.path.basename(DB_PATH)):
        os.remove(os.path.join(_backups, _name))

print("\n=== 12. Здоровье планировщика видно снаружи ===")
scheduler.reset()
check("12.1 пустое расписание не притворяется рабочим",
      scheduler.health()["jobs"] == 0)
with scheduler.job("t:h", 60, 60):
    pass
_h = scheduler.health()
check("12.2 отработавшая задача попадает в сводку",
      _h["jobs"] == 1 and _h["last_ok_age"] < 5)
check("12.3 свежая задача не считается просроченной", "stale" not in _h)
db_module.shared().exec(
    "UPDATE job_runs SET last_ok_at = ? WHERE job_key = ?",
    (time.time() - 400, "t:h"))
check("12.4 молчащая задача видна как stale (иначе «бэкапов нет неделю» "
      "выглядит как здоровье)", scheduler.health().get("stale") == ["t:h"])
scheduler.reset()

print("\n=== 13. Роль решает, что поднимает процесс ===")
from fastapi.testclient import TestClient

import main as main_module


def role_tasks(role: str) -> list[str]:
    """Какие задачи поднял бы процесс с этой ролью.

    Корутины закрываем сразу: запускать здесь ни поллинг, ни uvicorn не нужно —
    проверяется только состав."""
    saved = _settings.ROLE
    _settings.ROLE = role
    try:
        jobs = main_module.tasks_for_role()
        names = sorted(j.cr_code.co_name for j in jobs)
    finally:
        _settings.ROLE = saved
    for j in jobs:
        j.close()
    return names


check("13.1 ROLE=all — бот, API и планировщик в одном процессе",
      role_tasks("all") == ["run_api", "run_bot", "run_notifier"])
# воркер API не должен вести фоновые задачи: их владелец один на кластер
check("13.2 ROLE=api — только HTTP", role_tasks("api") == ["run_api"])
check("13.3 ROLE=scheduler — только фоновые задачи",
      role_tasks("scheduler") == ["run_notifier"])

_r = TestClient(main_module.app).get("/healthz")
_j = _r.json()
check("13.4 /healthz отвечает 200", _r.status_code == 200 and _j["ok"] is True)
check("13.5 в /healthz видно роль, кеш и планировщик",
      _j["role"] and "backend" in _j["cache"] and "jobs" in _j["scheduler"])

use_fallback()
importlib.reload(_settings)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
