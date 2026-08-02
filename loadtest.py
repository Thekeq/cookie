"""Нагрузка: сколько игроков выдерживает процесс и что ломается первым.

Вопрос не «работает ли ручка», а «работает ли она, когда в базе сто тысяч
строк и в неё одновременно ломятся сотни игроков». Эти два числа расходятся
сильнее всего именно там, где их не ждут: запрос, который на пустой базе идёт
2 мс, при 100 000 строк идёт 200 мс, если по колонке нет индекса. На проде это
выясняется в момент, когда закупка трафика уже оплачена.

Что здесь меряется:

  1. ЛАТЕНТНОСТЬ ПО РУЧКАМ — p50/p95/p99. Среднее прячет ровно то, из-за чего
     игрок уходит: приличное среднее при p99 в три секунды означает, что
     каждый сотый тап висит, а тапов за сессию сотни.

  2. РАЗМЕР БАЗЫ КАК ПАРАМЕТР — `--seed N` заранее насыпает N строк игроков.
     Прогон на пустой базе не отвечает ни на один интересный вопрос.

  3. ЦЕЛОСТНОСТЬ ПОСЛЕ НАГРУЗКИ — книга операций против колонок. Гонка,
     которая теряет начисление, проявляется только под конкуренцией и только
     в цифрах, а не в кодах ответов.

Чего здесь НЕТ: сети. Запросы идут через TestClient, то есть меряется процесс,
а не канал до него — задержку сети и TLS добавит любой внешний инструмент, а
вот что происходит ВНУТРИ под конкуренцией, показывает только это.

ПРО ДВИЖОК. По умолчанию берётся SQLite во временном файле — это удобно и
ничего не требует, но у него ОДИН писатель на файл, и под десятком потоков
хвост задержек упирается в очередь за блокировкой, а не в код. Цифры, по
которым принимают решения, снимаются на боевом движке: `--url` гонит тот же
профиль по PostgreSQL. Имя базы обязано содержать «test» — профиль пишет в неё
сотни тысяч строк, и одна опечатка в строке подключения не должна стоить
боевых данных.

БОЕВАЯ БАЗА НЕ УЧАСТВУЕТ. Файл создаётся во временном каталоге и удаляется в
конце; `DATABASE_URL` из окружения вычищается до импорта.

Запуск:
    python loadtest.py                       # 200 игроков, 3 сессии, 16 потоков
    python loadtest.py --seed 100000         # то же, но база «после раскрутки»
    python loadtest.py --users 500 --workers 32 --seed 50000
    python loadtest.py --url postgresql://u:p@localhost/cookie_test --seed 100000
"""
import argparse
import os
import sys
import tempfile
import threading
import time

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--users", type=int, default=200,
                    help="сколько РАЗНЫХ игроков ходит (лимитер считает по игроку)")
parser.add_argument("--sessions", type=int, default=3,
                    help="сколько игровых сессий проживает каждый игрок")
parser.add_argument("--workers", type=int, default=16,
                    help="сколько запросов идёт одновременно")
parser.add_argument("--seed", type=int, default=0,
                    help="насыпать столько посторонних игроков ДО прогона")
parser.add_argument("--p99", type=float, default=None,
                    help="порог p99 в мс (по умолчанию 1000 на PostgreSQL; "
                         "на SQLite не проверяется — задайте явно, если нужно)")
parser.add_argument("--url", default="",
                    help="гнать профиль по PostgreSQL (имя базы должно содержать test)")
parser.add_argument("--keep-db", action="store_true",
                    help="не удалять временную базу (для разбора запросов)")
args = parser.parse_args()

os.environ.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")
os.environ.pop("DATABASE_URL", None)          # боевой сервер не трогаем никогда
DB_PATH = os.path.join(tempfile.gettempdir(), f"cookie_load_{os.getpid()}.db")
if args.url:
    # Профиль насыпает сотни тысяч строк и пишет в них как хочет. Одна опечатка
    # в строке подключения — и он сделает это с боевой базой, поэтому имя
    # проверяется до импорта, а не «мы же аккуратно»
    name = args.url.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name.lower():
        raise SystemExit(f"отказ: база '{name}' не похожа на тестовую. Профиль "
                         "насыпает в неё сотни тысяч строк — имя обязано "
                         "содержать 'test'")
    os.environ["DATABASE_URL"] = args.url
os.environ["DATABASE_PATH"] = DB_PATH
os.environ.setdefault("LOG_FILE", os.path.join(tempfile.gettempdir(),
                                               f"cookie_load_{os.getpid()}.log"))

import hashlib                                  # noqa: E402
import hmac                                     # noqa: E402
import json                                     # noqa: E402
import logging                                  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402
from urllib.parse import urlencode              # noqa: E402

from fastapi.testclient import TestClient       # noqa: E402

from main import app                            # noqa: E402
from server import economy                      # noqa: E402
from server.game_logic import db                # noqa: E402

# логи процесса под нагрузкой — это ещё сотня тысяч строк на диск, и меряли бы
# мы уже их. Ошибки оставляем: 500 обязана быть видна
logging.getLogger().setLevel(logging.ERROR)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_UID = 700_000_000


