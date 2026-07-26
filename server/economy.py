"""Книга операций: единственное место, где фиксируется движение валют.

Зачем она. Баланс в `users` — это ИТОГ, и по нему нельзя сказать, чем он стал
таким. Пока игра живёт одним процессом, потерянных апдейтов нет: читаем и пишем
без await между ними. Но как только процессов станет больше одного (а к сотне
тысяч онлайна это неизбежно), read-modify-write начнёт терять записи молча —
цифра просто окажется другой, и заметить это будет нечем. Книга делает баланс
проверяемым: сумма движений обязана сходиться с колонкой, расхождение видно
сразу, а не через месяц по жалобам.

Вторая половина модуля — идемпотентность. Клиент ретраит: потерялся ответ,
вылетел Telegram, дважды нажали. Без токена операции ретрай выдаёт награду
ещё раз. `economy_ops` хранит токен и ответ первого запроса: повтор получает
тот же ответ, ничего не начисляя. В отличие от `click_batches`, который чистят
через час, токены не протухают — иначе поздний ретрай сработал бы снова.

Пишут сюда только денежные примитивы game_logic. Прямой UPDATE баланса в обход
книги — баг, его найдёт `reconcile`.
"""
import json
import math
import time
import uuid

from db import shared

db = shared()

# Валюта -> колонка в users. Сверка ходит ровно по этому словарю.
CURRENCY_COLUMN = {
    "cookies": "cookies",
    "xp": "xp",
    "bp_xp": "bp_xp",
    "energy": "energy",
    "prestige_points": "prestige_points",
    "offline_hours": "offline_bonus_hours",
}

# Валюты вне users: колонки нет, в сверку не входят, но в книге нужны —
# по ним считается выручка и разбираются возвраты
EXTERNAL = ("stars",)

# ИСКЛЮЧЕНИЕ: реген энергии в книгу не пишется. Он не минт, а производная от
# времени — энергия восстанавливается сама и пересчитывается при каждом чтении.
# Записывать её по батчу кликов значило бы утопить книгу в строках, из которых
# ничего не следует. Ledger'ятся только выдачи: награда уровня, покупка за
# Stars, промокод, награда батл-пасса. Поэтому сверка энергии — не «сумма
# равна балансу», а «сумма выдач не больше баланса плюс потраченное».
LEDGERED_PARTIAL = ("energy",)

MAX_ABS = 1e15


class ConflictError(Exception):
    """Операция с этим токеном уже выполняется. Клиенту — 409, пусть перечитает
    состояние: параллельный запрос ещё не закоммитился, и его ответа нет."""

    def __init__(self, user_id: int, operation_id: str = ""):
        super().__init__("err_state_conflict")
        self.user_id = user_id
        self.operation_id = operation_id


def _sane(x, what: str) -> float:
    """Пропускает только конечное число в разумных пределах.

    Стоит перед SQL, а не после: NaN, приехавший из деления на ноль в расчёте
    дохода, в SQLite ложится в колонку как NULL и отравляет баланс навсегда —
    все последующие арифметические операции с ним дают NULL."""
    if x is None:
        raise ValueError(f"err_bad_amount|{what}")
    x = float(x)
    if not math.isfinite(x) or abs(x) > MAX_ABS:
        raise ValueError(f"err_bad_amount|{what}")
    return x


def auto_op(user_id: int, reason: str) -> str:
    """Токен для движения, которое НЕ защищено от повтора.

    Уникальный, а не выведенный из данных, и это принципиально: `record` глушит
    конфликт по (operation_id, currency, seq), так что детерминированный токен
    молча съел бы второе законное начисление с той же причиной. Защита от
    ретрая — дело вызывающего: он передаёт свой operation_id там, где повтор
    действительно означает одну и ту же награду."""
    return f"auto:{reason}:{user_id}:{uuid.uuid4().hex}"


def already_recorded(operation_id: str, currency: str, seq: int = 0) -> bool:
    """Это движение уже записано?

    Нужно ПЕРЕД тем, как двигать баланс. Одной уникальности в книге мало:
    она погасила бы только вторую строку, а колонка уехала бы второй раз — и
    получилось бы расхождение, которого сверка уже не объяснит. Смотреть до
    записи безопасно: BEGIN IMMEDIATE выстраивает писателей в очередь, а если
    двое всё же разойдутся (PostgreSQL), второго снимет уникальный индекс —
    вместе со всей его транзакцией."""
    return db.q1("SELECT 1 AS x FROM economy_ledger WHERE operation_id = ? "
                 "AND currency = ? AND seq = ?",
                 (operation_id, currency, seq)) is not None


def record(user_id: int, currency: str, amount: float, reason: str,
           balance_after: float, operation_id: str, *, seq: int = 0,
           ref_type: str | None = None, ref_id: str | None = None,
           counts_earned: int = 0, season_id: int | None = None,
           idempotent: bool = False) -> None:
    """Одна неизменяемая строка книги.

    Зовётся из денежных примитивов и всегда в одной транзакции с самим
    движением: строка книги без движения (и наоборот) — это и есть расхождение,
    которое книга должна ловить.

    По умолчанию дубль токена — это ОШИБКА и он рвёт транзакцию: раз баланс уже
    сдвинут, тихо проглотить строку значит развести книгу с колонкой. Гасить
    конфликт (`idempotent=True`) можно там, где повтор не сопровождается
    движением, — например при повторном прогоне миграции входящих остатков."""
    amount = _sane(amount, f"{reason}.amount")
    balance_after = _sane(balance_after, f"{reason}.balance_after")
    tail = " ON CONFLICT (operation_id, currency, seq) DO NOTHING" if idempotent else ""
    with db.tx():
        db.exec(
            "INSERT INTO economy_ledger (user_id, operation_id, seq, currency, "
            "amount, reason, ref_type, ref_id, balance_after, counts_earned, "
            "season_id, external, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)" + tail,
            (user_id, operation_id, seq, currency, amount, reason, ref_type,
             ref_id, balance_after, counts_earned, season_id,
             1 if currency in EXTERNAL else 0, time.time()))


