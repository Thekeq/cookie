"""Канареечная выкладка: новая версия сначала берёт долю трафика, а не весь.

Зачем. До этого шага релиз выглядел так: `git pull`, `systemctl restart`,
надежда. Проблема не в том, что что-то сломается — сломается обязательно, — а в
том, КОГО это накроет. При обычной выкладке первым же неудачным запросом
накрывает всех, и узнаём мы об этом от игроков, а исправляем руками, в панике и
без плана. Канарейка меняет только одно, зато главное: новую версию сначала
видит 5-10% трафика, решение «жива или нет» принимается по цифрам, а не по
ощущению, и откат — это одна команда, которая уже написана и уже проверена.

ЧТО ЗДЕСЬ СРАВНИВАЕТСЯ. Не «канарейка против порога», а «канарейка против
парка за то же самое окно». Абсолютный порог в одиночку врёт в обе стороны:
ночью 200 мс — это норма, а в час пик та же цифра означает беду; и наоборот,
если легла база, плохо станет ВСЕМ, и откат релиза в этот момент — не спасение,
а второй инцидент поверх первого. Поэтому провал — это «хуже парка во столько-то
раз» И «выше абсолютного пола». Пол нужен на случай, когда парк тоже нездоров.

ЧЕГО ЭТО НЕ ЛЕЧИТ — и об этом честнее написать прямо здесь.

  * МИГРАЦИИ СХЕМЫ. База у канарейки и парка ОДНА. Новая версия, поднявшись,
    накатывает свои миграции на общую базу — и откат кода их не отменяет:
    обратной команды у миграций нет вовсе (см. RUNBOOK, сценарий 3). Поэтому
    preflight смотрит диф и отказывается выкатывать релиз со схемными
    изменениями без явного `--allow-schema`: релиз обязан быть совместим со
    старой версией в обе стороны (сначала добавили колонку, релизом позже
    начали её требовать).

  * ПЛАНИРОВЩИК. Канарейка — это только ROLE=api. Вторая копия планировщика
    означала бы второй ролловер сезона и второй бэкап; владение задачами
    разведено замком в Redis, но проверять это на релизе незачем.

  * МЕТРИКИ ПАРКА. Канарейка пишет счётчики в свою полку Redis
    (METRICS_NAMESPACE), иначе её /metrics отдал бы сумму по всем процессам,
    где её собственные пятисотки размыты девяткой здоровых.

Порядок операций жёсткий и одинаковый в обе стороны: трафик снимается ДО
остановки процесса и подаётся ПОСЛЕ готовности. Наоборот — это пачка
оборванных соединений у живых игроков.

Команды (каждая работает и отдельно — в три часа ночи нужен `rollback`, а не
весь цикл):

    python deploy/canary.py preflight --ref v1.4.0
    python deploy/canary.py release   --ref v1.4.0      # весь цикл целиком
    python deploy/canary.py deploy    --ref v1.4.0
    python deploy/canary.py weight 10
    python deploy/canary.py watch --window 300
    python deploy/canary.py promote --ref v1.4.0
    python deploy/canary.py rollback                    # снять канарейку
    python deploy/canary.py rollback --main             # вернуть парк на прошлый sha
    python deploy/canary.py status

Ключ `--dry-run` печатает команды вместо выполнения — на нём и проверяется
порядок действий, не дожидаясь плохого дня.
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

# ---------- где что лежит ----------
# Всё переопределяется окружением: этот файл лежит в репозитории, а пути на
# конкретной машине — свойство машины, а не кода.
MAIN_DIR = os.environ.get("CANARY_MAIN_DIR", "/opt/cookie")
CANARY_DIR = os.environ.get("CANARY_DIR", "/opt/cookie-canary")
MAIN_URL = os.environ.get("CANARY_MAIN_URL", "http://127.0.0.1:8000")
CANARY_URL = os.environ.get("CANARY_URL", "http://127.0.0.1:8001")
MAIN_UNITS = os.environ.get("CANARY_MAIN_UNITS", "cookie-api cookie-scheduler").split()
CANARY_UNIT = os.environ.get("CANARY_UNIT", "cookie-canary")
UPSTREAM_FILE = os.environ.get("CANARY_UPSTREAM_FILE",
                               "/etc/nginx/conf.d/cookie-upstream.conf")
STATE_FILE = os.environ.get("CANARY_STATE_FILE", "/opt/cookie/.canary-state.json")
# Токен /metrics: без него выгрузка отвечает 404, и наблюдать будет не за чем.
METRICS_TOKEN = os.environ.get("METRICS_TOKEN", "")

DRY_RUN = False


class Abort(Exception):
    """Причина, по которой выкладка останавливается. Печатается человеку."""


# ---------- оболочка вокруг системы ----------

def say(text: str):
    print(text, flush=True)


def run(cmd: list[str], check: bool = True, cwd: str | None = None) -> str:
    """Выполнить команду. В dry-run — напечатать и вернуть пустую строку."""
    shown = " ".join(cmd) + (f"   (в {cwd})" if cwd else "")
    if DRY_RUN:
        say(f"  [dry-run] {shown}")
        return ""
    say(f"  $ {shown}")
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and done.returncode != 0:
        raise Abort(f"команда упала ({done.returncode}): {shown}\n"
                    f"{done.stderr.strip()[:500]}")
    return done.stdout


def pip_install(folder: str) -> list[str]:
    """Команда установки зависимостей. Пути собираются через «/», а не
    os.path.join: скрипт исполняется на боевой машине под Linux, а запускают
    его иногда и с виндовой — обратный слеш в systemd-пути читается как
    опечатка и сбивает с толку ровно там, где сбиваться нельзя."""
    return [f"{folder}/venv/bin/pip", "install", "-q", "-r",
            f"{folder}/requirements.txt"]


def fetch(url: str, token: str = "", timeout: float = 5.0) -> tuple[int, str]:
    """(код, тело). Недоступность — это код 0, а не исключение: наблюдатель
    обязан пережить моргнувший процесс и посчитать это отказом, а не упасть
    сам, оставив канарейку под трафиком.

    Схема проверяется явно. Адреса приходят из переменных окружения, то есть
    из файла юнита, и опечатка вида file:///etc/shadow дала бы наблюдателю
    прочитать локальный файл и принять его за выгрузку метрик."""
    if not url.startswith(("http://", "https://")):
        return 0, f"недопустимая схема в адресе: {url[:40]}"
    request = urllib.request.Request(url)   # noqa: S310 — схема проверена выше
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:                              # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}"


# ---------- разбор выгрузки Prometheus ----------

Sample = tuple[str, tuple[tuple[str, str], ...]]


def parse_metrics(text: str) -> dict[Sample, float]:
    """Текст выгрузки -> {(имя, метки): значение}.

    Свой разбор, а не библиотека: формат тут ровно тот, который печатает наш же
    obs.render(), а тянуть зависимость в скрипт, который запускают на боевой
    машине в плохой день, — лишний способ не запуститься."""
    out: dict[Sample, float] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head, _, raw_value = line.rpartition(" ")
        if not head:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        name, labels = head, ()
        if "{" in head:
            name, _, rest = head.partition("{")
            rest = rest.rstrip("}")
            pairs = []
            for chunk in _split_labels(rest):
                key, _, val = chunk.partition("=")
                pairs.append((key.strip(), val.strip().strip('"')))
            labels = tuple(sorted(pairs))
        out[(name, labels)] = out.get((name, labels), 0.0) + value
    return out


def _split_labels(rest: str) -> list[str]:
    """Разбить `a="1",b="2,3"` по запятым ВНЕ кавычек.

    Значение метки — это в том числе маршрут и причина операции, и запятая
    внутри них встречается. Наивный split по запятой ломает такую строку и
    молча теряет метрику."""
    out, current, quoted = [], [], False
    for ch in rest:
        if ch == '"':
            quoted = not quoted
        if ch == "," and not quoted:
            out.append("".join(current))
            current = []
            continue
        current.append(ch)
    if current:
        out.append("".join(current))
    return [c for c in out if c]


def _matches(labels: tuple, want: dict) -> bool:
    have = dict(labels)
    return all(have.get(k) == v for k, v in want.items())


def total(samples: dict, name: str, **want) -> float:
    """Сумма счётчика по всем меткам, подходящим под фильтр."""
    return sum(v for (metric, labels), v in samples.items()
               if metric == name and _matches(labels, want))


def status_share(before: dict, after: dict, prefix: str,
                 skip: tuple[str, ...] = ()) -> tuple[float, float]:
    """(сколько ответов с таким классом кода, доля от всех) за окно.

    Именно приросты за окно, а не итоги: итог включает всё, что процесс успел
    до выкладки, и десять свежих пятисоток растворяются в миллионе старых
    двухсоток."""
    grew = 0.0
    for (metric, labels), value in after.items():
        if metric != "http_requests_total":
            continue
        code = dict(labels).get("status", "")
        if not code.startswith(prefix) or code in skip:
            continue
        grew += value - before.get((metric, labels), 0.0)
    whole = total(after, "http_requests_total") - total(before, "http_requests_total")
    return grew, (grew / whole if whole > 0 else 0.0)


def bucket_delta(before: dict, after: dict, name: str, **want) -> dict[float, float]:
    """Прирост корзин гистограммы за окно: {граница: сколько наблюдений}."""
    out: dict[float, float] = {}
    for (metric, labels), value in after.items():
        if metric != name + "_bucket" or not _matches(labels, want):
            continue
        raw = dict(labels).get("le", "")
        edge = math.inf if raw == "+Inf" else float(raw)
        out[edge] = out.get(edge, 0.0) + value - before.get((metric, labels), 0.0)
    return out


def quantile(buckets: dict[float, float], share: float) -> float:
    """Квантиль по накопительным корзинам, с интерполяцией внутри корзины.

    Точного значения гистограмма не хранит по построению — она хранит «сколько
    уложилось в 0.5 с» и «сколько в 1 с». Ответ поэтому приблизительный, и
    этого достаточно: решение принимается по «стало вдвое хуже», а не по
    третьему знаку. Попадание в +Inf возвращает бесконечность честно: значит
    хвост ушёл за последнюю границу, и любое конечное число здесь было бы
    выдумкой в пользу релиза."""
    if not buckets:
        return 0.0
    edges = sorted(buckets)
    whole = buckets[edges[-1]]          # +Inf накопил всё
    if whole <= 0:
        return 0.0
    target = share * whole
    previous_edge, previous_count = 0.0, 0.0
    for edge in edges:
        count = buckets[edge]
        if count >= target:
            if edge == math.inf:
                return math.inf
            span = count - previous_count
            part = (target - previous_count) / span if span > 0 else 0.0
            return previous_edge + (edge - previous_edge) * part
        previous_edge, previous_count = edge, count
    return math.inf


def ms(seconds: float) -> str:
    return "хвост за границей" if seconds == math.inf else f"{seconds * 1000:.0f} мс"


# ---------- решение ----------

class Limits:
    """Пороги. Отдельным объектом, чтобы их можно было проверить тестом, а не
    только глазами на боевой машине."""

    def __init__(self, error_share=0.01, error_ratio=2.0, client_share=0.05,
                 client_ratio=3.0, p99=2.0, p99_ratio=2.0, min_requests=200,
                 conflict_share=0.01):
        self.error_share = error_share
        self.error_ratio = error_ratio
        self.client_share = client_share
        self.client_ratio = client_ratio
        self.p99 = p99
        self.p99_ratio = p99_ratio
        self.min_requests = min_requests
        self.conflict_share = conflict_share


def verdict(canary: tuple[dict, dict], fleet: tuple[dict, dict],
            limits: Limits, unready: int) -> list[str]:
    """Список причин откатить. Пусто — релиз живой.

    Каждая проверка сравнивает канарейку и с полом, и с парком: одного пола
    мало (ночью и в час пик нормы разные), одного сравнения с парком мало
    (когда парку плохо, канарейке «не хуже» — не оправдание)."""
    before, after = canary
    fleet_before, fleet_after = fleet
    problems: list[str] = []

    served = total(after, "http_requests_total") - total(before,
                                                         "http_requests_total")
    if served < limits.min_requests:
        # Релиз, который никто не потрогал, — это не проверенный релиз. Пустое
        # окно обязано быть провалом, иначе выкладка ночью всегда «зелёная».
        return [f"канарейка получила {served:.0f} запросов из "
                f"{limits.min_requests} нужных — проверять нечего. Трафик до "
                f"неё не дошёл (вес в nginx, порт, юнит) или окно слишком "
                f"короткое"]

    if unready:
        problems.append(f"/readyz отвечал не 200 {unready} раз — процесс сам "
                        "сказал, что трафик ему давать нельзя")

    fatal, share = status_share(before, after, "5")
    _, fleet_share = status_share(fleet_before, fleet_after, "5")
    if fatal and share > max(limits.error_share, fleet_share * limits.error_ratio):
        problems.append(f"{fatal:.0f} ответов 5xx — {share:.1%} трафика "
                        f"канарейки против {fleet_share:.1%} у парка")

    # 429 не считаем: это работающий лимитер, а не поломка. Остальные 4xx —
    # это сломанный договор с фронтом, и снаружи он выглядит хуже пятисотки:
    # игрок видит не «ошибка сервера», а тихо не работающую кнопку.
    denied, denied_share = status_share(before, after, "4", skip=("429",))
    _, fleet_denied = status_share(fleet_before, fleet_after, "4", skip=("429",))
    if denied and denied_share > max(limits.client_share,
                                     fleet_denied * limits.client_ratio):
        problems.append(f"{denied:.0f} отказов 4xx (кроме 429) — "
                        f"{denied_share:.1%} против {fleet_denied:.1%} у парка")

    slow = quantile(bucket_delta(before, after, "http_request_duration_seconds"),
                    0.99)
    fleet_slow = quantile(
        bucket_delta(fleet_before, fleet_after, "http_request_duration_seconds"),
        0.99)
    if slow > max(limits.p99, fleet_slow * limits.p99_ratio):
        problems.append(f"p99 {ms(slow)} против {ms(fleet_slow)} у парка "
                        f"(пол {limits.p99 * 1000:.0f} мс)")

    dirty = (total(after, "db_dirty_connections_total")
             - total(before, "db_dirty_connections_total"))
    if dirty:
        # Такого не должно случаться вовсе: соединение с залипшей транзакцией
        # означает, что поток читает прошлое, а игрок получает «тебя нет».
        problems.append(f"{dirty:.0f} соединений выброшено с залипшей "
                        "транзакцией — это не бывает «немного»")

    conflicts = (total(after, "economy_ops_total", result="conflict")
                 - total(before, "economy_ops_total", result="conflict"))
    if conflicts and served and conflicts / served > limits.conflict_share:
        problems.append(f"{conflicts:.0f} конфликтов идемпотентности "
                        f"({conflicts / served:.1%}) — тот же токен операции "
                        "приходит с другим телом")

    return problems


# ---------- nginx ----------

def upstream_conf(main_url: str, canary_url: str, share: int) -> str:
    """Текст файла апстрима. Отдельной функцией — его проверяет тест.

    Вес 0 у nginx означает не «не давать трафик», а ошибку конфигурации,
    поэтому доля 0 выражается ОТСУТСТВИЕМ строки канарейки, а не весом 0.
    Ошибиться тут — значит откатить релиз командой, которая ничего не откатила.

    max_fails=1 плюс fail_timeout: если канарейка перестала отвечать, nginx
    уводит её из ротации сам, не дожидаясь наблюдателя. Наблюдатель считает
    цифры раз в несколько секунд, а игрок ждёт ответ сейчас."""
    lines = ["# Файл пишет deploy/canary.py. Править руками бессмысленно:",
             "# следующая выкладка перезапишет.",
             "upstream cookie_app {"]
    main = _host_port(main_url)
    lines.append(f"    server {main} weight={max(1, 100 - share)} "
                 "max_fails=3 fail_timeout=10s;")
    if share > 0:
        lines.append(f"    server {_host_port(canary_url)} weight={share} "
                     "max_fails=1 fail_timeout=30s;")
    lines.append("    keepalive 32;")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _host_port(url: str) -> str:
    return url.split("//", 1)[-1].rstrip("/")


def set_weight(share: int):
    """Подать канарейке долю трафика. Ноль — снять полностью."""
    text = upstream_conf(MAIN_URL, CANARY_URL, share)
    say(f"Доля трафика канарейки: {share}%")
    if DRY_RUN:
        say("  [dry-run] записал бы " + UPSTREAM_FILE + ":")
        say("".join("    " + line + "\n" for line in text.splitlines()))
    else:
        with open(UPSTREAM_FILE, "w", encoding="utf-8") as f:
            f.write(text)
    # nginx -t ДО reload: reload с битым конфигом оставляет старый рабочим, но
    # молча, и следующий рестарт машины поднимает nginx уже никак
    run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])


# ---------- состояние выкладки ----------

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(**fields):
    state = load_state()
    state.update(fields)
    if DRY_RUN:
        say(f"  [dry-run] записал бы {STATE_FILE}: {state}")
        return
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def current_sha(folder: str) -> str:
    return (run(["git", "-C", folder, "rev-parse", "HEAD"], check=False).strip()
            or "неизвестно")


# ---------- шаги выкладки ----------

SCHEMA_MARKERS = ("_migration(", "ALTER TABLE", "DROP COLUMN", "DROP TABLE",
                  "CREATE UNIQUE", "UNIQUES", "DEDUPE_MERGE", "SCHEMA")


def schema_risk(diff: str) -> list[str]:
    """Строки дифа, которые меняют схему общей базы.

    Проверка нарочно грубая и склонна к ложной тревоге: цена ошибки
    несимметрична. Лишний вопрос человеку стоит минуты, а миграция, уехавшая с
    канарейкой на общую базу, откатом кода не отменяется — её разгребают по
    сценарию 2 из RUNBOOK, то есть с остановкой сервиса и восстановлением на
    точку во времени."""
    risky = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        if any(marker in line for marker in SCHEMA_MARKERS):
            risky.append(line[1:].strip()[:120])
    return risky


def preflight(ref: str, allow_schema: bool):
    """Всё, что можно проверить ДО того, как хоть один игрок это увидит."""
    say(f"Preflight: {ref}")
    if not METRICS_TOKEN and not DRY_RUN:
        raise Abort("METRICS_TOKEN пуст — /metrics отвечает 404, и наблюдать "
                    "за канарейкой будет нечем. Выкладка вслепую хуже, чем "
                    "выкладка без канарейки: она создаёт уверенность")

    code, _ = fetch(MAIN_URL + "/readyz")
    if code != 200 and not DRY_RUN:
        raise Abort(f"парк сам не готов ({MAIN_URL}/readyz -> {code}). Сначала "
                    "инцидент, потом релиз: выкладка поверх аварии не даст "
                    "понять, что чинить")

    run(["git", "-C", CANARY_DIR, "fetch", "--tags", "--prune", "origin"])
    diff = run(["git", "-C", CANARY_DIR, "diff", "--unified=0",
                f"{current_sha(MAIN_DIR)}..{ref}", "--", "db.py"], check=False)
    risky = schema_risk(diff)
    if risky:
        say("  Релиз трогает схему общей базы:")
        for line in risky[:10]:
            say(f"    {line}")
        if not allow_schema:
            raise Abort(
                "выкладка остановлена. База у канарейки и парка одна, обратной "
                "команды у миграций нет: откат кода схему не вернёт. Релиз "
                "должен быть совместим со старой версией в обе стороны "
                "(колонку добавили сейчас, требовать начали следующим релизом). "
                "Если совместим — повторите с --allow-schema")
    say("  Preflight пройден")


def deploy(ref: str, skip_frontend: bool):
    """Поднять канарейку на новой версии. Трафика она пока не получает."""
    say(f"Выкладываю {ref} в канареечный каталог")
    run(["git", "-C", CANARY_DIR, "fetch", "--tags", "--prune", "origin"])
    run(["git", "-C", CANARY_DIR, "checkout", "--detach", ref])
    run(pip_install(CANARY_DIR))
    if not skip_frontend:
        # Фронт у канарейки свой: она раздаёт статику из своего webapp/dist, и
        # без сборки игрок получил бы старый бандл с новым API либо белый экран
        run(["npm", "ci"], cwd=CANARY_DIR + "/webapp")
        run(["npm", "run", "build"], cwd=CANARY_DIR + "/webapp")
    run(["systemctl", "restart", CANARY_UNIT])
    wait_ready(CANARY_URL, timeout=90)


def wait_ready(url: str, timeout: float = 90.0):
    """Ждать 200 на /readyz. Трафик подаётся только после этого."""
    say(f"Жду готовности {url}/readyz")
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        code, body = fetch(url + "/readyz")
        if code == 200:
            say("  готов")
            return
        last = f"{code} {body[:120]}"
        if DRY_RUN:
            say("  [dry-run] считаю, что поднялся")
            return
        time.sleep(2)
    raise Abort(f"{url}/readyz не отвечает 200 за {timeout:.0f} с: {last}")


def snapshot(url: str) -> dict:
    code, body = fetch(url + "/metrics", METRICS_TOKEN, timeout=10)
    if code != 200:
        raise Abort(f"{url}/metrics -> {code}: {body[:200]}")
    return parse_metrics(body)


def watch(window: float, poll: float, limits: Limits) -> list[str]:
    """Смотреть окно и вернуть причины откатить.

    Ошибка наблюдения — тоже причина откатить. Наблюдатель, который не смог
    прочитать метрики и на этом основании сказал «всё хорошо», вреднее
    отсутствующего: он даёт уверенность, ничем не подкреплённую."""
    if DRY_RUN:
        say(f"  [dry-run] смотрел бы {window:.0f} с, опрос раз в {poll:.0f} с")
        return []
    say(f"Наблюдаю {window:.0f} с (опрос раз в {poll:.0f} с)")
    try:
        canary_before, fleet_before = snapshot(CANARY_URL), snapshot(MAIN_URL)
    except Abort as e:
        return [f"не удалось снять метрики до окна: {e}"]

    unready = 0
    deadline = time.time() + window
    while time.time() < deadline:
        time.sleep(min(poll, max(0.0, deadline - time.time())))
        code, _ = fetch(CANARY_URL + "/readyz")
        if code != 200:
            unready += 1
            say(f"  /readyz -> {code}")
        # Ранний выход: если канарейка уже сыплет, досиживать окно незачем —
        # каждая лишняя минута это лишние игроки, которым сегодня не повезло
        try:
            early = verdict((canary_before, snapshot(CANARY_URL)),
                            (fleet_before, snapshot(MAIN_URL)), limits, unready)
        except Abort as e:
            return [f"метрики стали недоступны посреди окна: {e}"]
        fatal = [p for p in early if "получила" not in p]
        if fatal:
            say("  ранний отказ, окно досиживать незачем")
            return fatal

    try:
        return verdict((canary_before, snapshot(CANARY_URL)),
                       (fleet_before, snapshot(MAIN_URL)), limits, unready)
    except Abort as e:
        return [f"не удалось снять метрики после окна: {e}"]


def rollback(main: bool = False):
    """Снять канарейку. Порядок обратный подаче трафика и важен именно им."""
    say("Откат")
    set_weight(0)                      # сначала увести трафик…
    run(["systemctl", "stop", CANARY_UNIT], check=False)   # …и только потом гасить
    if not main:
        return
    previous = load_state().get("previous")
    if not previous:
        raise Abort(f"в {STATE_FILE} нет прошлого sha — откатывать парк не на "
                    f"что. Ручной путь: git -C {MAIN_DIR} checkout <sha>, "
                    f"затем systemctl restart {' '.join(MAIN_UNITS)}")
    say(f"Возвращаю парк на {previous}")
    run(["git", "-C", MAIN_DIR, "checkout", "--detach", previous])
    run(pip_install(MAIN_DIR))
    for unit in MAIN_UNITS:
        run(["systemctl", "restart", unit])
    wait_ready(MAIN_URL)


def promote(ref: str, skip_frontend: bool):
    """Перевести на новую версию весь парк.

    Прошлый sha записывается ДО переключения: после него узнать, откуда мы
    пришли, будет уже неоткуда, а именно этот вопрос задают первым."""
    previous = current_sha(MAIN_DIR)
    save_state(previous=previous, released=ref, at=time.time())
    say(f"Перевожу парк на {ref} (прошлый {previous[:12]})")
    run(["git", "-C", MAIN_DIR, "fetch", "--tags", "--prune", "origin"])
    run(["git", "-C", MAIN_DIR, "checkout", "--detach", ref])
    run(pip_install(MAIN_DIR))
    if not skip_frontend:
        run(["npm", "ci"], cwd=MAIN_DIR + "/webapp")
        run(["npm", "run", "build"], cwd=MAIN_DIR + "/webapp")
    for unit in MAIN_UNITS:
        run(["systemctl", "restart", unit])
    try:
        wait_ready(MAIN_URL)
    except Abort:
        # Канарейка была жива, а парк не поднялся — такое бывает: у парка есть
        # планировщик, которого у канарейки нет вовсе. Возвращаем как было и
        # оставляем канарейку под трафиком: она уже доказала, что работает.
        say("  парк не поднялся — возвращаю прошлую версию")
        rollback(main=True)
        raise
    set_weight(0)                      # канарейка больше не нужна: код тот же
    run(["systemctl", "stop", CANARY_UNIT], check=False)
    say(f"Готово: парк на {ref}, канарейка снята")


def status():
    say(f"Парк     {MAIN_DIR} {current_sha(MAIN_DIR)[:12]} {MAIN_URL}")
    say(f"Канарейка {CANARY_DIR} {current_sha(CANARY_DIR)[:12]} {CANARY_URL}")
    for url in (MAIN_URL, CANARY_URL):
        code, _ = fetch(url + "/readyz")
        say(f"  {url}/readyz -> {code or 'не отвечает'}")
    state = load_state()
    if state:
        when = time.strftime("%Y-%m-%d %H:%M",
                             time.localtime(state.get("at", 0)))
        say(f"Последняя выкладка: {state.get('released')} в {when}, "
            f"откат на {str(state.get('previous'))[:12]}")


# ---------- разбор аргументов ----------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("preflight", "deploy", "weight",
                                            "watch", "promote", "rollback",
                                            "release", "status"))
    parser.add_argument("value", nargs="?", help="доля трафика для weight")
    parser.add_argument("--ref", default="", help="тег или sha новой версии")
    parser.add_argument("--share", type=int, default=10,
                        help="доля трафика канарейки в процентах")
    parser.add_argument("--window", type=float, default=300,
                        help="сколько секунд наблюдать")
    parser.add_argument("--poll", type=float, default=15,
                        help="раз во сколько секунд опрашивать")
    parser.add_argument("--min-requests", type=int, default=200,
                        help="меньше этого числа запросов — окно не засчитано")
    parser.add_argument("--error-share", type=float, default=0.01,
                        help="доля 5xx, выше которой откат")
    parser.add_argument("--p99", type=float, default=2.0,
                        help="пол по p99 в секундах")
    parser.add_argument("--allow-schema", action="store_true",
                        help="релиз меняет схему общей базы — я понимаю риск")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="не пересобирать webapp")
    parser.add_argument("--main", action="store_true",
                        help="для rollback: вернуть и парк, а не только канарейку")
    parser.add_argument("--dry-run", action="store_true",
                        help="печатать команды вместо выполнения")
    return parser


def main(argv: list[str] | None = None) -> int:
    global DRY_RUN
    args = build_parser().parse_args(argv)
    DRY_RUN = args.dry_run
    limits = Limits(error_share=args.error_share, p99=args.p99,
                    min_requests=args.min_requests)

    needs_ref = ("preflight", "deploy", "promote", "release")
    if args.command in needs_ref and not args.ref:
        say(f"команде {args.command} нужен --ref")
        return 2

    try:
        if args.command == "status":
            status()
        elif args.command == "preflight":
            preflight(args.ref, args.allow_schema)
        elif args.command == "deploy":
            deploy(args.ref, args.skip_frontend)
        elif args.command == "weight":
            set_weight(int(args.value or args.share))
        elif args.command == "watch":
            problems = watch(args.window, args.poll, limits)
            for line in problems:
                say(f"  ПРОВАЛ: {line}")
            return 1 if problems else 0
        elif args.command == "promote":
            promote(args.ref, args.skip_frontend)
        elif args.command == "rollback":
            rollback(main=args.main)
        elif args.command == "release":
            preflight(args.ref, args.allow_schema)
            deploy(args.ref, args.skip_frontend)
            set_weight(args.share)
            problems = watch(args.window, args.poll, limits)
            if problems:
                for line in problems:
                    say(f"  ПРОВАЛ: {line}")
                rollback()
                say("Релиз откачен, парк остался на прежней версии")
                return 1
            promote(args.ref, args.skip_frontend)
    except Abort as e:
        say(f"ОСТАНОВЛЕНО: {e}")
        # Канарейка под трафиком и с неизвестным состоянием — худший исход:
        # игроки на ней есть, а следит за ней уже никто
        if args.command == "release":
            try:
                rollback()
            except Abort as second:
                say(f"откат тоже не прошёл: {second}\n"
                    f"РУКАМИ: снять вес канарейки в {UPSTREAM_FILE}, "
                    f"nginx -t && systemctl reload nginx, "
                    f"systemctl stop {CANARY_UNIT}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
