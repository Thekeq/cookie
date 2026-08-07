"""Наблюдаемость: корреляция запросов, метрики, структурные логи.

Зачем отдельным модулем. До этого шага единственным следом происходящего был
текстовый лог одного процесса. Пока процесс один, этого хватает: «упало» видно
глазами. После разделения ролей и появления воркеров вопрос звучит иначе — «на
каком из шести процессов и в каком из запросов», — и текстовый лог на него не
отвечает в принципе: строки от разных запросов перемешаны, а строки от разных
процессов лежат в разных файлах.

Три вещи, которые это чинят, и все три здесь:

  * ИДЕНТИФИКАТОР ЗАПРОСА. Заводится на входе, живёт в contextvar и подставляется
    в КАЖДУЮ строку лога автоматически (фильтром, а не руками на call site) и в
    заголовок ответа. По нему собирается вся история одного нажатия кнопки —
    включая то, что записали игровые модули, ничего не знающие про HTTP.

  * МЕТРИКИ. Счётчики и гистограммы в памяти процесса + выгрузка в формате
    Prometheus. Главная тонкость — воркеров несколько, а слушающий сокет один:
    scrape попадает в СЛУЧАЙНЫЙ воркер, и метрики из его памяти описывали бы
    1/N трафика, причём каждый раз другую. Поэтому при живом Redis процессы
    досылают туда свои приросты, а /metrics отдаёт сумму по всем. Без Redis
    (одиночный запуск) всё остаётся в памяти и ничего не теряется.

  * ГОТОВНОСТЬ ОТДЕЛЬНО ОТ ЖИВОСТИ. Живость — «процесс не завис», её проверяют
    часто и по ней ПЕРЕЗАПУСКАЮТ. Готовность — «этому процессу можно давать
    трафик», по ней выводят из балансировки. Смешивать их опасно: недоступная
    на минуту база — это не повод перезапускать все воркеры разом (перезапуск её
    не чинит, а очередь запросов теряется), это повод перестать слать им
    трафик. Сами ручки живут в main.py, здесь — то, что они считают.

Метрики намеренно свои, а не prometheus_client: нужны ровно счётчик, гистограмма
и датчик, зато нужна сумма по процессам через Redis, которой в prometheus_client
нет (там для этого отдельный multiprocess-режим с каталогом mmap-файлов, общим
для процессов на ОДНОЙ машине). Зависимость к тому же тянется в оба процесса и
на дев-машины.

Модуль не импортирует ни db, ни игровую логику: его тянет в том числе db.py.
"""
import bisect
import json
import logging
import os
import time
import uuid
from contextvars import ContextVar

from server import cache, settings

log = logging.getLogger(__name__)

# Префикс ключей в Redis — тот же, что у остального разделяемого состояния.
# METRICS_NAMESPACE разводит по разным полкам парк и канарейку: состояние
# лимитера у них общее намеренно, а счётчики — нет, иначе доля отказов новой
# версии считалась бы вместе со старой и не была бы видна вовсе.
_NS = (settings.METRICS_NAMESPACE + ":") if settings.METRICS_NAMESPACE else ""
_COUNTER_KEY = cache.PREFIX + "m:" + _NS + "counter"
_HIST_KEY = cache.PREFIX + "m:" + _NS + "hist"


# ---------- корреляция ----------

_req_id: ContextVar[str] = ContextVar("req_id", default="")
_req_user: ContextVar[int] = ContextVar("req_user", default=0)
# Платформа и страна запроса — (platform, country). Живут тут, а не
# прокидываются аргументами, ровно по той же причине, что и req_id: их пишет
# аналитика из игровых модулей, которые про HTTP не знают вовсе, и передавать
# их через десяток сигнатур значило бы получить их там, где кто-то не забыл.
_req_client: ContextVar[tuple] = ContextVar("req_client", default=("", ""))

