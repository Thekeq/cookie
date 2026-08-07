"""Состояние, общее для всех процессов: лимитер запросов и владение задачами.

Зачем вообще. До этого шага игра держалась на допущении «один процесс»:
лимитер жил словарём в памяти, фоновые задачи запускал тот же процесс, что и
API, — и всё это было верно. Второй воркер ломает ровно это: лимит становится
N-кратным (восемь воркеров — восьмикратный перебор промокодов и восьмикратная
нагрузка на тяжёлые ручки), а ролловер сезона стартует в каждом процессе
одновременно, и все они дерутся за один write-lock базы.

Здесь три примитива, которых для этого достаточно:
  * incr_window — скользящее окно счётчика (лимитер);
  * lock — «эту работу делаю я» (единственный владелец фоновой задачи);
  * get/setex (и обёртки get_json/set_json) — значение с TTL, то есть общий кэш.

Фолбэк в память — не заглушка, а рабочий режим. Без REDIS_URL всё работает
ровно как раньше: один процесс, состояние в памяти. Redis обязателен только
когда воркеров больше одного, и об этом отказывается запускаться
settings.problems(). Так дев и тесты не начинают требовать вторую службу.

Клиент СИНХРОННЫЙ намеренно. Весь код игры синхронный (SQLite тоже), и async-
клиент заставил бы протащить await через lock() и через check_rate_limit,
который зовётся в том числе из недр игровой логики. Одна лишняя блокирующая
операция на запрос там, где уже есть десятки синхронных SQL-вызовов, ничего не
меняет; протаскивание await через половину кодовой базы — меняет.

Поведение при недоступности Redis РАЗНОЕ, и это главное решение файла:
  * лимитер — fail open: моргнувший Redis не должен закрывать игру всем
    игрокам. Пропустить лишний запрос дешевле, чем массовый 429;
  * кэш — fail open в память процесса: значение с TTL по определению можно
    пересчитать, худшее последствие отказа — лишний запрос в базу;
  * lock — fail closed: не смогли убедиться, что владелец один — не работаем.
    Пропущенный тик планировщика стоит 15 минут, ролловер сезона, запущенный
    дважды, стоит пересчёта прогресса всем игрокам.
"""
import json
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager

from server import settings

log = logging.getLogger(__name__)

# Префикс всех ключей: один Redis обычно обслуживает не одну службу, и без
# префикса FLUSHDB соседа уносит и наши.
PREFIX = "cookie:"

# Redis упал — не долбим его на каждом запросе: соединение с таймаутом на
# каждый вызов превратит деградацию в полную остановку. Пробуем раз в 5 секунд.
_RETRY_AFTER = 5.0

_client = None            # redis.Redis | None
_client_ready = False     # пытались ли уже подключиться
_down_until = 0.0
_lock = threading.Lock()


def _connect():
    """Подключение к Redis или None, если его нет и не должно быть."""
    if not settings.REDIS_URL:
        return None
    try:
        import redis
    except ImportError:
        # REDIS_URL задан, а пакета нет — это не «работаем без Redis», это
        # незамеченная опечатка в деплое. Кричим в лог, но не роняем процесс:
        # игра на фолбэке останется играбельной, пока владелец не увидит.
        log.error("REDIS_URL задан, но пакет redis не установлен — "
                  "работаем на in-process фолбэке (pip install redis)")
        return None
    # Таймауты обязательны: без них зависший Redis подвешивает весь event loop,
    # то есть и API, и бота. Лучше быстро ошибиться и уйти на фолбэк.
    return redis.Redis.from_url(
        settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=2, socket_timeout=2,
        health_check_interval=30, retry_on_timeout=False)


def client():
    """Живой клиент или None. Подключается лениво и один раз на процесс."""
    global _client, _client_ready, _down_until
    if _client_ready:
        if _client is None:
            return None
        if _down_until and time.time() < _down_until:
            return None
        return _client
    with _lock:
        if not _client_ready:
            _client = _connect()
            _client_ready = True
    return _client


def _failed(where: str, exc: Exception):
    """Отметить Redis недоступным и не ходить в него следующие секунды."""
    global _down_until
    if not _down_until or time.time() > _down_until:
        log.warning("redis %s: %s — уходим на фолбэк на %.0f с",
                    where, exc, _RETRY_AFTER)
    _down_until = time.time() + _RETRY_AFTER


def enabled() -> bool:
    """Работаем ли мы сейчас на общем состоянии, а не на памяти процесса."""
    return client() is not None


# ---------- скользящее окно (лимитер) ----------

# Фолбэк: те же структуры, что были в game_logic до этого шага.
_windows: dict[str, list[float]] = {}
_gc_calls = 0
_GC_EVERY = 500


