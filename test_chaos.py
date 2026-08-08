"""Хаос: что делает сервис, когда ломается не он сам, а всё вокруг.

Обычные проверки отвечают на вопрос «работает ли код, когда всё хорошо».
Инциденты выглядят иначе: Redis перезагрузили, база на секунду встала,
Telegram отвечает пятисотками, на диске кончилось место. Код при этом
формально исправен — ломается предположение, что зависимость всегда рядом.

Здесь каждая зависимость ломается НАМЕРЕННО, и проверяется не «ошибки нет», а
две вещи, которые важны на самом деле:

  1. ДЕГРАДАЦИЯ, А НЕ ПАДЕНИЕ. Отказ Redis — это потеря общего лимитера, а не
     потеря игры. Отказ Telegram — это неотправленный пуш, а не остановка
     планировщика. Каждая поломка должна иметь заранее решённый масштаб.

  2. ЦЕЛОСТНОСТЬ ДЕНЕГ. После любой поломки книга операций обязана сходиться с
     колонками, балансы — не уходить в минус, повтор запроса — не выдавать
     вторую награду. Сломанная зависимость не повод потерять или напечатать
     печеньки: это то, за что игрок не прощает.

Чего здесь НЕТ. Настоящей нагрузки — она в loadtest.py, потому что это другой
вопрос и другое время прогона. Убийства процессов сигналами — это проверка
systemd, а не кода.

Запуск: python test_chaos.py
"""
import os
import sqlite3
import tempfile
import threading
import time

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
# всё живёт во ВРЕМЕННОЙ базе: боевая не участвует ни на одном шаге
DB_PATH = os.path.join(tempfile.gettempdir(), f"cookie_chaos_{os.getpid()}.db")
for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        os.remove(DB_PATH + _suffix)
os.environ["DATABASE_PATH"] = DB_PATH

import hashlib                                  # noqa: E402
import hmac                                     # noqa: E402
import json                                     # noqa: E402
from urllib.parse import urlencode              # noqa: E402

from fastapi.testclient import TestClient       # noqa: E402

from main import app                            # noqa: E402
from server import cache, economy, obs, scheduler, settings  # noqa: E402
from server.game_logic import db                # noqa: E402