# Значения приезжают снаружи и попадают в базу — чистим так же, как req_id.
_CLIENT_SAFE = "abcdefghijklmnopqrstuvwxyz0123456789_-"


def clean_client_tag(raw: str, limit: int = 16) -> str:
    """Метка платформы/страны в безопасном виде: строчная латиница, без мусора."""
    return "".join(c for c in (raw or "").lower() if c in _CLIENT_SAFE)[:limit]

# Идентификатор запроса приходит снаружи (reverse proxy обычно уже его ставит) и
# попадает в логи, поэтому чистим его так же, как X-Op-Id: в журнал не должно
# попасть ничего, что переносит строку или притворяется соседним полем.
_ID_SAFE = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."


def new_request_id(raw: str = "") -> str:
    """Идентификатор запроса: чужой, если прислали годный, иначе свой."""
    clean = "".join(c for c in (raw or "") if c in _ID_SAFE)[:64]
    return clean or uuid.uuid4().hex[:16]


def bind_request(req_id: str, user_id: int = 0, platform: str = "",
                 country: str = ""):
    """Привязать контекст к текущей задаче. Возвращает токены для сброса.

    Сбрасывать обязательно: воркер обслуживает следующий запрос в той же
    корутине не всегда, но contextvar без сброса живёт до конца задачи, и в
    фоновых строках лога остался бы чужой идентификатор."""
    return (_req_id.set(req_id), _req_user.set(user_id),
            _req_client.set((clean_client_tag(platform),
                             clean_client_tag(country, 8))))


def bind_user(user_id: int):
    """Игрок становится известен уже после аутентификации, а не на входе."""
    _req_user.set(int(user_id or 0))


def reset_request(tokens):
    _req_id.reset(tokens[0])
    _req_user.reset(tokens[1])
    # длина проверяется: bind_request зовут и старым способом (два токена) —
    # например тесты и фоновые задачи, у которых клиента нет вовсе
    if len(tokens) > 2:
        _req_client.reset(tokens[2])


def current_request_id() -> str:
    return _req_id.get()


def current_client() -> tuple:
    """(платформа, страна) текущего запроса; ('', '') вне запроса."""
    return _req_client.get()


class ContextFilter(logging.Filter):
    """Подставляет req_id/user_id в каждую запись.

    Именно фильтром: строчки пишут два десятка модулей, половина из которых про
    HTTP не знает вовсе, и требовать от них передавать extra= означало бы, что
    в трассировке будет ровно то, что кто-то не забыл добавить."""

    def filter(self, record):
        record.req_id = _req_id.get()
        record.user_id = _req_user.get()
        record.role = settings.ROLE
        record.pid = os.getpid()
        return True


class JsonFormatter(logging.Formatter):
    """Одна строка — один JSON-объект.

    Текстовый лог читается глазами, но не читается машиной: grep по нему находит
    строку, а не запрос, и «покажи все 500 за час с их req_id» на нём не
    делается. JSON включается LOG_JSON=1 — на дев-машине он неудобен, поэтому по
    умолчанию остаётся прежний текст."""

    def format(self, record):
        out = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "role": getattr(record, "role", ""),
            "pid": getattr(record, "pid", 0),
        }
        if getattr(record, "req_id", ""):
            out["req_id"] = record.req_id
        if getattr(record, "user_id", 0):
            out["user_id"] = record.user_id
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


# ---------- реестр метрик ----------

# Границы гистограмм в секундах. Верхняя — 10 с: всё, что дольше, попадает в
# +Inf и видно как «запрос не уложился ни в какой разумный срок».
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