def _incr_window_local(key: str, limit: int, window: float) -> tuple[bool, int]:
    global _gc_calls
    now = time.time()
    hits = [t for t in _windows.get(key, ()) if now - t < window]
    hits.append(now)          # свой вызов считаем ДО решения: тот, кто долбит
    _windows[key] = hits      # ручку сверх лимита, продлевает себе блокировку
    _gc_calls += 1
    if _gc_calls % _GC_EVERY == 0:
        # без уборки словарь растёт по ключу на игрока и не уменьшается никогда
        for k, ts in list(_windows.items()):
            if not ts or now - ts[-1] > 3600:
                _windows.pop(k, None)
    return len(hits) <= limit, len(hits)


def incr_window(key: str, limit: int, window: float) -> tuple[bool, int]:
    """(разрешено, счётчик) для скользящего окна `window` секунд.

    Именно скользящее, а не окно с фиксированным началом: у фиксированного на
    стыке двух окон проходит до 2*limit запросов за доли секунды, а лимитер
    здесь стоит в том числе перед перебором промокодов.

    Свой вызов учитывается всегда, даже когда он уже за лимитом — так у того,
    кто продолжает долбить ручку, окно не освобождается.
    """
    r = client()
    if r is None:
        return _incr_window_local(key, limit, window)
    full = PREFIX + "w:" + key
    now = time.time()
    try:
        pipe = r.pipeline(transaction=False)
        pipe.zremrangebyscore(full, 0, now - window)
        pipe.zadd(full, {f"{now:.6f}:{uuid.uuid4().hex[:8]}": now})
        pipe.zcard(full)
        # TTL на ключ: иначе в Redis навсегда останется по множеству на каждого
        # игрока, который однажды зашёл. +1 с — запас на дробное окно
        pipe.expire(full, int(math.ceil(window)) + 1)
        count = int(pipe.execute()[2])
    except Exception as e:
        # fail open: моргнувший Redis не должен закрыть игру всем сразу
        _failed("incr_window", e)
        return _incr_window_local(key, limit, window)
    return count <= limit, count


def reset_window(key: str):
    """Сброс окна (нужен тестам и админке)."""
    _windows.pop(key, None)
    r = client()
    if r is not None:
        try:
            r.delete(PREFIX + "w:" + key)
        except Exception as e:
            _failed("reset_window", e)


def reset_all_windows():
    """Сброс всех окон лимитера.

    Нужен тестам (проверка лимита обязана начинаться с чистого счётчика) и как
    аварийная кнопка: если лимит выставили слишком строгим и половина игроков
    сидит в 429, ждать истечения окон — плохой план. Ходим SCAN, а не KEYS:
    KEYS на боевом Redis блокирует его целиком на время обхода."""
    _windows.clear()
    r = client()
    if r is None:
        return
    try:
        for key in r.scan_iter(match=PREFIX + "w:*", count=500):
            r.delete(key)
    except Exception as e:
        _failed("reset_all_windows", e)


# ---------- значение с TTL (общий кэш) ----------

# Фолбэк кэша. Держим ЗАКОДИРОВАННУЮ строку, а не объект: с объектом читатели
# получали бы один и тот же словарь и правка одного портила бы кэш остальным
# (лидерборд, например, дописывает в строки is_me на каждый запрос). Через
# Redis такой аварии быть не может по построению — пусть и без него поведение
# будет тем же.
_kv: dict[str, tuple[float, str]] = {}     # key -> (когда протухнет, значение)
_KV_MAX = 256                              # потолок: кэш не должен течь


def _kv_get_local(key: str) -> str | None:
    hit = _kv.get(key)
    if hit is None:
        return None
    if hit[0] <= time.time():
        _kv.pop(key, None)
        return None
    return hit[1]


def _kv_set_local(key: str, ttl: float, value: str):
    now = time.time()
    if len(_kv) >= _KV_MAX:
        for k, (expires, _v) in list(_kv.items()):
            if expires <= now:
                _kv.pop(k, None)
        if len(_kv) >= _KV_MAX:
            _kv.clear()     # ключей больше, чем мы готовы помнить, — начинаем с нуля
    _kv[key] = (now + ttl, value)


def get(key: str) -> str | None:
    """Значение или None, если его нет либо срок вышел."""
    r = client()
    if r is None:
        return _kv_get_local(key)
    try:
        return r.get(PREFIX + "kv:" + key)
    except Exception as e:
        _failed("get", e)
        return _kv_get_local(key)


def setex(key: str, ttl: float, value: str) -> bool:
    """Положить значение на ttl секунд. False — легло только в память процесса.

    В память пишем ТОЛЬКО когда Redis не ответил: иначе процесс держал бы
    вторую копию каждого значения, а после падения Redis отдавал бы из неё
    данные, которых остальные воркеры уже не видят.

    TTL округляем вверх и не даём опуститься ниже секунды: SETEX с нулём
    Redis считает ошибкой, а не «положить и сразу забыть»."""
    r = client()
    if r is not None:
        try:
            r.setex(PREFIX + "kv:" + key, max(1, int(math.ceil(ttl))), value)
            return True
        except Exception as e:
            _failed("setex", e)
    _kv_set_local(key, ttl, value)
    return False


