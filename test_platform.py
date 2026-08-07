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
# Мастер-процесс только следит за детьми, а дети поднимают ТОЛЬКО ASGI —
# tasks_for_role не выполняется ни в одном из них. С ROLE=all это означает,
# что бэкапа, ролловера и пушей нет нигде, и снаружи сервис выглядит здоровым
s = reload_settings(WEB_CONCURRENCY="4", ROLE="all", BOT_MODE="webhook",
                    REDIS_URL="redis://localhost:6379/0",
                    DATABASE_URL="postgresql://u:p@localhost/cookie",
                    WEBAPP_URL="https://prod.example.com", BOT_TOKEN="123:AAA")
check("4.7 воркеры при ROLE=all — отказ (фоновые задачи не выполнит никто)",
      any("ROLE=all" in t and "фоновые" in t for t in fatals(s)))
s = reload_settings(WEB_CONCURRENCY="4", ROLE="scheduler", BOT_MODE="webhook",
                    REDIS_URL="redis://localhost:6379/0")
check("4.8 воркеры при ROLE=scheduler — тоже отказ",
      any("ROLE=scheduler" in t for t in fatals(s)))

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
# маршрут вешается при импорте main: без слеша это трейсбек из Starlette на
# старте вместо внятной причины
s = reload_settings(BOT_MODE="webhook", WEBHOOK_BASE="https://prod.example.com",
                    WEBHOOK_PATH="tg/webhook")
check("5.7 WEBHOOK_PATH без слеша — отказ",
      any("WEBHOOK_PATH" in t for t in fatals(s)))
s = reload_settings(WEBHOOK_PATH=None, BOT_MODE="polling", WEBHOOK_BASE=None)

print("\n=== 6. Один источник правды ===")
# смысл шага: ни одного os.getenv вне settings. Иначе у ключа снова окажется
# два значения, а прочитан ли .env — снова будет зависеть от порядка импортов
import pathlib

leaks = []
for path in pathlib.Path(".").rglob("*.py"):
    parts = set(path.parts)
    if "__pycache__" in parts or path.name.startswith(("test_", "temp_")):
        continue
    # deploy/ — не приложение, а инструменты вокруг него. Скрипт выкладки
    # работает РОВНО в тот момент, когда код приложения подменяется, и импорт
    # server.settings означал бы, что выкладка падает от поломки в том, что она
    # выкатывает. Свои переменные он читает сам и из окружения юнита.
    if "deploy" in parts:
        continue
    if path.name in ("settings.py", "simplay.py", "balance_sim.py"):
        continue
    text = path.read_text(encoding="utf-8")
    for i, ln in enumerate(text.splitlines(), 1):
        if "os.getenv" in ln or "os.environ.get" in ln:
            # Адрес базы — единственное исключение: тесты и симуляторы
            # подменяют его после импорта, снимок настроек был бы для них
            # путём к боевой базе
            if "DATABASE_PATH" in ln or "DATABASE_URL" in ln:
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


def role_tasks(role: str, mode: str = "polling") -> list[str]:
    """Какие задачи поднял бы процесс с этой ролью и этим режимом бота.

    Корутины закрываем сразу: запускать здесь ни поллинг, ни uvicorn не нужно —
    проверяется только состав."""
    saved = (_settings.ROLE, _settings.BOT_MODE)
    _settings.ROLE, _settings.BOT_MODE = role, mode
    try:
        jobs = main_module.tasks_for_role()
        names = sorted(j.cr_code.co_name for j in jobs)
    finally:
        _settings.ROLE, _settings.BOT_MODE = saved
    for j in jobs:
        j.close()
    return names


check("13.1 ROLE=all — бот, API и планировщик в одном процессе",
      role_tasks("all") == ["run_api", "run_bot", "run_notifier"])
# воркер API не должен вести фоновые задачи: их владелец один на кластер
check("13.2 ROLE=api — только HTTP", role_tasks("api") == ["run_api"])
check("13.3 ROLE=scheduler — только фоновые задачи",
      role_tasks("scheduler") == ["run_notifier"])
# в webhook-режиме отдельной задачи под бота нет: апдейты приходят обычными
# запросами в API. Раньше поллинг поднимался всегда и внутри звал
# delete_webhook — то есть BOT_MODE=webhook в конфиге ничего не менял
check("13.6 BOT_MODE=webhook — поллинг не поднимается",
      role_tasks("all", "webhook") == ["run_api", "run_notifier"])

_r = TestClient(main_module.app).get("/healthz")
_j = _r.json()
check("13.4 /healthz отвечает 200", _r.status_code == 200 and _j["ok"] is True)
check("13.5 в /healthz видно роль, кеш и планировщик",
      _j["role"] and "backend" in _j["cache"] and "jobs" in _j["scheduler"])

print("\n=== 14. Webhook: маршрут, секрет, регистрация ===")
import asyncio
import pathlib as _pathlib
from types import SimpleNamespace

from fastapi import FastAPI

from bot import webhook

UPDATE = {"update_id": 77,
          "message": {"message_id": 1, "date": 0,
                      "chat": {"id": 5, "type": "private"},
                      "from": {"id": 5, "is_bot": False, "first_name": "T"},
                      "text": "/start"}}


class FakeDp:
    """Диспетчер-заглушка: проверяем маршрут, а не игровые хендлеры."""

    def __init__(self, boom=False):
        self.seen = []
        self.boom = boom

    def include_router(self, router):
        pass

    def resolve_used_update_types(self):
        return ["message", "pre_checkout_query"]

    async def feed_update(self, bot, update):
        self.seen.append(update.update_id)
        if self.boom:
            raise RuntimeError("хендлер сломался")


class FakeBot:
    """Telegram-заглушка: что именно бот у него спросил и что установил."""

    def __init__(self, url=""):
        self.info_url = url
        self.calls = []

    async def get_webhook_info(self):
        return SimpleNamespace(url=self.info_url)

    async def set_webhook(self, url, **kw):
        self.calls.append((url, kw))
        self.info_url = url


def hook_app(mode="webhook", path="/tg/test-hook", secret="s3cret"):
    """Приложение с одним маршрутом webhook'а и подменённым диспетчером."""
    _settings.BOT_MODE = mode
    _settings.WEBHOOK_PATH = path
    _settings.WEBHOOK_SECRET = secret
    app = FastAPI()
    added = webhook.install(app)
    return app, added


_saved_hook = (_settings.BOT_MODE, _settings.WEBHOOK_PATH,
               _settings.WEBHOOK_SECRET, _settings.WEBHOOK_BASE, webhook.dp)

# на поллинге ручки быть не должно: открытый маршрут, кормящий диспетчер,
# не нужен там, где апдейты забирает сам бот
_app, _added = hook_app(mode="polling")
check("14.1 при BOT_MODE=polling маршрут не регистрируется",
      _added is False
      and not any(getattr(r, "path", "") == "/tg/test-hook"
                  for r in _app.routes))

_app, _added = hook_app()
_fake = FakeDp()
webhook.dp = _fake
_client = TestClient(_app)
check("14.2 при BOT_MODE=webhook маршрут появился", _added is True)

# Секрет — единственная настоящая защита: путь утекает в логи прокси, а по
# подделанному successful_payment бот выдал бы товар бесплатно
_r = _client.post("/tg/test-hook", json=UPDATE)
check("14.3 без секретного заголовка — 403",
      _r.status_code == 403 and _fake.seen == [])