# name -> (тип, пояснение). Реестр статический: метрика, о которой не написано,
# что она значит, через месяц не читается никем, включая автора.
META = {
    "http_requests_total": ("counter", "HTTP-запросы по маршруту и коду ответа"),
    "http_request_duration_seconds": ("histogram", "Время ответа API"),
    "http_requests_in_flight": ("gauge", "Запросов в обработке прямо сейчас"),
    "db_queries_total": ("counter", "Запросы к базе по типу"),
    "db_query_seconds": ("histogram", "Время запроса к базе"),
    "db_tx_retries_total": ("counter", "Повторы BEGIN IMMEDIATE (база занята)"),
    "db_dirty_connections_total": ("counter", "Соединения, выброшенные с "
                                   "залипшей транзакцией"),
    "economy_minted_total": ("counter", "Начислено валюты по книге операций"),
    "economy_spent_total": ("counter", "Списано валюты по книге операций"),
    "economy_refunded_total": ("counter", "Возвращено валюты (refund Stars)"),
    "economy_ops_total": ("counter", "Токены идемпотентности: new/replay/conflict"),
    "job_last_ok_age_seconds": ("gauge", "Сколько прошло с успеха фоновой задачи"),
    "job_runs_total": ("gauge", "Запусков фоновой задачи всего"),
    "job_fails_total": ("gauge", "Падений фоновой задачи всего"),
    "cache_backend_up": ("gauge", "1 — разделяемое состояние в Redis, 0 — в памяти"),
    "notifications_total": ("counter", "Пуши по исходу отправки"),
    "backup_total": ("counter", "Бэкапы по исходу: ok/fail"),
    "backup_size_bytes": ("gauge", "Размер последнего снимка"),
    "backup_age_seconds": ("gauge", "Возраст самого свежего снимка"),
    "backup_drill_total": ("counter", "Учения по восстановлению: ok/fail"),
}

_counters: dict[tuple, float] = {}
_hists: dict[tuple, list] = {}
_gauges: dict[tuple, float] = {}

# Что уже отдано в Redis. Досылаем ПРИРОСТ, а не итог: итог перетирал бы вклад
# остальных воркеров, а прирост складывается с ними HINCRBYFLOAT.
_sent_counters: dict[tuple, float] = {}
_sent_hists: dict[tuple, list] = {}


def _key(name: str, labels: dict) -> tuple:
    return name, tuple(sorted((k, str(v)) for k, v in labels.items()))


def inc(name: str, value: float = 1.0, **labels):
    """Счётчик: только вверх. Сброс происходит с процессом, и это нормально —
    Prometheus умеет считать rate() через сброс."""
    k = _key(name, labels)
    _counters[k] = _counters.get(k, 0.0) + value


def observe(name: str, seconds: float, **labels):
    """Гистограмма: раскладывает наблюдение по границам BUCKETS.

    Именно гистограмма, а не среднее: среднее время ответа скрывает ровно то,
    ради чего его смотрят. Тысяча запросов по 20 мс и десять по 8 с дают
    приличное среднее и совершенно негодный p99."""
    k = _key(name, labels)
    row = _hists.get(k)
    if row is None:
        row = _hists[k] = [0.0] * (len(BUCKETS) + 2)   # ...корзины, sum, count
    # Корзины НАКОПИТЕЛЬНЫЕ (так требует формат): наблюдение попадает во все
    # границы не меньше себя. Первую из них ищем бинарно — observe зовётся на
    # каждый запрос к базе, и линейный проход по границам был бы самой частой
    # операцией в процессе.
    for i in range(bisect.bisect_left(BUCKETS, seconds), len(BUCKETS)):
        row[i] += 1
    row[-2] += seconds
    row[-1] += 1


def set_gauge(name: str, value: float, **labels):
    """Датчик: текущее значение. В Redis не сводится — это либо свойство
    процесса (запросов в работе), либо величина, которую все процессы читают из
    базы и посчитали бы одинаково (возраст последнего бэкапа)."""
    _gauges[_key(name, labels)] = float(value)


def add_gauge(name: str, delta: float, **labels):
    k = _key(name, labels)
    _gauges[k] = _gauges.get(k, 0.0) + delta


# ---------- сведение по процессам ----------