def delete(key: str):
    """Забыть значение (инвалидация кэша, тесты, админка)."""
    _kv.pop(key, None)
    r = client()
    if r is not None:
        try:
            r.delete(PREFIX + "kv:" + key)
        except Exception as e:
            _failed("delete", e)


def get_json(key: str):
    """Разобранное значение или None.

    Мусор в кэше (обрезанная запись, значение от прошлой версии кода со сменой
    формата) читается как промах, а не как исключение: ручка обязана пережить
    порченый кэш, пересчитав данные."""
    raw = get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        log.warning("кэш %s: значение не разбирается — считаем промахом", key)
        return None


def set_json(key: str, ttl: float, value) -> bool:
    """Положить сериализуемое значение на ttl секунд."""
    try:
        raw = json.dumps(value, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        log.error("кэш %s: значение не сериализуется (%s) — не кэшируем", key, e)
        return False
    return setex(key, ttl, raw)


# ---------- владение задачей ----------

_locks: dict[str, tuple[str, float]] = {}   # name -> (token, expires_at)


def _acquire_local(name: str, ttl: float, token: str) -> bool:
    now = time.time()
    with _lock:
        held = _locks.get(name)
        if held and held[1] > now:
            return False
        _locks[name] = (token, now + ttl)
        return True


def _release_local(name: str, token: str):
    with _lock:
        if _locks.get(name, ("", 0))[0] == token:
            _locks.pop(name, None)


@contextmanager
def lock(name: str, ttl: float):
    """`with lock("x", 600) as mine:` — mine True только у одного владельца.

    ttl — страховка от смерти владельца: если процесс убили посреди работы,
    замок сам освободится через ttl, а не заблокирует задачу навсегда. Поэтому
    ttl должен быть заметно больше самой долгой работы под этим замком.

    Отпускаем только СВОЙ замок (сверяем токен): если работа затянулась дольше
    ttl и замок успел перейти к другому процессу, снимать его чужими руками
    нельзя — иначе на задаче окажется сразу двое.

    Redis недоступен — считаем, что замок НЕ взят (fail closed). Единственность
    владельца проверить нечем, а фоновые задачи здесь такие, что выполнить их
    дважды дороже, чем пропустить тик.
    """
    token = uuid.uuid4().hex
    r = client()
    if r is None:
        # Без Redis процесс один (WEB_CONCURRENCY > 1 без REDIS_URL не
        # запускается вовсе), поэтому память процесса — достаточная правда
        if not settings.REDIS_URL:
            mine = _acquire_local(name, ttl, token)
            try:
                yield mine
            finally:
                if mine:
                    _release_local(name, token)
            return
        # REDIS_URL задан, но недоступен: воркеров может быть много
        log.warning("замок %s не взят: redis недоступен", name)
        yield False
        return

    full = PREFIX + "lock:" + name
    try:
        mine = bool(r.set(full, token, nx=True, ex=int(math.ceil(ttl))))
    except Exception as e:
        _failed("lock", e)
        yield False
        return
    try:
        yield mine
    finally:
        if mine:
            _release_redis(r, full, token)


_UNLOCK_LUA = ("if redis.call('get', KEYS[1]) == ARGV[1] then "
               "return redis.call('del', KEYS[1]) else return 0 end")


def _release_redis(r, full: str, token: str):
    """Снять свой замок.

    Сверка токена и удаление одним шагом (Lua): между GET и DEL замок мог
    истечь и достаться другому процессу, и тогда DEL снял бы ЧУЖОЙ замок —
    на задаче оказалось бы двое.

    Если EVAL запрещён (managed-Redis с урезанным набором команд, прокси),
    делаем то же в два шага. Окно на гонку появляется, но выбор здесь между
    маленьким окном и замком, который не снимается вообще."""
    try:
        r.eval(_UNLOCK_LUA, 1, full, token)
        return
    except Exception as e:
        if "unknown command" not in str(e).lower() and "not allowed" not in str(e).lower():
            _failed("unlock", e)
            return
    try:
        if r.get(full) == token:
            r.delete(full)
    except Exception as e:
        _failed("unlock", e)


# ---------- служебное ----------

def health() -> dict:
    """Для /healthz: на чём мы сейчас работаем."""
    if not settings.REDIS_URL:
        return {"backend": "in-process"}
    r = client()
    if r is None:
        return {"backend": "in-process", "redis": "down"}
    try:
        r.ping()
        return {"backend": "redis", "redis": "up"}
    except Exception as e:
        _failed("ping", e)
        return {"backend": "in-process", "redis": f"down: {e}"}


def _reset_for_tests():
    """Забыть подключение и фолбэк-состояние (только для тестов)."""
    global _client, _client_ready, _down_until
    with _lock:
        _client = None
        _client_ready = False
        _down_until = 0.0
        _windows.clear()
        _locks.clear()
        _kv.clear()