def begin_op(operation_id: str, user_id: int, kind: str) -> dict | None:
    """None — операция наша, выполняем. dict — это РЕПЛЕЙ, вернуть как есть.

    Обязана вызываться ВНУТРИ db.tx(): токен должен откатиться вместе с
    эффектом. Иначе упавший на середине запрос оставит после себя вечный
    'open', и игрок больше никогда не получит эту награду."""
    if not db._tx_depth:
        raise RuntimeError("begin_op вне транзакции: токен не откатится с эффектом")
    if db.exec("INSERT INTO economy_ops (operation_id, user_id, kind, status, "
               "created_at) VALUES (?, ?, ?, 'open', ?) "
               "ON CONFLICT (operation_id) DO NOTHING",
               (operation_id, user_id, kind, time.time())) == 1:
        return None
    row = db.q1("SELECT status, response FROM economy_ops WHERE operation_id = ?",
                (operation_id,))
    if row and row["status"] == "done" and row["response"]:
        return json.loads(row["response"])
    # 'open' виден только на PostgreSQL и только если первый запрос ещё не
    # закоммитился: на SQLite BEGIN IMMEDIATE сериализует писателей
    raise ConflictError(user_id, operation_id)


def finish_op(operation_id: str, response: dict) -> dict:
    """Запоминает ответ, чтобы ретрай получил ровно его."""
    db.exec("UPDATE economy_ops SET status = 'done', response = ? "
            "WHERE operation_id = ? AND status = 'open'",
            (json.dumps(response, ensure_ascii=False), operation_id))
    return response


# ---------- входящие остатки ----------

def backfill_opening():
    """Заводит в книгу тех, кто пришёл в игру до неё.

    Без этого сверка провалится у КАЖДОГО существующего игрока в первый же
    день: сумма движений будет нулём против непустого баланса. Одна строка на
    валюту с ненулевым остатком плюс снимок total_earned/season_earned, от
    которого дальше считается прирост."""
    if not db._migration("backfill:ledger_opening"):
        return
    now = time.time()
    cols = ", ".join(sorted(set(CURRENCY_COLUMN.values())))
    users = db.q(f"SELECT user_id, season_id, total_earned, season_earned, {cols} "
                 f"FROM users")
    for u in users:
        with db.tx():
            for currency, column in CURRENCY_COLUMN.items():
                value = u[column] or 0
                if not value:
                    continue
                record(u["user_id"], currency, value, "opening_balance", value,
                       f"opening:{u['user_id']}:{currency}",
                       season_id=u["season_id"], idempotent=True)
            db.exec("INSERT INTO economy_opening (user_id, total_earned, "
                    "season_earned, captured_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT (user_id) DO NOTHING",
                    (u["user_id"], u["total_earned"] or 0, u["season_earned"] or 0, now))
    db._mark("backfill:ledger_opening")
    if users:
        print(f"[*] Миграция: входящие остатки записаны в книгу, {len(users)} игроков")


# ---------- сверка ----------

def reconcile(user_id: int) -> dict:
    """Сходится ли колонка с книгой. drift != 0 — потерянный апдейт.

    Энергия проверяется односторонне (см. LEDGERED_PARTIAL): её реген в книгу
    не пишется, поэтому сравнивать сумму с балансом бессмысленно.

    Аккаунт, заведённый уже после миграции, снимка в economy_opening не имеет —
    и не должен: он пришёл с нулями, и нули тут же и получаются."""
    user = db.get_user(user_id)
    if not user:
        return {}
    sums = {r["currency"]: r["s"] for r in db.q(
        "SELECT currency, SUM(amount) s FROM economy_ledger "
        "WHERE user_id = ? AND external = 0 GROUP BY currency", (user_id,))}
    out = {}
    for currency, column in CURRENCY_COLUMN.items():
        if currency in LEDGERED_PARTIAL:
            continue
        balance = float(user[column] or 0)
        ledger = float(sums.get(currency) or 0)
        out[currency] = {"balance": balance, "ledger": ledger,
                         "drift": round(balance - ledger, 6)}
    opening = db.q1("SELECT total_earned, season_earned FROM economy_opening "
                    "WHERE user_id = ?", (user_id,)) or {"total_earned": 0.0,
                                                         "season_earned": 0.0}
    earned = db.q1("SELECT COALESCE(SUM(amount), 0) s FROM economy_ledger "
                   "WHERE user_id = ? AND currency = 'cookies' AND counts_earned = 1",
                   (user_id,))["s"]
    out["total_earned"] = {
        "balance": float(user["total_earned"] or 0),
        "ledger": float(opening["total_earned"]) + float(earned),
        "drift": round(float(user["total_earned"] or 0)
                       - float(opening["total_earned"]) - float(earned), 6),
    }
    return out


def drift_report(limit: int = 50) -> list[dict]:
    """Игроки, у которых книга разошлась с балансом. Для админки и тестов."""
    bad = []
    for row in db.q("SELECT user_id FROM users ORDER BY user_id"):
        state = reconcile(row["user_id"])
        drift = {k: v["drift"] for k, v in state.items() if abs(v["drift"]) > 1e-6}
        if drift:
            bad.append({"user_id": row["user_id"], "drift": drift})
        if len(bad) >= limit:
            break
    return bad


backfill_opening()