def _field(name: str, labels: tuple, extra=None) -> str:
    """Имя поля в hash'е Redis. JSON, а не 'name|k=v': значение метки — это в
    том числе маршрут и причина операции, и любой самодельный разделитель рано
    или поздно встретится внутри значения."""
    return json.dumps([name, list(labels), extra], ensure_ascii=False)


def flush() -> bool:
    """Дослать приросты в Redis. False — не смогли (или Redis не настроен).

    Приросты списываем ТОЛЬКО после успеха: иначе моргнувший Redis навсегда
    съедал бы часть трафика из метрик."""
    r = cache.client()
    if r is None:
        return False
    payload_c = [(k, v - _sent_counters.get(k, 0.0)) for k, v in _counters.items()]
    payload_c = [(k, d) for k, d in payload_c if d]
    payload_h = []
    for k, row in _hists.items():
        prev = _sent_hists.get(k) or [0.0] * len(row)
        for i, v in enumerate(row):
            if v - prev[i]:
                payload_h.append((k, i, v - prev[i]))
    if not payload_c and not payload_h:
        return True
    try:
        pipe = r.pipeline(transaction=False)
        for (name, labels), delta in payload_c:
            pipe.hincrbyfloat(_COUNTER_KEY, _field(name, labels), delta)
        for (name, labels), i, delta in payload_h:
            pipe.hincrbyfloat(_HIST_KEY, _field(name, labels, i), delta)
        pipe.execute()
    except Exception as e:
        log.warning("метрики: не удалось дослать в redis: %s", e)
        return False
    for k, _ in payload_c:
        _sent_counters[k] = _counters[k]
    for k, row in _hists.items():
        _sent_hists[k] = list(row)
    return True


def _shared_totals():
    """(счётчики, гистограммы) из Redis или (None, None), если его нет."""
    r = cache.client()
    if r is None:
        return None, None
    try:
        raw_c = r.hgetall(_COUNTER_KEY)
        raw_h = r.hgetall(_HIST_KEY)
    except Exception as e:
        log.warning("метрики: не удалось прочитать redis: %s", e)
        return None, None
    counters: dict[tuple, float] = {}
    hists: dict[tuple, list] = {}
    for field, value in raw_c.items():
        name, labels, _ = json.loads(field)
        counters[(name, tuple(tuple(p) for p in labels))] = float(value)
    for field, value in raw_h.items():
        name, labels, idx = json.loads(field)
        k = (name, tuple(tuple(p) for p in labels))
        row = hists.setdefault(k, [0.0] * (len(BUCKETS) + 2))
        row[int(idx)] = float(value)
    return counters, hists


def reset_shared():
    """Забыть сведённые метрики (тесты и «начать счёт заново»)."""
    _counters.clear()
    _hists.clear()
    _gauges.clear()
    _sent_counters.clear()
    _sent_hists.clear()
    r = cache.client()
    if r is not None:
        try:
            r.delete(_COUNTER_KEY, _HIST_KEY)
        except Exception as e:
            log.warning("метрики: не удалось очистить redis: %s", e)


# ---------- выгрузка ----------

def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels_str(labels, extra: tuple = ()) -> str:
    pairs = [f'{k}="{_escape(str(v))}"' for k, v in (*extra, *labels)]
    return "{" + ",".join(pairs) + "}" if pairs else ""