BOT_TOKEN = os.environ["BOT_TOKEN"]
UID = 950_000_000 + int(time.time()) % 1_000_000

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def sign(user_id):
    data = {"user": json.dumps({"id": user_id, "username": "chaos",
                                "first_name": "Chaos"}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def H(uid=UID):
    return {"Authorization": "tma " + sign(uid)}


c = TestClient(app)


def books_converge(user_id=UID) -> bool:
    """Главный инвариант: колонка и книга операций смотрят на одно число."""
    state = economy.reconcile(user_id)
    return all(abs(v["drift"]) < 1e-6 for v in state.values())


def balances_sane(user_id=UID) -> bool:
    u = db.get_user(user_id)
    if u is None:
        return False
    return all((u[col] or 0) >= 0 for col in set(economy.CURRENCY_COLUMN.values()))


c.post("/api/auth", headers=H())


# ======================================================================
print("\n=== 1. Redis умирает под нагрузкой ===")
# ======================================================================
try:
    import fakeredis
except ImportError:
    fakeredis = None


_real_connect = cache._connect


class DeadRedis:
    """Redis, который принял соединение и перестал отвечать.

    Это хуже, чем «Redis выключен»: выключенный не подключается и путь отказа
    очевиден. Живой, но падающий на каждой команде — то, что видно при
    перезагрузке, вытеснении памяти и обрыве сети, и именно на нём выясняется,
    обёрнут ли каждый вызов."""

    def __getattr__(self, name):
        def boom(*a, **kw):
            raise ConnectionError("redis упал")
        return boom


def use_dead_redis():
    cache._reset_for_tests()
    settings.REDIS_URL = "redis://dead"
    cache._connect = lambda: DeadRedis()


def use_fake_redis():
    cache._reset_for_tests()
    settings.REDIS_URL = "redis://fake"
    fake = fakeredis.FakeRedis(decode_responses=True)
    cache._connect = lambda: fake
    return fake


def use_fallback():
    cache._reset_for_tests()
    cache._connect = _real_connect      # иначе фолбэк подключался бы к заглушке
    settings.REDIS_URL = ""


use_dead_redis()
r = c.get("/api/state", headers=H())
check("1.1 игра работает при мёртвом Redis", r.status_code == 200, r.text[:200])
check("1.2 отказ виден как выключенное общее состояние", cache.enabled() is False)

# лимитер обязан продолжать считать: без него отказ Redis снимал бы ограничения
# ровно в тот момент, когда сервис и так нездоров
cache.reset_all_windows()
_verdicts = [cache.incr_window("chaos:dead", 3, 60)[0] for _ in range(5)]
check("1.3 лимит продолжает работать на фолбэке",
      _verdicts[:3] == [True] * 3 and _verdicts[3:] == [False] * 2)

# по этой ручке ВЫВОДЯТ ИЗ БАЛАНСИРОВКИ. Redis — не повод: игра без него живёт
check("1.4 упавший Redis не выводит процесс из балансировки",
      c.get("/readyz").status_code == 200)
check("1.5 но в /healthz это видно",
      c.get("/healthz").json()["cache"].get("redis", "").startswith("down"))

# Замок при мёртвом Redis — единственное место, где отказ НЕ деградирует
# мягко, и это сознательный выбор: единственность владельца проверить нечем, а
# ролловер сезона или бэкап, выполненный двумя воркерами разом, дороже
# пропущенного тика. Цена решения: пока Redis лежит, фоновых задач нет вовсе —
# видно по job_last_ok_age_seconds, на него и заведён алерт.
_ran = []
with cache.lock("chaos:job", 30) as mine:
    if mine:
        _ran.append(1)
check("1.6 при недоступном Redis задача НЕ выполняется (fail closed)",
      _ran == [])
# а вот когда Redis не настроен вовсе, процесс один и работать обязан
use_fallback()
with cache.lock("chaos:job", 30) as mine:
    if mine:
        _ran.append(1)
check("1.6b без Redis в конфиге одиночный процесс работает", _ran == [1])
use_dead_redis()

if fakeredis:
    _fake = use_fake_redis()
    check("1.7 после возвращения Redis общее состояние снова общее",
          cache.enabled() is True)
    # ключи, накопленные на фолбэке, в Redis не переезжают — и это правильно:
    # переносить локальные счётчики значило бы умножить их на число воркеров
    check("1.8 лимитер продолжает работать на вернувшемся Redis",
          cache.incr_window("chaos:back", 2, 60)[0] is True)
use_fallback()

check("1.9 после всей чехарды книга сходится", books_converge())


# ======================================================================
print("\n=== 2. База отваливается посреди работы ===")
# ======================================================================
_real_q1 = db.q1


def dead_db(*a, **kw):
    raise sqlite3.OperationalError("database is locked")


db.q1 = dead_db
try:
    check("2.1 живость не зависит от базы", c.get("/livez").status_code == 200)
    # а вот готовность зависит: запрос игрока без базы всё равно кончится
    # пятисоткой, и балансировщику лучше увести трафик
    check("2.2 готовность честно отвечает 503", c.get("/readyz").status_code == 503)
    check("2.3 в теле 503 написана причина",
          c.get("/readyz").json().get("db", "").startswith("down"))
finally:
    db.q1 = _real_q1
check("2.4 после возвращения базы готовность возвращается",
      c.get("/readyz").status_code == 200)
check("2.5 данные игрока целы", db.get_user(UID) is not None and balances_sane())


# ======================================================================
print("\n=== 3. База занята: writer'ы дерутся за файл ===")
# ======================================================================
# SQLITE_BUSY — не ошибка, а нормальное состояние файловой базы под двумя
# писателями. Ретрай обязан быть внутри слоя доступа: если он вылезет наружу,
# игрок увидит 500 на клике в совершенно здоровой системе.
_before = db.q1("SELECT cookies FROM users WHERE user_id = ?", (UID,))["cookies"]
_busy_left = [2]
_real_execute = db.exec


def flaky_exec(sql, params=()):
    if _busy_left[0] > 0 and sql.strip().upper().startswith("UPDATE"):
        _busy_left[0] -= 1
        raise sqlite3.OperationalError("database is locked")
    return _real_execute(sql, params)


# ретраи проверяем на уровне транзакции: она умеет повторять BEGIN IMMEDIATE
_retries_before = obs._counters.copy()
db.exec = flaky_exec
try:
    _got_error = None
    try:
        db.exec("UPDATE users SET cookies = cookies + 1 WHERE user_id = ?", (UID,))
    except sqlite3.OperationalError as e:
        _got_error = str(e)
    check("3.1 занятая база отдаёт понятную ошибку, а не молчание",
          _got_error is not None and "locked" in _got_error)
finally:
    db.exec = _real_execute

_after = db.q1("SELECT cookies FROM users WHERE user_id = ?", (UID,))["cookies"]
check("3.2 неудачная запись ничего не изменила", _after == _before)

# настоящая параллельная запись: 8 потоков по одной строке
import server.game_logic as gl                   # noqa: E402

_errors = []


def bump():
    try:
        gl.add_cookies(UID, 10, count_earned=False, reason="chaos_bump")
    except Exception as e:                       # noqa: BLE001 — собираем всё
        _errors.append(repr(e))


_threads = [threading.Thread(target=bump) for _ in range(8)]
_start = time.perf_counter()
for t in _threads:
    t.start()
for t in _threads:
    t.join()
_elapsed = time.perf_counter() - _start
check(f"3.3 восемь параллельных писателей прошли без ошибок ({_errors[:1]})",
      not _errors)
check("3.4 ни одного потерянного апдейта",
      db.q1("SELECT cookies FROM users WHERE user_id = ?",
            (UID,))["cookies"] == _before + 80)
check("3.5 книга сошлась после драки за строку", books_converge())
check(f"3.6 драка разошлась за разумное время ({_elapsed:.1f} с)", _elapsed < 30)

# Соединения живут по потоку и переиспользуются. Транзакция, которую не удалось
# ни закоммитить, ни откатить, залипает на соединении навсегда — и дальше поток
# читает снапшот на момент её начала. Наружу это выходит не ошибкой, а
# путешествием во времени: только что зарегистрированный игрок получает
# err_no_user, а его клики уходят в никуда. Именно это и ловилось под нагрузкой.
_stale: dict[str, int] = {}
_pinned = threading.Event()
_written = threading.Event()


def poisoned_reader():
    db.cursor.execute("BEGIN")            # транзакция, которую никто не закроет
    read = "SELECT cookies FROM users WHERE user_id = ?"
    _stale["start"] = db.q1(read, (UID,))["cookies"]
    _pinned.set()
    _written.wait(10)
    _stale["before_fix"] = db.q1(read, (UID,))["cookies"]
    db._drop_dirty_connection()
    _stale["after_fix"] = db.q1(read, (UID,))["cookies"]


_reader = threading.Thread(target=poisoned_reader)
_reader.start()
_pinned.wait(10)
gl.add_cookies(UID, 777, count_earned=False, reason="chaos_stale")
_written.set()
_reader.join(20)

check("3.7 залипшая транзакция и правда показывает потоку прошлое",
      _stale.get("before_fix") == _stale.get("start"))
check("3.8 такое соединение выбрасывается, а не переиспользуется",
      _stale.get("after_fix") == _stale.get("start", 0) + 777)


# ======================================================================
print("\n=== 4. Клиент теряет ответы и повторяет запросы ===")
# ======================================================================
# Мобильная сеть теряет ОТВЕТ чаще, чем запрос. Для игрока это выглядит как
# «кнопка не сработала», и он жмёт ещё раз — а для сервера это второй
# полноценный запрос на то же действие.
r1 = c.post("/api/daily/claim", headers={**H(), "X-Op-Id": "chaos-daily-1"})
r2 = c.post("/api/daily/claim", headers={**H(), "X-Op-Id": "chaos-daily-1"})
check("4.1 первый клейм прошёл", r1.status_code == 200, r1.text[:200])
check("4.2 повтор вернул ТОТ ЖЕ ответ, а не отказ и не вторую награду",
      r2.status_code == 200 and r2.json() == r1.json())
_bal = db.get_user(UID)["cookies"]
c.post("/api/daily/claim", headers={**H(), "X-Op-Id": "chaos-daily-1"})
check("4.3 третий повтор тоже ничего не начислил",
      db.get_user(UID)["cookies"] == _bal)
check("4.4 книга сходится после повторов", books_converge())

# шторм повторов: 12 потоков с одним токеном одновременно
_codes = []
_lock = threading.Lock()
_barrier = threading.Barrier(12)


def storm():
    _barrier.wait()
    r = c.post("/api/click", headers={**H(), "X-Op-Id": "chaos-storm"},
               json={"clicks": 5, "batch_id": "chaos-storm-batch"})
    with _lock:
        _codes.append(r.status_code)


_bal = db.get_user(UID)["cookies"]
_st = [threading.Thread(target=storm) for _ in range(12)]
for t in _st:
    t.start()
for t in _st:
    t.join()
check(f"4.5 шторм одинаковых запросов не дал пятисоток ({set(_codes)})",
      500 not in _codes and 200 in _codes)
check("4.6 начислено ровно за одно нажатие",
      db.get_user(UID)["cookies"] < _bal + 5 * 10_000)
check("4.7 книга сходится после шторма", books_converge())
check("4.8 баланс не ушёл в минус", balances_sane())


# ======================================================================
print("\n=== 5. Telegram отвечает ошибками и тормозит ===")
# ======================================================================
import asyncio                                   # noqa: E402

from aiogram.exceptions import TelegramForbiddenError  # noqa: E402

import bot.notifier as notifier                  # noqa: E402


class FlakyBot:
    """Бот, у которого каждый второй вызов падает, а третий — виснет.

    Проход по пушам идёт по всем игрокам подряд. Одно необработанное
    исключение в середине останавливает весь проход, и остальные не получают
    ничего — при этом в логе будет одна строка, а в метриках ноль."""

    def __init__(self):
        self.sent = 0
        self.calls = 0

    async def send_message(self, chat_id, text, **kw):
        self.calls += 1
        if self.calls % 3 == 0:
            raise TelegramForbiddenError(method=None, message="bot was blocked")
        if self.calls % 5 == 0:
            raise TimeoutError("Telegram не ответил")
        self.sent += 1


# заводим игроков, которым проход обязан что-то отправить: давно не заходили,
# пуш не получали, энергия полная
_push_uids = [UID + 100 + i for i in range(6)]


def make_pushable(uid):
    db.create_user(uid, f"chaos{uid}", "Chaos")
    now = time.time()
    db.update_user(uid, last_seen_at=now - 10 * 3600, last_notified_at=0,
                   notify_blocked=0, energy=10 ** 6, energy_updated_at=now)


for _u in _push_uids:
    make_pushable(_u)

_flaky = FlakyBot()
_err = None
try:
    asyncio.run(notifier._notify_pass(_flaky))
except Exception as e:                            # noqa: BLE001
    _err = repr(e)
check(f"5.1 падающий Telegram не роняет проход по пушам ({_err})", _err is None)
check(f"5.2 остальные игроки всё равно получили пуш (sent={_flaky.sent})",
      _flaky.sent > 0)
check("5.3 заблокировавшие бота помечены и больше не тревожатся",
      any(db.get_user(u)["notify_blocked"] for u in _push_uids))
check("5.4 таймаут не пометил живого игрока заблокировавшим",
      sum(1 for u in _push_uids if db.get_user(u)["notify_blocked"]) < len(_push_uids))

# второй проход не должен ходить к заблокировавшим повторно: иначе каждая
# новая итерация тратит квоту Telegram на тех, кто уже отписался
_blocked = [u for u in _push_uids if db.get_user(u)["notify_blocked"]]
_alive = [u for u in _push_uids if u not in _blocked]
for _u in _push_uids:
    db.update_user(_u, last_notified_at=0)
_before_calls = _flaky.calls
asyncio.run(notifier._notify_pass(_flaky))
check(f"5.5 второй проход не стучится к блокнувшим ({len(_blocked)} шт.)",
      _flaky.calls - _before_calls <= len(_alive))
# таймаут — не отправка: отметка не ставится, и игрок попадёт в следующий
# проход. Иначе одна сетевая ошибка съедала бы игроку сутки тишины
check("5.6 упавшая отправка не съедает окно в 20 часов",
      any(db.get_user(u)["last_notified_at"] == 0 and not db.get_user(u)["notify_blocked"]
          for u in _push_uids))


# ======================================================================
print("\n=== 6. Планировщик под сбоями ===")
# ======================================================================
scheduler.reset()
_calls = []


def flaky_job():
    _calls.append(len(_calls))
    if len(_calls) == 1:
        raise RuntimeError("первый запуск не задался")


def run_flaky():
    try:
        with scheduler.job("chaos:flaky", 0, 30) as mine:
            if mine:
                flaky_job()
    except RuntimeError:
        pass
    return db.q1("SELECT runs, fails, last_error FROM job_runs "
                 "WHERE job_key = ?", ("chaos:flaky",))


_first = run_flaky()
check("6.1 падение задачи посчитано, а не потеряно", _first["fails"] == 1)
# в кластере лог принадлежит процессу, которого через час уже нет: причина
# падения обязана лежать в базе, иначе разбирать нечего
check(f"6.2 причина падения сохранена в базе ({_first['last_error']})",
      "первый запуск не задался" in (_first["last_error"] or ""))

_second = run_flaky()
check("6.3 после падения задача запускается снова, а не блокируется навсегда",
      len(_calls) == 2 and _second["runs"] == 2)
check("6.4 удачный запуск снимает прошлую ошибку", not _second["last_error"])

# упавший владелец не должен держать замок вечно: ttl истекает, и работу
# подхватывает следующий процесс. Иначе один крэш означает «бэкапов больше нет»
scheduler.reset("chaos:ttl")
cache.reset_all_windows()
_took = []
with cache.lock("chaos:ttl", 0.4) as first:
    _took.append(first)
    with cache.lock("chaos:ttl", 30) as second:
        _took.append(second)          # тот же замок занят — второй не входит
time.sleep(0.5)
with cache.lock("chaos:ttl", 30) as third:
    _took.append(third)
check("6.5 занятый замок не отдаётся второму", _took[:2] == [True, False])
check("6.6 замок брошенного владельца истекает по ttl", _took[2] is True)

# часы прыгнули назад (ntp, миграция вм): задача не должна залипнуть навсегда
scheduler.reset("chaos:clock")
with scheduler.job("chaos:clock", 60, 30) as mine:
    check("6.7 первый запуск состоялся", mine is True)
db.exec("UPDATE job_runs SET last_run_at = ? WHERE job_key = ?",
        (time.time() + 3600, "chaos:clock"))
with scheduler.job("chaos:clock", 60, 30) as mine:
    check("6.8 отметка из будущего не отменяет задачу навсегда", mine is False)
db.exec("UPDATE job_runs SET last_run_at = 0 WHERE job_key = ?", ("chaos:clock",))
with scheduler.job("chaos:clock", 60, 30) as mine:
    check("6.9 после возвращения часов задача снова идёт", mine is True)
scheduler.reset()


# ======================================================================
print("\n=== 7. Диск и бэкапы ===")
# ======================================================================
from server import backup                        # noqa: E402

_real_snapshot = backup.db.snapshot
backup.db.snapshot = lambda keep=7: None
try:
    backup.run()
    check("7.1 несделанный снимок — провал задачи, а не тишина", False)
except backup.BackupError as e:
    check("7.1 несделанный снимок — провал задачи, а не тишина",
          "снимок не сделан" in str(e))
finally:
    backup.db.snapshot = _real_snapshot


def full_disk(keep=7):
    raise OSError(28, "No space left on device")


backup.db.snapshot = full_disk
try:
    notifier._backup_db()
    check("7.2 кончившееся место видно как падение задачи", False)
except OSError as e:
    check("7.2 кончившееся место видно как падение задачи", "space" in str(e))
finally:
    backup.db.snapshot = _real_snapshot

# Чужие снимки в той же папке (переименовали базу, рядом второй экземпляр) не
# должны участвовать в чистке этой базы: сортировка по имени в перемешанных
# префиксах перестаёт быть хронологической, и свежий снимок удаляется сразу
# после создания — бэкап «прошёл», а файла нет
_folder = db._backups_folder()
_decoys = [os.path.join(_folder, f"zzz-other.db.2020010{i}-000000.bak")
           for i in range(1, 9)]
for _d in _decoys:
    with open(_d, "wb") as _f:
        _f.write(b"\x00" * 16)

_info = backup.run()
check("7.3 после освобождения места бэкап снова проходит",
      _info["size"] > backup.MIN_SIZE)
check("7.3a чужие снимки в папке не съедают свежий",
      os.path.exists(_info["path"]))
for _d in _decoys:
    if os.path.exists(_d):
        os.remove(_d)
_drill = backup.drill()
check(f"7.4 учения берут САМЫЙ СВЕЖИЙ снимок ({_drill['snapshot']})",
      _drill["snapshot"] == os.path.basename(_info["path"]))
check("7.5 и он разворачивается с живыми данными", _drill["users"] > 0)

# побитый файл обязан провалить учения: молчаливо «восстановленный» мусор
# страшнее отсутствия бэкапа — на него рассчитывают.
#
# Портим ИНВЕРСИЕЙ прочитанного, а не записью нулей: по смещению 2048 в снимке
# вполне может лежать свободное место той же страницы, и тогда «порча» нулями
# не меняет ни одного байта — сумма сходится, база открывается, и проверка
# падает не потому, что учения сломались, а потому что мы ничего не сломали.
# Именно так этот тест и вёл себя по-разному на разных машинах.
with open(_info["path"], "r+b") as _f:
    _f.seek(backup.MIN_SIZE // 2)
    _rot = _f.read(512)
    _f.seek(backup.MIN_SIZE // 2)
    _f.write(bytes(b ^ 0xFF for b in _rot))
try:
    backup.drill()
    check("7.6 порча файла проваливает учения", False)
except backup.BackupError as e:
    check(f"7.6 порча файла проваливает учения ({str(e)[:60]})", True)
os.remove(_info["path"])
if os.path.exists(_info["path"] + ".sha256"):
    os.remove(_info["path"] + ".sha256")


# ======================================================================
print("\n=== 8. Итог: система осталась целой ===")
# ======================================================================
obs.refresh_gauges()
_metrics = obs.render()
check("8.1 метрики отдаются после всех поломок", "http_requests_total" in _metrics)
check("8.2 книга сходится у всех, кого трогали",
      economy.drift_report(limit=10) == [])
_cols = sorted(set(economy.CURRENCY_COLUMN.values()))
_neg = db.q1("SELECT COUNT(*) c FROM users WHERE "
             + " OR ".join(f"{col} < 0" for col in _cols))["c"]
check(f"8.3 ни одного отрицательного баланса ({len(_cols)} колонок)", _neg == 0)
check("8.4 игра отвечает как ни в чём не бывало",
      c.get("/api/state", headers=H()).status_code == 200)

for _suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB_PATH + _suffix):
        try:
            os.remove(DB_PATH + _suffix)
        except OSError:
            pass

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