_r = _client.post("/tg/test-hook", json=UPDATE,
                  headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret-almost"})
check("14.4 с чужим секретом — 403", _r.status_code == 403 and _fake.seen == [])
check("14.5 ответ 403 не подсказывает, чем секрет не подошёл",
      "s3cret" not in _r.text and "secret" not in _r.json()["detail"].lower())

_hdr = {"X-Telegram-Bot-Api-Secret-Token": "s3cret"}
_r = _client.post("/tg/test-hook", json=UPDATE, headers=_hdr)
check("14.6 со своим секретом апдейт доходит до диспетчера",
      _r.status_code == 200 and _fake.seen == [77])

# Ненулевой код = Telegram присылает тот же апдейт снова. На кривом теле это
# был бы бесконечный поток повторов, а разбирать его всё равно нечем
_r = _client.post("/tg/test-hook", content=b"{not json", headers=_hdr)
check("14.7 мусор в теле — 200, но диспетчеру не отдан",
      _r.status_code == 200 and _fake.seen == [77])
_r = _client.post("/tg/test-hook", json={"no_update_id": 1}, headers=_hdr)
check("14.8 апдейт без update_id — 200 и не отдан",
      _r.status_code == 200 and _fake.seen == [77])

webhook.dp = FakeDp(boom=True)
_r = _client.post("/tg/test-hook", json=UPDATE, headers=_hdr)
check("14.9 упавший хендлер не превращается в поток повторов",
      _r.status_code == 200)

# compare_digest("", "") пропускает ЛЮБОЙ запрос — пустой секрет обязан
# закрывать ручку, а не открывать её всем
_app2, _ = hook_app(path="/tg/test-hook2", secret="")
webhook.dp = FakeDp()
_r = TestClient(_app2).post("/tg/test-hook2", json=UPDATE)
check("14.10 пустой секрет закрывает ручку, а не открывает",
      _r.status_code == 503 and webhook.dp.seen == [])

print("\n=== 15. Webhook: адрес и регистрация в Telegram ===")
_settings.WEBHOOK_PATH = "/tg/hook"
_settings.WEBHOOK_BASE = "https://prod.example.com/"
check("15.1 адрес собирается без двойного слеша",
      webhook.url() == "https://prod.example.com/tg/hook")
_settings.WEBHOOK_BASE = ""
check("15.2 без адреса регистрировать нечего", webhook.url() == "")
_settings.WEBHOOK_BASE = "https://prod.example.com"
_settings.WEBHOOK_SECRET = "s3cret"
webhook.dp = FakeDp()

_b = FakeBot(url="")
check("15.3 адрес не выставлен — регистрируем",
      asyncio.run(webhook.ensure_registered(_b)) == "установлен"
      and len(_b.calls) == 1)
_url, _kw = _b.calls[0]
check("15.4 setWebhook получает адрес и секрет",
      _url == "https://prod.example.com/tg/hook"
      and _kw["secret_token"] == "s3cret")
# за время деплоя игрок мог нажать /start или оплатить — терять это нельзя
check("15.5 накопленные апдейты не сбрасываются",
      _kw["drop_pending_updates"] is False)
# список типов берётся у диспетчера: руками его пришлось бы править на каждый
# новый хендлер, и забытая правка ломает бота МОЛЧА
check("15.6 типы апдейтов берутся у диспетчера",
      set(_kw["allowed_updates"]) == {"message", "pre_checkout_query"})

check("15.7 уже настроенный webhook не переустанавливается",
      asyncio.run(webhook.ensure_registered(_b)) == "уже настроен"
      and len(_b.calls) == 1)
# чужой адрес = либо старый деплой, либо кто-то поднял копию бота на поллинге и
# снял боевой webhook. Такое молчание находится только по жалобам игроков
_b2 = FakeBot(url="https://old.example.com/tg/hook")
check("15.8 сбитый адрес возвращается на место",
      asyncio.run(webhook.ensure_registered(_b2)) == "установлен"
      and _b2.calls[0][0] == "https://prod.example.com/tg/hook")
_settings.WEBHOOK_BASE = ""
check("15.9 без адреса не зовём setWebhook вслепую",
      asyncio.run(webhook.ensure_registered(FakeBot())) == "нет адреса")

# Настоящий диспетчер должен знать про оба типа апдейтов, которые есть у бота:
# на заглушках этого не видно, а без pre_checkout_query не пройдёт ни один
# платёж — Telegram просто не пришлёт запрос
webhook.dp = _saved_hook[4]
webhook.setup_dispatcher()
_types = set(webhook.dp.resolve_used_update_types())
check(f"15.10 реальный диспетчер просит нужные типы ({sorted(_types)})",
      {"message", "pre_checkout_query"} <= _types)
webhook.setup_dispatcher()          # второй вызов не должен падать
check("15.11 повторная регистрация хендлеров идемпотентна", True)

# Starlette проверяет маршруты по порядку регистрации, а Mount("/") совпадает с
# любым путём: install ниже монтирования статики означает 404 на все апдейты
_main_src = _pathlib.Path("main.py").read_text(encoding="utf-8")
check("15.12 webhook.install идёт до монтирования статики в корень",
      0 < _main_src.index("webhook.install(app)")
      < _main_src.index('app.mount("/"'))

(_settings.BOT_MODE, _settings.WEBHOOK_PATH, _settings.WEBHOOK_SECRET,
 _settings.WEBHOOK_BASE, webhook.dp) = _saved_hook

print("\n=== 16. Воркеры API и отдельный планировщик ===")

# Мастер-процесс блокирующий: Multiprocess вешает обработчики сигналов и ждёт
# детей в главном потоке. Поэтому развилка стоит ДО asyncio.run, а не внутри
# задачи — иначе блокирующий цикл встал бы рядом с событийным.
_seen = {}


def _fake_workers():
    _seen["workers"] = True


def _fake_asyncio_run(coro):
    _seen["asyncio"] = True
    coro.close()


_saved_run = (main_module.serve_api_workers, main_module.asyncio.run,
              main_module.preflight, _settings.WEB_CONCURRENCY)
main_module.serve_api_workers = _fake_workers
main_module.asyncio.run = _fake_asyncio_run
main_module.preflight = lambda: None
try:
    _settings.WEB_CONCURRENCY = 1
    main_module.run()
    check("16.1 один воркер — обычный процесс с задачами роли",
          _seen == {"asyncio": True})
    _seen.clear()
    _settings.WEB_CONCURRENCY = 4
    main_module.run()
    check("16.2 несколько воркеров — мастер-процесс вместо цикла задач",
          _seen == {"workers": True})
finally:
    (main_module.serve_api_workers, main_module.asyncio.run,
     main_module.preflight, _settings.WEB_CONCURRENCY) = _saved_run

# Дети создаются fork/spawn, и объект приложения такого не переживает: на
# spawn-платформах его пришлось бы сериализовать. Строку каждый ребёнок
# импортирует у себя сам.
_cfg = {}
_real_config = main_module.uvicorn.Config


class _FakeSock:
    closed = False

    def close(self):
        self.closed = True


class _FakeConfig:
    def __init__(self, app, **kw):
        _cfg["app"], _cfg["kw"] = app, kw
        self.workers = kw.get("workers")
        self.sock = _FakeSock()

    def bind_socket(self):
        return self.sock


class _FakeMulti:
    def __init__(self, config, sockets):
        _cfg["sockets"] = sockets

    def run(self):
        _cfg["ran"] = True


import uvicorn.supervisors as _sup

_saved_multi = _sup.Multiprocess
main_module.uvicorn.Config = _FakeConfig
_sup.Multiprocess = _FakeMulti
_saved_conc = _settings.WEB_CONCURRENCY
try:
    _settings.WEB_CONCURRENCY = 4
    main_module.serve_api_workers()
finally:
    main_module.uvicorn.Config = _real_config
    _sup.Multiprocess = _saved_multi
    _settings.WEB_CONCURRENCY = _saved_conc

check("16.3 приложение передаётся строкой импорта, а не объектом",
      _cfg["app"] == "main:app")
check(f"16.4 число воркеров берётся из настроек ({_cfg['kw'].get('workers')})",
      _cfg["kw"]["workers"] == 4)
# Сокет открывает мастер и раздаёт детям: порт занят один раз, соединения
# раскладывает ядро — ни второго порта, ни балансировщика не нужно
check("16.5 все воркеры слушают ОДИН сокет мастера",
      _cfg["ran"] and len(_cfg["sockets"]) == 1)
check("16.6 сокет закрывается после остановки мастера",
      _cfg["sockets"][0].closed)

# Ротация — это переименование файла. N процессов ротируют его каждый по
# своему счётчику, и часть записей уходит в уже переименованный файл.
_main_src = _pathlib.Path("main.py").read_text(encoding="utf-8")
check("16.7 при нескольких воркерах у каждого процесса свой файл лога",
      "WEB_CONCURRENCY > 1" in _main_src and "os.getpid()" in _main_src
      and _main_src.index("LOG_FILE = settings.LOG_FILE")
      < _main_src.index("os.getpid()"))

_units = {name: _pathlib.Path("deploy", name).read_text(encoding="utf-8")
          for name in ("cookie-api.service", "cookie-scheduler.service")}
check("16.8 юнит API объявляет ROLE=api",
      "Environment=ROLE=api" in _units["cookie-api.service"])
# планировщик обязан быть один: .env общий на оба юнита, и WEB_CONCURRENCY из
# него утёк бы сюда, а с ним старт отменяется правилом 4.8
check("16.9 юнит планировщика объявляет ROLE=scheduler и один воркер",
      "Environment=ROLE=scheduler" in _units["cookie-scheduler.service"]
      and "Environment=WEB_CONCURRENCY=1" in _units["cookie-scheduler.service"])
check("16.10 оба юнита перезапускаются сами (падение задачи роняет процесс)",
      all("Restart=always" in u for u in _units.values()))
check("16.11 оба юнита работают не от root",
      all("User=cookie" in u for u in _units.values()))
# на остановке может идти pg_dump или проход пушей — их нельзя рубить по
# дефолтным 90 секундам молча
check("16.12 у планировщика запас на остановку больше, чем у API",
      "TimeoutStopSec=120" in _units["cookie-scheduler.service"])

print("\n=== 17. Наблюдаемость: корреляция, метрики, живость/готовность ===")
import json as _json
import logging as _logging

from server import obs

# --- конфиг ---
s = reload_settings(LOG_JSON="1", METRICS_TOKEN="x" * 32, SENTRY_DSN="https://k@o.io/1",
                    GRACEFUL_TIMEOUT="35")
check("17.1 новые ключи читаются",
      s.LOG_JSON is True and s.GRACEFUL_TIMEOUT == 35
      and s.METRICS_TOKEN == "x" * 32)
check("17.2 в сводке видно режим логов, метрики и sentry",
      "logs=json" in s.summary() and "metrics=on" in s.summary()
      and "sentry=on" in s.summary())
# токен и DSN — такие же секреты, как BOT_TOKEN: сводка печатается в лог на
# старте, и попадание их туда равносильно публикации
check("17.3 токен метрик и DSN не печатаются целиком",
      "x" * 32 not in s.summary() and "https://k@o.io/1" not in s.summary()
      and "METRICS_TOKEN" in s._SECRET_KEYS and "SENTRY_DSN" in s._SECRET_KEYS)
# короткий токен подбирается перебором, а за ним обороты валюты и список ручек
check("17.4 короткий METRICS_TOKEN — фатальная ошибка конфига",
      any("METRICS_TOKEN" in t for t in fatals(reload_settings(METRICS_TOKEN="short"))))
check("17.5 пустой METRICS_TOKEN претензий не вызывает",
      not any("METRICS_TOKEN" in t for t in texts(reload_settings(METRICS_TOKEN=None))))
importlib.reload(_settings)

# --- идентификатор запроса ---
check("17.6 свой идентификатор выдаётся, когда чужого нет",
      len(obs.new_request_id("")) == 16 and obs.new_request_id() != obs.new_request_id())
check("17.7 чужой идентификатор берётся как есть",
      obs.new_request_id("abc-123.XY") == "abc-123.XY")
# идентификатор идёт в строку лога: перевод строки в нём — это подделка
# соседней записи в журнале, а пробел с кавычкой — подделка поля
check("17.8 из чужого идентификатора вычищается всё, кроме безопасного",
      obs.new_request_id('a b"\nc') == "abc"
      and len(obs.new_request_id("z" * 200)) == 64)

_tok = obs.bind_request("rq-1", 0)
obs.bind_user(555)
_rec = _logging.LogRecord("t", _logging.INFO, "f", 1, "привет %s", ("мир",), None)
obs.ContextFilter().filter(_rec)
check("17.9 фильтр подставляет запрос, игрока, роль и pid",
      _rec.req_id == "rq-1" and _rec.user_id == 555
      and _rec.role == _settings.ROLE and _rec.pid == os.getpid())
_line = _json.loads(obs.JsonFormatter().format(_rec))
check("17.10 JSON-лог: одна строка — один объект с сообщением и контекстом",
      _line["msg"] == "привет мир" and _line["req_id"] == "rq-1"
      and _line["user_id"] == 555 and _line["level"] == "INFO")
try:
    raise ValueError("бум")
except ValueError:
    _rec2 = _logging.LogRecord("t", _logging.ERROR, "f", 1, "упало", (),
                               sys.exc_info())
obs.ContextFilter().filter(_rec2)
check("17.11 трейсбек попадает в JSON отдельным полем",
      "ValueError: бум" in _json.loads(obs.JsonFormatter().format(_rec2))["exc"])
obs.reset_request(_tok)
_rec3 = _logging.LogRecord("t", 20, "f", 1, "x", (), None)
obs.ContextFilter().filter(_rec3)
# contextvar без сброса живёт до конца задачи, и фоновая строка ушла бы в лог
# с идентификатором чужого запроса — хуже, чем совсем без него
check("17.12 после сброса контекст не течёт в фоновые строки",
      obs.current_request_id() == "" and _rec3.req_id == ""
      and _rec3.user_id == 0
      and "req_id" not in _json.loads(obs.JsonFormatter().format(_rec3)))

# --- арифметика метрик ---
use_fallback()
obs.reset_shared()
obs.inc("http_requests_total", method="GET", path="/api/state", status=200)
obs.inc("http_requests_total", 2, method="GET", path="/api/state", status=200)
obs.inc("http_requests_total", method="GET", path="/api/state", status=500)
_txt = obs.render()
check("17.13 счётчик складывается по одинаковым меткам",
      'http_requests_total{method="GET",path="/api/state",status="200"} 3' in _txt)
check("17.14 разные метки — разные ряды",
      'http_requests_total{method="GET",path="/api/state",status="500"} 1' in _txt)
check("17.15 в выгрузке есть HELP и TYPE",
      "# HELP http_requests_total" in _txt
      and "# TYPE http_requests_total counter" in _txt)
check("17.16 метрика без наблюдений в выгрузку не попадает",
      "notifications_total" not in _txt)

obs.reset_shared()
for _v in (0.001, 0.03, 7.0, 60.0):
    obs.observe("http_request_duration_seconds", _v, method="GET", path="/x")
_txt = obs.render()
_b = {}
for _l in _txt.splitlines():
    _m = _l.startswith("http_request_duration_seconds_bucket")
    if _m:
        _b[_l.split('le="')[1].split('"')[0]] = float(_l.rsplit(" ", 1)[1])
# корзины накопительные: наблюдение попадает во все границы не меньше себя,
# иначе p99 в Prometheus считается по мусору
check("17.17 корзины гистограммы накопительные",
      _b["0.005"] == 1 and _b["0.05"] == 2 and _b["10"] == 3 and _b["+Inf"] == 4)
check("17.18 сумма и число наблюдений сходятся",
      "http_request_duration_seconds_count{method=\"GET\",path=\"/x\"} 4" in _txt
      and "_sum{method=\"GET\",path=\"/x\"} 67.031" in _txt)

obs.reset_shared()
obs.add_gauge("http_requests_in_flight", 1)
obs.add_gauge("http_requests_in_flight", 1)
obs.add_gauge("http_requests_in_flight", -1)
check("17.19 датчик ходит в обе стороны",
      "http_requests_in_flight 1" in obs.render())
obs.set_gauge("http_requests_in_flight", 0)

# --- сведение по процессам ---
if fakeredis:
    _fake = use_fake_redis()
    obs.reset_shared()
    obs.inc("notifications_total", 5, result="sent")
    obs.observe("db_query_seconds", 0.2, op="read")
    check("17.20 первая досылка уходит в redis", obs.flush() is True)
    check("17.21 повторная досылка не дублирует прирост",
          obs.flush() is True
          and "notifications_total{result=\"sent\"} 5" in obs.render())
    # второй процесс: своя память, тот же redis. Именно ради этого метрики и
    # сводятся — scrape приходит в случайный воркер из шести
    _mem_c, _mem_h = dict(obs._counters), dict(obs._hists)
    _sent_c, _sent_h = dict(obs._sent_counters), dict(obs._sent_hists)
    obs._counters.clear(), obs._hists.clear()
    obs._sent_counters.clear(), obs._sent_hists.clear()
    obs.inc("notifications_total", 3, result="sent")
    obs.observe("db_query_seconds", 0.3, op="read")
    _txt = obs.render()
    check("17.22 /metrics отдаёт сумму по всем процессам, а не 1/N",
          'notifications_total{result="sent"} 8' in _txt)
    check("17.23 гистограммы тоже складываются между процессами",
          'db_query_seconds_count{op="read"} 2' in _txt
          and 'db_query_seconds_sum{op="read"} 0.5' in _txt)
    # моргнувший redis не должен съедать прирост навсегда: он не списывается,
    # пока не подтверждён
    obs.reset_shared()
    obs.inc("notifications_total", 4, result="failed")
    _real_pipe = _fake.pipeline

    def _boom(*a, **k):
        raise RuntimeError("redis лёг")

    _fake.pipeline = _boom
    check("17.24 отказ redis не роняет процесс и виден как False",
          obs.flush() is False)
    _fake.pipeline = _real_pipe
    check("17.25 после возвращения redis потерянный прирост доезжает",
          obs.flush() is True
          and 'notifications_total{result="failed"} 4' in obs.render())
    obs.reset_shared()
    obs._counters.update(_mem_c), obs._hists.update(_mem_h)
    obs._sent_counters.update(_sent_c), obs._sent_hists.update(_sent_h)
    use_fallback()
else:
    print("  --  17.20-17.25 пропущены: нет fakeredis")

# --- ручки ---
obs.reset_shared()
_c = TestClient(main_module.app)
_r = _c.get("/livez")
check("17.26 /livez отвечает 200 и называет процесс",
      _r.status_code == 200 and _r.json()["pid"] == os.getpid())
_r = _c.get("/readyz")
check("17.27 /readyz отвечает 200 при живой базе",
      _r.status_code == 200 and _r.json()["db"] == "up")

_saved_q1 = db_module.DataBase.q1
db_module.DataBase.q1 = lambda self, *a, **k: (_ for _ in ()).throw(
    RuntimeError("нет соединения"))
try:
    _r = _c.get("/readyz")
finally:
    db_module.DataBase.q1 = _saved_q1
# 503 обязан быть КОДОМ: балансировщик читает код, «200 {ok: false}» для него
# здоровый процесс
check("17.28 упавшая база — это 503, а не 200 с полем",
      _r.status_code == 503 and _r.json()["ok"] is False)
check("17.29 после возвращения базы готовность возвращается",
      _c.get("/readyz").status_code == 200)

_saved_token = _settings.METRICS_TOKEN
try:
    _settings.METRICS_TOKEN = ""
    # 404, а не 401: «сюда нужен пароль» подтверждает, что тут есть что смотреть
    check("17.30 без токена в конфиге ручки метрик нет вовсе",
          _c.get("/metrics").status_code == 404)
    _settings.METRICS_TOKEN = "t" * 32
    check("17.31 чужой токен отбивается",
          _c.get("/metrics", headers={"Authorization": "Bearer wrong"}
                 ).status_code == 401)
    check("17.32 без заголовка вовсе — тоже отказ",
          _c.get("/metrics").status_code == 401)
    _r = _c.get("/metrics", headers={"Authorization": "Bearer " + "t" * 32})
    check("17.33 с токеном отдаётся текст в формате Prometheus",
          _r.status_code == 200
          and _r.headers["content-type"].startswith("text/plain")
          and "# TYPE http_requests_total counter" in _r.text)
finally:
    _settings.METRICS_TOKEN = _saved_token

# --- проводка ---
check("17.34 у каждого ответа есть свой идентификатор в заголовке",
      _c.get("/livez").headers.get("X-Request-Id")
      != _c.get("/livez").headers.get("X-Request-Id"))
check("17.35 присланный идентификатор возвращается тем же",
      _c.get("/livez", headers={"X-Request-Id": "trace-9"}
             ).headers["X-Request-Id"] == "trace-9")
_txt = obs.render()
# метка пути — ШАБЛОН маршрута, а не сам путь: иначе /api/user/123 заводит по
# ряду на игрока, и хранилище метрик ложится раньше базы
check("17.36 запросы считаются по шаблону маршрута",
      'path="/livez"' in _txt and 'method="GET"' in _txt)
check("17.37 время запроса пишется в гистограмму",
      "http_request_duration_seconds_count" in _txt)
check("17.38 запросы к базе тоже посчитаны",
      'db_queries_total{op="read"}' in _txt)

# «когда последний раз проходил бэкап» — первый вопрос после инцидента, и
# отвечать на него /metrics обязан сам, а не через чтение чужого лога
with scheduler.job("t:metrics", 60, 60):
    pass
obs.refresh_gauges()
_txt = obs.render()
check("17.39 состояние фоновых задач видно в метриках",
      'job_last_ok_age_seconds{job="t:metrics"}' in _txt
      and 'job_runs_total{job="t:metrics"} 1' in _txt
      and "cache_backend_up" in _txt)
scheduler.reset()

# Значение метки может содержать что угодно (маршрут, причина операции), и
# незакавыченная кавычка в нём ломает разбор всей выгрузки
obs.reset_shared()
obs.inc("http_requests_total", method='G"T', path="a\\b", status=200)
check("17.40 кавычки и слеши в значениях меток экранируются",
      'method="G\\"T"' in obs.render() and 'path="a\\\\b"' in obs.render())

obs.reset_shared()
_notifier_src = _pathlib.Path("bot", "notifier.py").read_text(encoding="utf-8")
# у планировщика своего /metrics нет — HTTP он не поднимает вовсе, и без
# досылки пуши и бэкапы не были бы видны в мониторинге ни одной цифрой
check("17.41 планировщик досылает свои метрики сам",
      "obs.flush" in _notifier_src)
check("17.42 остановка ждёт добитые запросы (GRACEFUL_TIMEOUT)",
      _main_src.count("timeout_graceful_shutdown=settings.GRACEFUL_TIMEOUT") == 2)
check("17.43 sentry не включается без DSN", obs.init_sentry() is False)

print("\n=== 18. CI: прогон проверок, линтер, поиск секретов ===")
sys.path.insert(0, "tools")
import check_secrets as _sec

import run_tests as _runner

# Набор, забытый в списке, — это набор, который в CI не запускается вовсе.
# Проверка не «список непустой», а «список совпадает с тем, что лежит рядом»
_suite_files = sorted(p.name for p in _pathlib.Path(".").glob("test_*.py"))
check("18.1 в прогоне перечислены ВСЕ наборы проверок",
      sorted(_runner.SUITES) == _suite_files)
_runner_src = _pathlib.Path("run_tests.py").read_text(encoding="utf-8")
# каждый набор подменяет окружение до импорта db: в общем процессе первый
# импорт зафиксировал бы базу для всех остальных
check("18.2 наборы идут отдельными процессами",
      "subprocess.call" in _runner_src and "sys.executable" in _runner_src)
check("18.3 из окружения прогона вычищается боевая база",
      'env.pop("DATABASE_URL"' in _runner_src)

_ci = _pathlib.Path(".github", "workflows", "ci.yml")
check("18.4 есть workflow на пуш и на pull request", _ci.exists())
_ci_src = _ci.read_text(encoding="utf-8")
for _need, _what in (("python run_tests.py", "18.5 CI гоняет все наборы"),
                     ("ruff check", "18.6 CI гоняет линтер"),
                     ("npm run build", "18.7 CI собирает фронт (tsc + vite)"),
                     ("postgres:16", "18.8 CI поднимает живой PostgreSQL"),
                     ("check_secrets.py", "18.9 CI ищет секреты в индексе"),
                     ("migrate_to_postgres.py", "18.10 CI прогоняет переезд базы")):
    check(_what, _need in _ci_src)
# без этого второй пуш встаёт в очередь за устаревшим прогоном
check("18.11 новый пуш отменяет предыдущий прогон", "cancel-in-progress" in _ci_src)
check("18.12 у прогонов есть предел по времени", "timeout-minutes" in _ci_src)

check("18.13 конфиг линтера в репозитории",
      _pathlib.Path("ruff.toml").exists())

# Сканер обязан ЛОВИТЬ, а не молча проходить: правило, которое ничего не
# находит, выглядит в CI ровно так же, как правило, которое работает
_caught = lambda s: any(rx.search(s) for rx, _ in _sec.PATTERNS)  # noqa: E731
check("18.14 ловит настоящий токен бота",
      _caught("BOT_TOKEN=7712345678:AAH9xKk2LmNoPqRsTuVwXyZ0123456789abc"))  # secret-scan-ok
check("18.15 ловит приватный ключ и токены сервисов",
      _caught("-----BEGIN RSA PRIVATE KEY-----")  # secret-scan-ok
      and _caught("ghp_abcdefghijklmnopqrstuvwxyz0123456789")  # secret-scan-ok
      and _caught("AKIAIOSFODNN7EXAMPLE"))  # secret-scan-ok
check("18.16 ловит пароль в строке подключения и боевой DSN",
      _caught("postgresql://cookie:Sup3rSecret@db.prod.internal:5432/c")  # secret-scan-ok
      and _caught("https://1234@o99.ingest.sentry.io/5"))  # secret-scan-ok
# ложные срабатывания опаснее, чем кажется: с ними правило выключают целиком
check("18.17 не ругается на тестовый токен и примеры",
      not _caught("123456789:AAtestTOKENtestTOKENtestTOKENtest12")
      and not _caught("postgresql://user:pass@localhost/cookie_test"))
check("18.18 боевая база и .env числятся запрещёнными в индексе",
      ".env" in _sec.FORBIDDEN_NAMES and "data.db" in _sec.FORBIDDEN_NAMES
      and ".db" in _sec.FORBIDDEN_SUFFIXES)
check("18.19 сканер смотрит индекс git, а не рабочий каталог",
      "git" in _pathlib.Path("tools", "check_secrets.py").read_text(
          encoding="utf-8") and _sec.main() == 0)

print("\n=== 19. Бэкапы, шифрование и восстановление ===")
import base64 as _b64
import shlex as _shlex

from server import backup as _bk

# ---- проверка ключа ----
_good_key = _b64.b64encode(b"K" * 32).decode()
reload_settings(BACKUP_ENCRYPT_KEY=None)
check("19.1 без ключа шифрование выключено", _bk._key() is None)
reload_settings(BACKUP_ENCRYPT_KEY=_good_key)
check("19.2 ключ на 32 байта принимается", _bk._key() == b"K" * 32)
for _bad, _why in ((_b64.b64encode(b"short").decode(), "19.3 короткий ключ отвергнут"),
                   ("не base64!!", "19.4 не-base64 ключ отвергнут")):
    reload_settings(BACKUP_ENCRYPT_KEY=_bad)
    try:
        _bk._key()
        check(_why, False)
    except _bk.BackupError:
        check(_why, True)

# ---- шифрование ----
reload_settings(BACKUP_ENCRYPT_KEY=_good_key)
_tmp = tempfile.gettempdir()
_plain = os.path.join(_tmp, f"bk_plain_{os.getpid()}")
_enc = _plain + ".enc"
_dec = _plain + ".dec"
# больше одного куска: перестановка и обрыв кусков ловятся только на многих
_marker = b"USER-BALANCE-1076078800"
_payload = _marker + b"x" * (_bk.CHUNK + 17) + _marker
with open(_plain, "wb") as _f:
    _f.write(_payload)

_bk.encrypt_file(_plain, _enc, _bk._key())
_cipher_bytes = open(_enc, "rb").read()
check("19.5 открытых данных в шифртексте не остаётся",
      _marker not in _cipher_bytes and _cipher_bytes.startswith(_bk.MAGIC)
      and len(_cipher_bytes) > len(_payload))
_bk.decrypt_file(_enc, _dec, _bk._key())
check("19.6 расшифровка возвращает ровно исходный файл",
      open(_dec, "rb").read() == _payload)

# чужой ключ обязан ЛОМАТЬСЯ, а не отдавать мусор: расшифровка «успешно, но
# ерундой» означала бы восстановление в битую базу
try:
    _bk.decrypt_file(_enc, _dec, b"Z" * 32)
    check("19.7 чужой ключ не расшифровывает", False)
except _bk.BackupError as e:
    check("19.7 чужой ключ не расшифровывает", "не расшифровался" in str(e))

# обрезанный файл — самая частая порча при отправке по сети
_cut = _plain + ".cut"
with open(_cut, "wb") as _f:
    _f.write(_cipher_bytes[:-500])
try:
    _bk.decrypt_file(_cut, _dec, _bk._key())
    check("19.8 обрезанный архив не проходит молча", False)
except _bk.BackupError:
    check("19.8 обрезанный архив не проходит молча", True)

# один изменённый байт в середине
_raw = bytearray(_cipher_bytes)
_raw[len(_raw) // 2] ^= 0xFF
_bad_path = _plain + ".bad"
with open(_bad_path, "wb") as _f:
    _f.write(bytes(_raw))
try:
    _bk.decrypt_file(_bad_path, _dec, _bk._key())
    check("19.9 подмена байта в архиве обнаружена", False)
except _bk.BackupError:
    check("19.9 подмена байта в архиве обнаружена", True)

try:
    _bk.decrypt_file(_plain, _dec, _bk._key())
    check("19.10 незашифрованный файл не принимается за архив", False)
except _bk.BackupError as e:
    check("19.10 незашифрованный файл не принимается за архив",
          "не наш формат" in str(e))

check("19.11 контрольная сумма считается потоком",
      _bk.sha256(_plain) == __import__("hashlib").sha256(_payload).hexdigest())

# ---- отправка наружу ----
_up_script = os.path.join(_tmp, f"bk_up_{os.getpid()}.py")
with open(_up_script, "w", encoding="utf-8") as _f:
    # аргументы: что отправить, куда, каким кодом выйти, и имя как его увидела
    # команда — по нему проверяется подстановка {name}
    _f.write("import shutil, sys\n"
             "code = int(sys.argv[3])\n"
             "if code:\n"
             "    sys.exit(code)\n"
             "shutil.copyfile(sys.argv[1], sys.argv[2])\n"
             "open(sys.argv[2] + '.name', 'w').write(sys.argv[4])\n")
_dest = os.path.join(_tmp, f"bk_dest_{os.getpid()}")
_py = _shlex.quote(sys.executable.replace("\\", "/"))
_script_q = _shlex.quote(_up_script.replace("\\", "/"))


def _cmd(dest, code="0"):
    return f"{_py} {_script_q} {{src}} {_shlex.quote(dest)} {code} {{name}}"


reload_settings(BACKUP_UPLOAD_CMD=None)
check("19.12 без команды отправка просто выключена", _bk.upload(_plain) is False)

reload_settings(BACKUP_UPLOAD_CMD=_cmd(_dest))
check("19.13 отправка запускает команду и подставляет {src}",
      _bk.upload(_plain) is True and os.path.exists(_dest)
      and open(_dest, "rb").read() == _payload)

# ненулевой код возврата — это «файл НЕ уехал», и молчать об этом нельзя:
# «бэкапы отправляются» проверяется ровно в момент аварии
reload_settings(BACKUP_UPLOAD_CMD=_cmd(_dest, code="3"))
try:
    _bk.upload(_plain)
    check("19.14 неудачная отправка — ошибка, а не тишина", False)
except _bk.BackupError as e:
    check("19.14 неудачная отправка — ошибка, а не тишина", "3" in str(e))

reload_settings(BACKUP_UPLOAD_CMD="этой-команды-нет-нигде {src}")
try:
    _bk.upload(_plain)
    check("19.15 отсутствующая команда отправки — ошибка", False)
except _bk.BackupError as e:
    check("19.15 отсутствующая команда отправки — ошибка", "не найдена" in str(e))

# {name} должен быть ИМЕНЕМ файла, а не путём: иначе в чужом хранилище
# появится дерево каталогов этой машины
reload_settings(BACKUP_UPLOAD_CMD=_cmd(_dest))
_bk.upload(_plain)
check("19.16 {name} — имя файла, а не путь с машины",
      open(_dest + ".name", encoding="utf-8").read() == os.path.basename(_plain))

# ---- полный проход ----
os.unlink(_dest) if os.path.exists(_dest) else None
reload_settings(BACKUP_ENCRYPT_KEY=_good_key, BACKUP_UPLOAD_CMD=_cmd(_dest))
_info = _bk.run()
check("19.17 снимок сделан, посчитан и отправлен",
      _info["encrypted"] and _info["uploaded"]
      and _info["size"] > _bk.MIN_SIZE and os.path.exists(_dest))
check("19.18 рядом со снимком лежит контрольная сумма",
      os.path.exists(_info["path"] + ".sha256")
      and _bk.sha256(_info["path"]) in open(
          _info["path"] + ".sha256", encoding="utf-8").read())
# наружу уезжает ЗАШИФРОВАННОЕ, иначе в чужом хранилище лежат имена и балансы
check("19.19 наружу ушёл шифртекст, а не сама база",
      open(_dest, "rb").read(4) == _bk.MAGIC)
check("19.20 зашифрованная копия рядом с базой не остаётся",
      not os.path.exists(_info["path"] + ".enc"))

# ---- учения ----
_drill = _bk.drill()
check("19.21 учения разворачивают снимок и считают строки",
      _drill["encrypted"] and "users" in _drill and _drill["age_hours"] < 1)

# битый файл при целой сумме — ровно то, ради чего сумма и пишется
_snap = _bk.latest(".bak")
_keep = open(_snap, "rb").read()
with open(_snap, "r+b") as _f:
    _f.write(b"\x00" * 64)
try:
    _bk.drill()
    check("19.22 порча снимка на диске обнаружена", False)
except _bk.BackupError as e:
    check("19.22 порча снимка на диске обнаружена", "сумма не" in str(e))
with open(_snap, "wb") as _f:
    _f.write(_keep)
check("19.23 после починки учения снова проходят", _bk.drill()["users"] >= 0)

# ---- бракованные снимки ----
_folder = _bk._folder()
_saved = {f: open(os.path.join(_folder, f), "rb").read()
          for f in os.listdir(_folder)}
for _f in _saved:
    os.remove(os.path.join(_folder, _f))
try:
    _bk.drill()
    check("19.24 учения без единого снимка — ошибка", False)
except _bk.BackupError as e:
    check("19.24 учения без единого снимка — ошибка", "снимков нет" in str(e))
check("19.25 сводка честно говорит, что копий нет",
      _bk.status()["snapshots"] == 0)

# пустой снимок страшнее отсутствующего: задача отчиталась об успехе
_real_snapshot = _bk.db.snapshot
_bk.db.snapshot = lambda keep=7: open(
    os.path.join(_folder, "empty.20200101-000000.bak"), "wb").close() or \
    os.path.join(_folder, "empty.20200101-000000.bak")
try:
    _bk.run()
    check("19.26 пустой снимок считается провалом", False)
except _bk.BackupError as e:
    check("19.26 пустой снимок считается провалом", "мал" in str(e))
_bk.db.snapshot = _real_snapshot
for _f, _data in _saved.items():
    open(os.path.join(_folder, _f), "wb").write(_data)
for _f in list(os.listdir(_folder)):
    if _f.startswith("empty."):
        os.remove(os.path.join(_folder, _f))

# висячая сумма от вычищенного снимка
open(os.path.join(_folder, "gone.20200101-000000.bak.sha256"), "w").write("x")
_bk._prune_orphan_sums()
check("19.27 суммы от удалённых снимков не копятся",
      not os.path.exists(os.path.join(_folder,
                                      "gone.20200101-000000.bak.sha256")))

# ---- конфиг ----
_s19 = reload_settings(BACKUP_ENCRYPT_KEY=_b64.b64encode(b"short").decode())
check("19.28 кривой ключ шифрования отменяет старт",
      any("BACKUP_ENCRYPT_KEY" in t for t in fatals(_s19)))
_s19 = reload_settings(BACKUP_UPLOAD_CMD="rclone copy куда-то",
                       BACKUP_ENCRYPT_KEY=_good_key)
check("19.29 команда отправки без {src} отменяет старт",
      any("{src}" in t for t in fatals(_s19)))
_s19 = reload_settings(BACKUP_UPLOAD_CMD="rclone copyto {src} r2:b/{name}",
                       BACKUP_ENCRYPT_KEY=None)
check("19.30 отправка без шифрования — предупреждение, а не отказ",
      "незашифрованным" in texts(_s19)
      and not any("незашифрованным" in t for t in fatals(_s19)))
_s19 = reload_settings(BACKUP_ENCRYPT_KEY="A" * 43 + "=",
                       BACKUP_UPLOAD_CMD="rclone copyto {src} r2:b/{name}")
check("19.31 ключ бэкапа не печатается в сводке",
      "AAAA" not in _s19.summary() and "backup=offsite+enc" in _s19.summary()
      and "BACKUP_ENCRYPT_KEY" in _s19._SECRET_KEYS)
check("19.32 без отправки сводка честно говорит про локальную копию",
      "backup=local-only" in reload_settings(
          BACKUP_UPLOAD_CMD=None, BACKUP_ENCRYPT_KEY=None).summary())

# ---- расписание и метрики ----
reload_settings(BACKUP_ENCRYPT_KEY=None, BACKUP_UPLOAD_CMD=None)
import bot.notifier as _notifier

importlib.reload(_notifier)
_job_keys = [k for k, _, _, _ in _notifier.JOBS]
check("19.33 бэкап и учения — отдельные задачи планировщика",
      "db_backup" in _job_keys and "backup_drill" in _job_keys)
reload_settings(BACKUP_DRILL_INTERVAL_H="0")
importlib.reload(_notifier)
check("19.34 учения выключаются нулевым интервалом",
      "backup_drill" not in [k for k, _, _, _ in _notifier.JOBS]
      and "db_backup" in [k for k, _, _, _ in _notifier.JOBS])
reload_settings(BACKUP_DRILL_INTERVAL_H=None)
importlib.reload(_notifier)

# провал бэкапа обязан дойти до планировщика: задача, которая проглотила
# ошибку, выглядит в метриках здоровой ровно до дня восстановления
_bk_run_real = _bk.run
_bk.run = lambda: (_ for _ in ()).throw(_bk.BackupError("места нет"))
try:
    _notifier._backup_db()
    check("19.35 провал бэкапа не проглатывается", False)
except RuntimeError as e:
    check("19.35 провал бэкапа не проглатывается", "места нет" in str(e))
_bk.run = _bk_run_real

obs.refresh_gauges()
_txt19 = obs.render()
check("19.36 возраст и размер копии видны в метриках",
      "backup_age_seconds" in _txt19 and "backup_size_bytes" in _txt19
      and "backup_drill_total" in _txt19)
check("19.37 состояние бэкапов видно в /healthz",
      "backup.status()" in open("main.py", encoding="utf-8").read()
      and "age_hours" in _bk.status())

# ---- PITR ----
_pitr = _bk.pitr_status()
check("19.38 на SQLite PITR честно объявлен неприменимым",
      _pitr["supported"] is False)
check("19.39 для PostgreSQL статус архива спрашивается у живой базы",
      "pg_stat_archiver" in open(os.path.join("server", "backup.py"),
                                 encoding="utf-8").read())

# ---- runbook ----
_rb = _pathlib.Path("deploy", "RUNBOOK.md")
check("19.40 runbook на месте", _rb.exists())
_rb_src = _rb.read_text(encoding="utf-8") if _rb.exists() else ""
for _need, _what in (("RPO", "19.41 указан RPO — сколько данных теряем"),
                     ("RTO", "19.42 указан RTO — за сколько поднимаемся"),
                     ("archive_timeout", "19.43 описано включение WAL-архива"),
                     ("recovery_target_time", "19.44 описан откат на момент"),
                     ("pg_restore", "19.45 описано разворачивание дампа"),
                     ("getWebhookInfo", "19.46 не забыт webhook после переезда"),
                     ("systemctl stop", "19.47 сказано остановить запись до "
                                        "восстановления")):
    check(_what, _need in _rb_src)

print("\n=== 20. Приватность: страницы, экспорт, удаление, журнал доступа ===")
# Пункт 30 плана. Проверяется не «страница открывается», а три вещи, ошибка в
# каждой из которых стоит дорого: текст обещает ровно те сроки, что стоят в
# конфиге; удаление уносит игровое и НЕ трогает бухгалтерию; повторное нажатие
# ничего не списывает второй раз.
import hashlib as _hl
import hmac as _hm
import json as _js
from urllib.parse import urlencode as _ue

from server import auth as _auth
from server import economy as _eco
from server import game_config as cfg
from server import game_logic as _gl
from server.routers import legal as _legal

_LC = TestClient(main_module.app)
_LUID = 940_000_000 + os.getpid() % 1_000_000
_LADMIN = _LUID + 1


def _sign(uid, username="privacy", first_name="Privacy"):
    """initData с подписью того же токена, который захватил server.auth."""
    data = {"user": _js.dumps({"id": uid, "username": username,
                               "first_name": first_name}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = _hm.new(b"WebAppData", _auth.BOT_TOKEN.encode(), _hl.sha256).digest()
    data["hash"] = _hm.new(secret, dcs.encode(), _hl.sha256).hexdigest()
    return {"Authorization": "tma " + _ue(data)}


# ---- страницы ----
_r = _LC.get("/privacy")
_priv = _r.text
check("20.1 /privacy отдаётся как HTML",
      _r.status_code == 200
      and _r.headers["content-type"].startswith("text/html"))
check("20.2 /terms отдаётся как HTML",
      _LC.get("/terms").status_code == 200)
# страница обязана перечислять ровно то, что код собирает: идентификатор
# Telegram, язык, счётчики, платежи Stars, аналитику
check("20.3 перечислены реальные категории данных",
      all(_s in _priv for _s in ("идентификатор", "username", "Stars",
                                 "economy_ledger", "session")))
check("20.4 названо реальное событие аналитики из кода",
      "first_merge" in _priv and "tutorial_complete" in _priv)
# сроки берутся из конфига, а не из текста: поменяют константу — поменяется
# страница, и обещание не разъедется с поведением
check("20.5 сроки хранения совпадают с константами конфига",
      str(cfg.EVENTS_TTL_DAYS) in _priv and str(cfg.OPS_TTL_DAYS) in _priv
      and str(cfg.BACKUP_KEEP) in _priv)
check("20.6 сказано, что бэкапы шифрованные и удаление ждёт срок хранения",
      "AES-256-GCM" in _priv and "срок" in _priv)
check("20.7 сказано, что книгу и платежи удалить нельзя",
      "append-only" in _priv and "обезличив" in _priv.lower())

# ---- экспорт ----
check("20.8 без подписи экспорт не отдаётся",
      _LC.get("/api/legal/export").status_code == 401)
check("20.9 вход игрока",
      _LC.post("/api/auth", headers=_sign(_LUID)).status_code == 200)
_gl.add_cookies(_LUID, 1234.0, count_earned=True,
                operation_id=f"test_priv:{_LUID}", reason="test")
db_module.shared().exec(
    "INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
    "status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
    (_LUID, "test_item", 10, f"chg_{_LUID}", "fulfilled", time.time()))
db_module.shared().exec(
    "INSERT INTO farm (user_id, building_key, count) VALUES (?, ?, ?)",
    (_LUID, "oven", 2))
_r = _LC.get("/api/legal/export", headers=_sign(_LUID))
_exp = _r.json() if _r.status_code == 200 else {}
check("20.10 экспорт отдаёт профиль и все связанные таблицы",
      _r.status_code == 200 and _exp["account"]["user_id"] == _LUID
      and set(_exp["data"]) >= {"analytics_events", "purchases", "farm",
                                "economy_ledger", "referrals", "duels"})
check("20.11 в экспорте видны и платежи, и книга операций",
      len(_exp["data"]["purchases"]) == 1
      and any(r["reason"] == "test" for r in _exp["data"]["economy_ledger"]))
check("20.12 экспорт называет сроки хранения числами из конфига",
      _exp["retention"]["analytics_events_days"] == cfg.EVENTS_TTL_DAYS
      and _exp["retention"]["backup_snapshots"] == cfg.BACKUP_KEEP)

# ---- удаление ----
check("20.13 удаление без слова подтверждения отбивается",
      _LC.post("/api/legal/delete", json={"confirm": "yes"},
               headers=_sign(_LUID)).status_code == 400)
_r = _LC.post("/api/legal/delete", json={"confirm": "DELETE"},
              headers=_sign(_LUID))
_del = _r.json() if _r.status_code == 200 else {}
check("20.14 удаление проходит и отчитывается, что снесло",
      _r.status_code == 200 and _del["already_deleted"] is False
      and _del["deleted_rows"].get("farm") == 1)
_row = db_module.shared().get_user(_LUID)
check("20.15 строка игрока осталась обезличенной, а не удалённой",
      _row is not None and _row["first_name"] == _legal.TOMBSTONE
      and _row["username"] is None)
check("20.16 прогресс и баланс обнулены",
      float(_row["cookies"]) == 0 and float(_row["xp"]) == 0
      and _row["total_clicks"] == 0 and _row["notify_blocked"] == 1)
check("20.17 игровые строки удалены",
      db_module.shared().q1("SELECT COUNT(*) c FROM farm WHERE user_id = ?",
                            (_LUID,))["c"] == 0)
# главное: удаление НЕ должно вычищать бухгалтерию
check("20.18 книга операций и платежи остались",
      db_module.shared().q1("SELECT COUNT(*) c FROM economy_ledger "
                            "WHERE user_id = ?", (_LUID,))["c"] > 0
      and db_module.shared().q1("SELECT COUNT(*) c FROM purchases "
                                "WHERE user_id = ?", (_LUID,))["c"] == 1)
# баланс погашен ЧЕРЕЗ книгу, иначе сверка кричала бы на этого игрока каждый день
_rec = _eco.reconcile(_LUID)
check("20.19 после удаления сверка не находит расхождения",
      all(abs(v["drift"]) < 1e-6 for v in _rec.values()))
check("20.20 факт удаления записан отдельной строкой",
      db_module.shared().q1(
          "SELECT COUNT(*) c FROM analytics_events WHERE user_id = ? AND event = ?",
          (_LUID, "account_deleted"))["c"] == 1)
# двойная отправка: вторая ничего не трогает и не списывает баланс ещё раз
_ledger_before = db_module.shared().q1(
    "SELECT COUNT(*) c FROM economy_ledger WHERE user_id = ?", (_LUID,))["c"]
_r2 = _LC.post("/api/legal/delete", json={"confirm": "DELETE"},
               headers=_sign(_LUID))
check("20.21 повторное удаление идемпотентно",
      _r2.status_code == 200 and _r2.json()["already_deleted"] is True)
check("20.22 повтор не пишет в книгу второй раз",
      db_module.shared().q1("SELECT COUNT(*) c FROM economy_ledger "
                            "WHERE user_id = ?", (_LUID,))["c"] == _ledger_before)

# ---- журнал административного доступа ----
_saved_admin = _auth.ADMIN_ID
_auth.ADMIN_ID = _LADMIN
try:
    check("20.23 чужой в админку не проходит и в журнал не попадает",
          _LC.get("/api/admin/stats", headers=_sign(_LUID)).status_code == 403)
    check("20.24 админ проходит",
          _LC.get("/api/admin/stats",
                  headers=_sign(_LADMIN, "adm", "Adm")).status_code == 200)
    _log = _LC.get("/api/admin/access-log",
                   headers=_sign(_LADMIN, "adm", "Adm")).json()
    _paths = [r["event"] for r in _log["access_log"]]
    check("20.25 обращение админа записано в журнал",
          any("/api/admin/stats" in p for p in _paths))
    check("20.26 журнал знает, кто именно смотрел",
          all(r["admin_id"] == _LADMIN for r in _log["access_log"]))
    check("20.27 отказ чужому в журнал не записан",
          not any("403" in p for p in _paths)
          and db_module.shared().q1(
              "SELECT COUNT(*) c FROM analytics_events WHERE user_id = ? AND event LIKE ?",
              (_LUID, "admin_access:%"))["c"] == 0)
    check("20.28 журнал сам называет свой срок хранения",
          _log["retention_days"] == cfg.EVENTS_TTL_DAYS)
finally:
    _auth.ADMIN_ID = _saved_admin

check("20.29 второго механизма журналирования в админке не заведено",
      "admin_access" not in _pathlib.Path("server", "routers", "admin.py"
                                          ).read_text(encoding="utf-8")
      and "legal.admin_audit" in _pathlib.Path("main.py").read_text(
          encoding="utf-8"))

# ---- процедура удаления из бэкапов ----
_rb_src2 = _pathlib.Path("deploy", "RUNBOOK.md").read_text(encoding="utf-8")
for _need, _what in (("BACKUP_KEEP", "20.30 срок жизни снимков назван константой"),
                     ("7 суток", "20.31 сказано, за сколько удаление доезжает "
                                 "до копий"),
                     ("account_deleted", "20.32 описано, как повторить удаление "
                                         "после восстановления"),
                     ("economy_ledger", "20.33 сказано, что книга и платежи "
                                        "не удаляются")):
    check(_what, _need in _rb_src2)

print("\n=== 21. Аналитика: одна таблица, обязательные поля, выгрузка ===")
# Раздел «Аналитика» плана. Проверяется не «строка записалась», а четыре вещи,
# без каждой из которых таблица бесполезна: обязательные поля заполняются САМИ
# (поле, которое просят заполнить руками, останется пустым); стык с книгой идёт
# по operation_id; TTL не уносит невыгруженное; второй системы событий нет.
from server import support as _sup

_AUID = 950_000_000 + os.getpid() % 1_000_000
_shared = db_module.shared()
_shared.exec("DELETE FROM analytics_events WHERE user_id = ?", (_AUID,))
_shared.exec("INSERT INTO users (user_id, lang, source_code, created_at) "
             "VALUES (?, ?, ?, ?)", (_AUID, "uk", "tiktok_jan", time.time()))

_eid = _gl.track("unit_test_event", _AUID, value=7, operation_id="op:unit:1",
                 extra_field="hello")
_row = _shared.q1("SELECT * FROM analytics_events WHERE event_id = ?", (_eid,))
check("21.1 track() возвращает event_id и пишет строку",
      bool(_eid) and _row is not None)
check("21.2 обязательные поля заполнены без участия вызывающего",
      _row["user_id"] == _AUID and _row["created_at"] > 0
      and _row["config_version"] == cfg.CONFIG_VERSION
      and _row["lang"] == "uk" and _row["variant"]
      and _row["source"] == "link" and _row["campaign"] == "tiktok_jan")
check("21.3 версия конфига — отпечаток КОНСТАНТ, а не случайная строка",
      cfg.CONFIG_VERSION == cfg.config_version() and len(cfg.CONFIG_VERSION) == 12)
check("21.4 стык с книгой идёт по operation_id",
      _row["economy_operation_id"] == "op:unit:1")
check("21.5 непредусмотренные свойства уезжают в props, а не теряются",
      _js.loads(_row["props"])["extra_field"] == "hello")
check("21.6 ветка эксперимента детерминирована",
      _gl.experiment_variant(_AUID) == _gl.experiment_variant(_AUID))


# ошибка аналитики не имеет права ронять игру
def _boom(*_a, **_k):
    raise RuntimeError("boom")


_saved_exec = _gl.db.exec
try:
    _gl.db.exec = _boom
    check("21.7 падение записи аналитики не выбрасывает наружу",
          _gl.track("boom_event", _AUID) is None)
finally:
    _gl.db.exec = _saved_exec

# ---- выгрузка ----
_batch = _gl.export_batch(limit=100)
check("21.8 выгрузка отдаёт сырые строки и курсор",
      _batch["count"] > 0 and _batch["next_after_id"] > 0
      and _batch["pending"] >= _batch["count"])
check("21.9 props в выгрузке — объект, а не строка с json внутри",
      all(isinstance(r["props"], (dict, type(None))) for r in _batch["rows"]))
check("21.10 забор НИЧЕГО не помечает: оборвавшийся экспорт не теряет данные",
      _shared.q1("SELECT exported_at FROM analytics_events WHERE event_id = ?",
                 (_eid,))["exported_at"] == 0)
_ack = _gl.mark_exported(_batch["next_after_id"])
check("21.11 ack помечает выгруженным и уменьшает очередь",
      _ack["marked"] > 0
      and _shared.q1("SELECT exported_at FROM analytics_events "
                     "WHERE event_id = ?", (_eid,))["exported_at"] > 0)
check("21.12 повторный ack ничего не помечает второй раз",
      _gl.mark_exported(_batch["next_after_id"])["marked"] == 0)

# ---- TTL не уносит невыгруженное ----
from bot import notifier as _notif

_old = time.time() - (cfg.EVENTS_TTL_DAYS + 1) * 86400
_shared.exec("INSERT INTO analytics_events (event_id, user_id, event, value, "
             "created_at, exported_at) VALUES (?, ?, ?, 0, ?, 0)",
             (f"ttl_pending_{_AUID}", _AUID, "old_pending", _old))
_shared.exec("INSERT INTO analytics_events (event_id, user_id, event, value, "
             "created_at, exported_at) VALUES (?, ?, ?, 0, ?, ?)",
             (f"ttl_done_{_AUID}", _AUID, "old_exported", _old, time.time()))
_notif._prune_events()
check("21.13 TTL забирает выгруженное",
      _shared.q1("SELECT COUNT(*) c FROM analytics_events WHERE event_id = ?",
                 (f"ttl_done_{_AUID}",))["c"] == 0)
check("21.14 TTL НЕ забирает невыгруженное — иначе D30 неизмерим по построению",
      _shared.q1("SELECT COUNT(*) c FROM analytics_events WHERE event_id = ?",
                 (f"ttl_pending_{_AUID}",))["c"] == 1)
# ... но не бесконечно: запас есть, и по его истечении строка уходит громко
_shared.exec("UPDATE analytics_events SET created_at = ? WHERE event_id = ?",
             (_old - (cfg.ANALYTICS_EXPORT_GRACE_DAYS + 1) * 86400,
              f"ttl_pending_{_AUID}"))
_notif._prune_events()
check("21.15 сверх запаса невыгруженное всё же удаляется (диск дороже)",
      _shared.q1("SELECT COUNT(*) c FROM analytics_events WHERE event_id = ?",
                 (f"ttl_pending_{_AUID}",))["c"] == 0)

# ---- второй системы нет ----
_gl_src = _pathlib.Path("server", "game_logic.py").read_text(encoding="utf-8")
check("21.16 таблицы events в коде не осталось",
      "FROM events" not in _gl_src and "INTO events" not in _gl_src
      and "FROM events" not in _pathlib.Path(
          "server", "routers", "legal.py").read_text(encoding="utf-8"))
check("21.17 снимок конфига ивента уехал в app_state, а не в аналитику",
      "app_state" in _gl_src)
_ev_id = _gl.event_id_of("unit_evt", 1000)
_gl.set_event_killed(_ev_id, True)
check("21.18 kill switch ивента переживает чистилку аналитики",
      _shared.q1("SELECT value FROM app_state WHERE name = ?",
                 (f"event_cfg:{_ev_id}:killed",))["value"] == 1)
_gl.set_event_killed(_ev_id, False)

print("\n=== 22. Поддержка и anti-fraud (пункт 31) ===")
# Главное здесь — не «ручка отвечает 200», а что инструмент ЗАПРЕЩАЕТ то, что
# запрещает INCIDENTS §4: выдать мимо книги. И что массовая выдача идемпотентна:
# повтор после обрыва не платит второй раз.
_SUID = 960_000_000 + os.getpid() % 1_000_000
_SUID2 = _SUID + 1
for _u in (_SUID, _SUID2):
    _shared.exec("INSERT INTO users (user_id, first_name, created_at) "
                 "VALUES (?, ?, ?)", (_u, "Support", time.time()))
_shared.exec(
    "INSERT INTO purchases (user_id, item_key, stars_amount, tg_payment_id, "
    "status, created_at, granted_at, granted_payload) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
    (_SUID, "cookies_pack", 50, f"chg_sup_{_SUID}", "fulfilled", time.time(),
     time.time(), '{"cookies": 1000}'))
_gl.add_cookies(_SUID, 1000, count_earned=False,
                operation_id=f"purchase:chg_sup_{_SUID}",
                reason="purchase_cookies", ref_type="charge",
                ref_id=f"chg_sup_{_SUID}")

_chain = _sup.payment_chain(_SUID)
check("22.1 цепочка invoice -> payment -> grant -> refund собирается",
      len(_chain) == 1
      and list(_chain[0]["stages"]) == ["invoice", "payment", "grant", "refund"]
      and _chain[0]["stages"]["payment"]["done"]
      and _chain[0]["stages"]["grant"]["done"]
      and _chain[0]["stages"]["refund"]["done"] is False)
check("22.2 к платежу подтянуты движения книги (стык по operation_id)",
      any(r["operation_id"] == f"purchase:chg_sup_{_SUID}"
          for r in _chain[0]["ledger"]))

# --- компенсация ---
_before = float(_shared.get_user(_SUID)["cookies"])
_comp = _sup.compensate(_SUID, "cookies", 500, "support_payment_lost",
                        "INC-42", "тест")
check("22.3 компенсация начисляет и возвращает баланс",
      float(_shared.get_user(_SUID)["cookies"]) == _before + 500
      and _comp["already_paid"] is False)
check("22.4 компенсация ВСЕГДА проходит через книгу",
      _shared.q1("SELECT COUNT(*) c FROM economy_ledger WHERE operation_id = ?",
                 (_comp["operation_id"],))["c"] == 1)
check("22.5 после компенсации сверка не находит расхождения",
      all(abs(v["drift"]) < 1e-6 for v in _eco.reconcile(_SUID).values()))
check("22.6 компенсация не идёт в заработок (иначе поднимет уровень и место)",
      _shared.q1("SELECT counts_earned FROM economy_ledger "
                 "WHERE operation_id = ?",
                 (_comp["operation_id"],))["counts_earned"] == 0)
_again = _sup.compensate(_SUID, "cookies", 500, "support_payment_lost", "INC-42")
check("22.7 повтор по тому же инциденту не платит второй раз",
      _again["already_paid"] is True
      and float(_shared.get_user(_SUID)["cookies"]) == _before + 500)
for _bad, _what in (
        (("cookies", 500, "чинил руками", "INC-42"),
         "22.8 свободная причина отвергается"),
        (("prestige_points", 5, "support_goodwill", "INC-42"),
         "22.9 валюта без денежного примитива отвергается"),
        (("cookies", -500, "support_goodwill", "INC-42"),
         "22.10 отнять этой ручкой нельзя"),
        (("cookies", 500, "support_goodwill", "   "),
         "22.11 компенсация без метки инцидента отвергается")):
    try:
        _sup.compensate(_SUID, *_bad)
        check(_what, False)
    except ValueError:
        check(_what, True)
# Не «слова UPDATE нет в файле» (оно есть в объяснении запрета), а «модуль
# не выполняет ни одной пишущей команды»: единственный путь наружу — денежные
# примитивы game_logic, а они пишут в книгу той же транзакцией
_sup_src = _pathlib.Path("server", "support.py").read_text(encoding="utf-8")
check("22.12 инструмент не выполняет ни одной записи в обход книги",
      'db.exec(' not in _sup_src and 'db.q1w(' not in _sup_src)

# --- массовая ---
_dry = _sup.compensate_bulk([_SUID, _SUID2], "cookies", 100,
                            "support_rollback", "INC-43")
check("22.13 массовая выдача по умолчанию ничего не выдаёт (dry-run)",
      _dry["dry_run"] is True and _dry["cohort_size"] == 2
      and _shared.q1("SELECT COUNT(*) c FROM economy_ledger WHERE ref_id = ?",
                     ("inc-43",))["c"] == 0)
_bulk = _sup.compensate_bulk([_SUID, _SUID2], "cookies", 100,
                             "support_rollback", "INC-43", dry_run=False)
check("22.14 массовая выдача платит всей когорте",
      _bulk["paid"] == 2 and _bulk["failed_count"] == 0)
_bulk2 = _sup.compensate_bulk([_SUID, _SUID2], "cookies", 100,
                              "support_rollback", "INC-43", dry_run=False)
check("22.15 повторный запуск партии не платит второй раз",
      _bulk2["paid"] == 0 and _bulk2["already_paid"] == 2)
_bulk3 = _sup.compensate_bulk([_SUID, 10_101_010_101], "cookies", 100,
                              "support_rollback", "INC-44", dry_run=False)
check("22.16 несуществующий игрок не роняет всю партию",
      _bulk3["paid"] == 1 and _bulk3["failed_count"] == 1)
# когорта берётся из данных, а не из списка жалоб
_cohort = _sup.cohort("ledger_reason", "support_rollback", 0, time.time() + 60)
check("22.17 когорта инцидента поднимается из книги",
      _SUID in _cohort and _SUID2 in _cohort)

# --- флаги ---
_shared.exec("UPDATE users SET total_clicks = ?, created_at = ? "
             "WHERE user_id = ?", (10 ** 9, time.time() - 3600, _SUID2))
_flags = {f["flag"] for f in _sup.flags_for(_SUID2)}
check("22.18 нереальный темп кликов флагуется", "click_rate" in _flags)
_shared.exec("UPDATE users SET cookie_debt = 5 WHERE user_id = ?", (_SUID2,))
check("22.19 непогашенный долг после возврата флагуется",
      "refund_debt" in {f["flag"] for f in _sup.flags_for(_SUID2)})
_susp = _sup.suspicious(10)
check("22.20 общий список подозрительных собирается по всей базе",
      any(r["user_id"] == _SUID2 for r in _susp["click_rate"])
      and "thresholds" in _susp)

# --- ручки под общим журналом админки ---
_auth.ADMIN_ID = _LADMIN
try:
    _r = _LC.get(f"/api/admin/player/{_SUID}",
                 headers=_sign(_LADMIN, "adm", "Adm"))
    _card = _r.json() if _r.status_code == 200 else {}
    check("22.21 карточка игрока отдаётся админу",
          _r.status_code == 200 and _card["user"]["user_id"] == _SUID
          and "payments" in _card and "reconcile" in _card and "flags" in _card)
    check("22.22 обращение к карточке записано в общий журнал с ЦЕЛЬЮ",
          any("/api/admin/player/" in r["event"]
              and r["target_user_id"] == _SUID
              for r in _LC.get("/api/admin/access-log",
                               headers=_sign(_LADMIN, "adm", "Adm")
                               ).json()["access_log"]))
    check("22.23 чужой в инструмент поддержки не проходит",
          _LC.get(f"/api/admin/player/{_SUID}",
                  headers=_sign(_LUID)).status_code == 403
          and _LC.post("/api/admin/compensate", json={
              "user_id": _SUID, "amount": 1, "reason": "support_goodwill",
              "incident": "x"}, headers=_sign(_LUID)).status_code == 403)
    _r = _LC.post("/api/admin/compensate", json={
        "user_id": _SUID, "currency": "cookies", "amount": 10,
        "reason": "нет такой", "incident": "INC-45"},
        headers=_sign(_LADMIN, "adm", "Adm"))
    check("22.24 ручка компенсации отбивает причину не из списка",
          _r.status_code == 400)
    _r = _LC.get("/api/admin/analytics/export?limit=5",
                 headers=_sign(_LADMIN, "adm", "Adm"))
    check("22.25 выгрузка аналитики доступна тем же путём, что и админка",
          _r.status_code == 200 and "rows" in _r.json()
          and _r.json()["ttl_days"] == cfg.EVENTS_TTL_DAYS)
    check("22.26 второго механизма журналирования в админке по-прежнему нет",
          "admin_access" not in _pathlib.Path(
              "server", "routers", "admin.py").read_text(encoding="utf-8"))
finally:
    _auth.ADMIN_ID = _saved_admin

# ---- рантбуки ----
_inc_src = _pathlib.Path("deploy", "INCIDENTS.md").read_text(encoding="utf-8")
for _need, _what in (
        ("/api/admin/compensate", "22.27 INCIDENTS описывает компенсацию ручкой"),
        ("dry_run", "22.28 INCIDENTS предупреждает про сухой прогон"),
        ("/api/admin/fraud", "22.29 INCIDENTS описывает разбор подозрительного"),
        ("export_analytics.py", "22.30 INCIDENTS знает про остановку выгрузки")):
    check(_what, _need in _inc_src)
_rb_src3 = _pathlib.Path("deploy", "RUNBOOK.md").read_text(encoding="utf-8")
for _need, _what in (
        ("export_analytics.py", "22.31 RUNBOOK описывает выгрузку аналитики"),
        ("ANALYTICS_EXPORT_GRACE_DAYS",
         "22.32 RUNBOOK называет запас, после которого сырое удаляется")):
    check(_what, _need in _rb_src3)

for _u in (_AUID, _SUID, _SUID2):
    for _t in ("users", "analytics_events", "purchases", "economy_ops"):
        db_module.shared().exec(f"DELETE FROM {_t} WHERE user_id = ?", (_u,))

# уборка за собой: временная база общая на весь набор
for _t in ("users", "analytics_events", "purchases", "farm", "economy_ledger",
           "economy_ops", "economy_opening"):
    if _t == "economy_ledger":
        continue  # append-only: строку не удалить даже в тестах
    db_module.shared().exec(f"DELETE FROM {_t} WHERE user_id IN (?, ?)",
                            (_LUID, _LADMIN))

for _f in (_plain, _enc, _dec, _cut, _bad_path, _dest, _up_script):
    if os.path.exists(_f):
        os.remove(_f)

use_fallback()
importlib.reload(_settings)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