def headers(uid: int) -> dict:
    data = {"user": json.dumps({"id": uid, "username": f"load{uid}",
                                "first_name": "Load"}),
            "auth_date": str(int(time.time()))}
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    return {"Authorization": "tma " + urlencode(data)}


# ---------- фон: база «как после раскрутки» ----------

def seed_users(count: int):
    """Насыпать посторонних игроков, чтобы запросы шли по большой таблице.

    Балансы остаются нулевыми намеренно: книга операций у этих строк пустая, и
    сверка сходимости в конце не должна ловить чужой шум. Задача этих строк —
    один только объём: чтобы индекс работал по-настоящему, а не по трём
    записям, которые целиком лежат в одной странице."""
    now = time.time()
    step = 5000
    done = 0
    while done < count:
        batch = [(BASE_UID + 1_000_000 + i, f"seed{i}", "Seed", now, now, now)
                 for i in range(done, min(done + step, count))]
        with db.tx():
            # ON CONFLICT, а не INSERT OR IGNORE: второй понимает только SQLite,
            # а профиль обязан гоняться на том движке, который стоит на проде
            db.cursor.executemany(db._sql(
                "INSERT INTO users (user_id, username, first_name, "
                "energy_updated_at, passive_collected_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING"), batch)
        done += len(batch)
        print(f"\r  насыпано {done}/{count}", end="", flush=True)
    print()


def wipe_previous():
    """Ничего не удаляем — объясняем, что прогон дописывает.

    Книга операций append-only на уровне триггеров, и это правильно: харнесс
    не тот повод, ради которого стоит пробивать инвариант экономики. Значит
    повторный прогон по той же базе просто добавляет к ней ещё один слой:
    сверка сойдётся, но «сколько строк было» будет расти. Нужен чистый профиль
    — пересоздайте тестовую базу, это одна команда."""
    rows = db.q1("SELECT COUNT(*) c FROM users WHERE user_id >= ?",
                 (BASE_UID,))["c"]
    if rows:
        print(f"  в базе уже {rows} строк от прошлых прогонов — прогон "
              "допишет к ним. Чистый профиль = пересозданная база")


# ---------- сбор времён ----------

_lock = threading.Lock()
_times: dict[str, list[float]] = {}
_codes: dict[str, dict[int, int]] = {}


def timed(client, method: str, path: str, label: str, uid: int = 0, **kw):
    started = time.perf_counter()
    try:
        response = getattr(client, method)(path, **kw)
        code = response.status_code
        # 429 — не поломка, а работающий лимитер; остальное надо видеть с телом,
        # иначе «6 четырёхсоток из 4600» останутся строкой в таблице
        if code >= 400 and code != 429:
            with _lock:
                _errors.append(f"{label} uid={uid} -> {code} {response.text[:120]}")
    except Exception as e:                        # noqa: BLE001
        code = -1
        response = None
        with _lock:
            _errors.append(f"{label} uid={uid}: {type(e).__name__}: {e}")
    elapsed = (time.perf_counter() - started) * 1000
    with _lock:
        _times.setdefault(label, []).append(elapsed)
        bucket = _codes.setdefault(label, {})
        bucket[code] = bucket.get(code, 0) + 1
    return response


_errors: list[str] = []
_registered: set[int] = set()      # кому /api/auth ответил 200


# ---------- профиль сессии ----------

def session(client, uid: int, run: int):
    """Один заход игрока в приложение.

    Порядок и пропорции взяты из настоящей сессии: открыл, посмотрел
    состояние, потапал несколько батчей, поставил предмет на доску, забрал
    дейлик, ушёл. Смысл именно в пропорции: если гонять одну ручку, узкое
    место находится не то, которое найдут игроки."""
    head = headers(uid)
    if run == 0:
        r = timed(client, "post", "/api/auth", "POST /api/auth", uid, headers=head)
        if r is not None and r.status_code == 200:
            with _lock:
                _registered.add(uid)
    timed(client, "get", "/api/state", "GET /api/state", uid, headers=head)
    for batch in range(4):
        timed(client, "post", "/api/click", "POST /api/click", uid, headers=head,
              json={"clicks": 20, "batch_id": f"load-{uid}-{run}-{batch}"})
    timed(client, "post", "/api/merge/spawn", "POST /api/merge/spawn", uid,
          headers=head, json={"level": 1})
    if run == 0:
        timed(client, "post", "/api/daily/claim", "POST /api/daily/claim", uid,
              headers={**head, "X-Op-Id": f"load-daily-{uid}"})
    timed(client, "get", "/api/leaderboard", "GET /api/leaderboard", uid,
          headers=head)


# ---------- отчёт ----------