def render() -> str:
    """Текст в формате Prometheus по ВСЕМ процессам, какие удалось собрать.

    Сначала дописываем свой прирост, потом читаем сумму: иначе scrape всегда
    отставал бы ровно на то, что этот процесс успел с прошлого раза."""
    flush()
    counters, hists = _shared_totals()
    if counters is None:
        counters, hists = dict(_counters), {k: list(v) for k, v in _hists.items()}
    lines = []
    for name, (kind, help_text) in sorted(META.items()):
        rows_c = {k: v for k, v in counters.items() if k[0] == name}
        rows_h = {k: v for k, v in hists.items() if k[0] == name}
        rows_g = {k: v for k, v in _gauges.items() if k[0] == name}
        if not rows_c and not rows_h and not rows_g:
            continue
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for (_, labels), value in sorted(rows_c.items()):
            lines.append(f"{name}{_labels_str(labels)} {value:g}")
        for (_, labels), value in sorted(rows_g.items()):
            lines.append(f"{name}{_labels_str(labels)} {value:g}")
        for (_, labels), row in sorted(rows_h.items()):
            for i, edge in enumerate(BUCKETS):
                le = ("le", f"{edge:g}")
                lines.append(f"{name}_bucket{_labels_str(labels, (le,))} {row[i]:g}")
            lines.append(f"{name}_bucket{_labels_str(labels, (('le', '+Inf'),))} "
                         f"{row[-1]:g}")
            lines.append(f"{name}_sum{_labels_str(labels)} {row[-2]:g}")
            lines.append(f"{name}_count{_labels_str(labels)} {row[-1]:g}")
    return "\n".join(lines) + "\n"


def refresh_gauges():
    """Датчики, которые считаются не по ходу работы, а по состоянию базы.

    Вынесены сюда и вызываются перед выгрузкой: держать их актуальными
    постоянно незачем, а вот отвечать на «когда последний раз проходил бэкап»
    /metrics обязан — это ровно тот вопрос, который задают после инцидента.
    Импорт локальный: obs тянет db.py, и обратная зависимость закольцевала бы
    старт."""
    import db as db_module

    set_gauge("cache_backend_up", 1 if cache.enabled() else 0)
    try:
        rows = db_module.shared().q(
            "SELECT job_key, last_ok_at, runs, fails FROM job_runs")
    except Exception as e:
        log.warning("метрики: job_runs недоступна: %s", e)
        return
    now = time.time()
    for r in rows:
        job = r["job_key"]
        # last_ok_at = 0 значит «успеха не было ни разу»: возраст в этом случае
        # равен всему времени существования отметки, и показывать его как
        # «55 лет с 1970-го» честнее, чем как ноль
        set_gauge("job_last_ok_age_seconds", now - (r["last_ok_at"] or 0), job=job)
        set_gauge("job_runs_total", r["runs"] or 0, job=job)
        set_gauge("job_fails_total", r["fails"] or 0, job=job)

    # Возраст снимка считается по ФАЙЛУ, а не по отметке задачи: задача может
    # отчитаться об успехе, не создав ничего (нет pg_dump, кончилось место), и
    # тогда единственный честный ответ на «что мы восстановим» — время файла.
    try:
        folder = db_module.shared()._backups_folder()
        stamps = [os.path.getmtime(os.path.join(folder, f))
                  for f in os.listdir(folder) if not f.endswith(".sha256")]
        if stamps:
            set_gauge("backup_age_seconds", now - max(stamps))
    except OSError:
        pass  # каталога нет — бэкапов не было ни одного, датчик не выставляем


# ---------- Sentry ----------

def init_sentry():
    """Включается только заполненным SENTRY_DSN.

    Пакета нет, а DSN задан — это не «работаем без Sentry», это незамеченная
    опечатка в деплое: ошибки будут молча уходить в никуда ровно тогда, когда
    они нужнее всего. Кричим в лог, но процесс не роняем."""
    if not settings.SENTRY_DSN:
        return False
    try:
        import sentry_sdk
    except ImportError:
        log.error("SENTRY_DSN задан, но пакет sentry-sdk не установлен — "
                  "ошибки никуда не отправляются (pip install sentry-sdk)")
        return False
    sentry_sdk.init(dsn=settings.SENTRY_DSN, environment=settings.ROLE,
                    # события с телом запроса не отправляем: в теле лежит
                    # initData, то есть действующий пропуск в чужой аккаунт
                    send_default_pii=False,
                    traces_sample_rate=0.0)
    log.info("sentry: включён (role=%s)", settings.ROLE)
    return True