def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def report(seconds: float, total: int):
    print(f"\n{'ручка':<26} {'n':>6} {'p50,мс':>9} {'p95,мс':>9} "
          f"{'p99,мс':>9} {'max,мс':>9}  коды")
    print("-" * 88)
    for label in sorted(_times, key=lambda k: -percentile(_times[k], 0.95)):
        values = _times[label]
        codes = ", ".join(f"{code}:{n}" for code, n in sorted(_codes[label].items()))
        print(f"{label:<26} {len(values):>6} "
              f"{percentile(values, 0.50):>9.1f} {percentile(values, 0.95):>9.1f} "
              f"{percentile(values, 0.99):>9.1f} {max(values):>9.1f}  {codes}")
    print("-" * 88)
    print(f"{total} запросов за {seconds:.1f} с — {total / seconds:.0f} rps "
          f"на {args.workers} одновременных")


def verdict(seconds: float, total: int) -> int:
    """Что считать провалом. Пороги — не украшение: без них прогон становится
    «посмотрели на цифры и разошлись», а деградация замечается на релизе."""
    problems = []
    fives = sum(n for label in _codes for code, n in _codes[label].items()
                if code >= 500 or code == -1)
    if fives:
        problems.append(f"{fives} ответов 5xx/исключений")
    # 4xx кроме 429 — это игрок, которому ручка сказала «нельзя». Под нагрузкой
    # такого быть не должно: тот же сценарий в одиночку проходит целиком
    fours = sum(n for label in _codes for code, n in _codes[label].items()
                if 400 <= code < 500 and code != 429)
    if fours:
        problems.append(f"{fours} отказов 4xx на сценарии, который в одиночку "
                        "проходит целиком")
    if _errors:
        for line in _errors[:5]:
            print(f"  пример отказа: {line}")

    # Порог задержек имеет смысл только на боевом движке. У SQLite ОДИН писатель
    # на файл, и под десятком потоков хвост меряет очередь за блокировкой, а не
    # код: любое число тут будет либо вечно красным, либо ничего не значащим, а
    # вечно красная проверка ровно так же бесполезна, как отсутствующая.
    p99_limit = args.p99
    if p99_limit is None:
        p99_limit = 1000.0 if db.DIALECT == "postgres" else 0.0
    if p99_limit:
        slow = {label: percentile(values, 0.99) for label, values in _times.items()
                if percentile(values, 0.99) > p99_limit}
        if slow:
            problems.append(f"p99 выше {p99_limit:.0f} мс: "
                            + ", ".join(f"{k} {v:.0f}мс" for k, v in slow.items()))
    else:
        print("  задержки не проверяются: SQLite держит одного писателя на файл, "
              "и хвост тут — очередь за блокировкой, а не код. Цифры по проду: "
              "python loadtest.py --url postgresql://…/cookie_test")

    # регистрация, которая ответила 200 и не оставила строки, — худший из
    # возможных исходов: игрок увидит «начни сначала» на втором экране
    missing = [uid for uid in sorted(_registered) if db.get_user(uid) is None]
    if missing:
        problems.append(f"{len(missing)} игроков зарегистрировались успешно, но "
                        f"строки в базе нет: {missing[:3]}")

    drift = economy.drift_report(limit=5)
    if drift:
        problems.append(f"книга не сошлась у {len(drift)} игроков: {drift[:2]}")

    negative = db.q1("SELECT COUNT(*) c FROM users WHERE cookies < 0")["c"]
    if negative:
        problems.append(f"{negative} игроков с отрицательным балансом")

    print()
    if problems:
        for p in problems:
            print(f"  ПРОВАЛ: {p}")
        return 1
    print(f"  OK: {total} запросов, 5xx нет, книга сходится"
          + (f", p99 в пределах {p99_limit:.0f} мс" if p99_limit else ""))
    return 0


# ---------- прогон ----------

def main() -> int:
    where = args.url.rsplit("@", 1)[-1] if args.url else DB_PATH
    if args.url:
        print(f"Движок: {db.DIALECT}, {where}")
        wipe_previous()

    if args.seed:
        print(f"Насыпаю {args.seed} посторонних игроков…")
        started = time.perf_counter()
        seed_users(args.seed)
        print(f"  готово за {time.perf_counter() - started:.1f} с")

    rows = db.q1("SELECT COUNT(*) c FROM users")["c"]
    print(f"База: {rows} игроков, {db.DIALECT} {where}")
    print(f"Нагрузка: {args.users} игроков x {args.sessions} сессий, "
          f"{args.workers} одновременных запросов")

    client = TestClient(app)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # Сессии идут волнами, с барьером между ними. Без барьера пул успевает
        # взять в работу второй заход игрока, пока первый ещё висит в
        # /api/auth: тогда /api/state честно отвечает err_no_user, и профиль
        # рапортует «404 под нагрузкой» про поломку, которой нет — у живого
        # игрока второй заход не может обогнать первый. Один такой ложный
        # отказ стоит дня разбирательств, поэтому порядок тут не украшение.
        for run in range(args.sessions):
            uids = [BASE_UID + user for user in range(args.users)]
            list(pool.map(lambda uid, r=run: session(client, uid, r), uids))
    seconds = time.perf_counter() - started

    total = sum(len(v) for v in _times.values())
    report(seconds, total)
    return verdict(seconds, total)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        if not args.keep_db and not args.url:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.remove(DB_PATH + suffix)
                except OSError:
                    pass
    raise SystemExit(code)
