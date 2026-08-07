"""Инструмент поддержки и anti-fraud поверх книги операций (пункт 31 плана).

Книга существует давно, инструмента над ней не было. Разбор жалобы выглядел
так: `grep` по логу, `SELECT` руками в psql, компенсация прямым `UPDATE users`.
Последнее — ровно то, что `deploy/INCIDENTS.md` §4 запрещает текстом: начисление
мимо книги навсегда разводит колонку с суммой движений, и `economy.drift_report`
после этого ругается на игрока каждый день, а настоящую утечку валюты в этом
шуме уже не разглядеть.

Запрет, живущий только в тексте рантбука, нарушается в третьем часу инцидента.
Поэтому здесь он сделан свойством ИНСТРУМЕНТА: единственный способ выдать
компенсацию — `compensate()`, а она умеет только звать денежные примитивы
game_logic, каждый из которых пишет в книгу той же транзакцией. Прямого доступа
к колонке баланса в этом модуле нет вовсе.

Три остальные вещи, ради которых модуль заведён:

  * ЦЕПОЧКА `invoice -> payment -> grant -> refund` по игроку. Стадии лежат в
    разных местах (строка `purchases`, `granted_payload`, движения книги), и
    собрать их в одну картинку глазами — это и есть те двадцать минут, которые
    тратятся на каждую жалобу «списали и не дали».

  * МАССОВАЯ компенсация по когорте инцидента. Когорта берётся из книги (кого
    задело), а не из головы, и выдача идемпотентна по инциденту: повторный
    запуск после обрыва не платит второй раз.

  * ФЛАГИ подозрительных. Не приговор, а причина посмотреть: каждый флаг
    объясняет себя строкой и числом.
"""
import logging
import time

from server import economy
from server import game_config as cfg
from server import game_logic as gl
from server.game_logic import db

log = logging.getLogger(__name__)

# Причины компенсации. Список закрытый, и это не бюрократия: `reason` уезжает в
# книгу, по нему потом считают «сколько стоил инцидент» и по нему же его ищут.
# Свободный текст в этом поле означает, что через месяц один и тот же инцидент
# найдётся под четырьмя разными названиями, а посчитать его будет нельзя.
#
# Префикс общий (support_) — по нему одним запросом поднимается вся ручная
# раздача за период, отдельно от игровых начислений.
COMPENSATION_REASONS = {
    "support_payment_lost": "платёж прошёл, товар не выдан (INCIDENTS §4)",
    "support_rollback": "откат релиза отнял прогресс (INCIDENTS §5)",
    "support_bad_event": "неверный ивент или множитель (INCIDENTS §9)",
    "support_data_loss": "восстановление из копии потеряло прогресс (RUNBOOK)",
    "support_goodwill": "жест доброй воли по обращению в поддержку",
}

# Что можно выдать. Только валюты, у которых есть денежный примитив с книгой:
# `cookies` (add_cookies) и `energy` (grant_energy). Престиж, уровни и
# батл-пасс сюда не входят намеренно — у них нет примитива, и «компенсация»
# ими означала бы прямой UPDATE, то есть ровно то, что модуль запрещает.
COMPENSATION_CURRENCIES = ("cookies", "energy")

# Потолок одной выдачи. Не про доверие к админу, а про опечатку: лишний ноль в
# массовой раздаче на тысячу человек — это конец экономики сезона, и заметят
# его по графику через сутки.
COMPENSATION_MAX = 1e12

# Сколько игроков разрешено накрыть одной массовой выдачей за раз. Больше —
# отдельными партиями: длинный HTTP-запрос, оборвавшийся на середине, не должен
# оставлять когорту в наполовину выплаченном состоянии (хотя идемпотентность
# по инциденту делает второй заход безопасным, см. _op_id).
COHORT_LIMIT = 5000


def _op_id(incident: str, user_id: int, currency: str) -> str:
    """Токен операции компенсации.

    Выведен из данных, а не случайный, и это главное свойство всей массовой
    выдачи: повторный запуск после обрыва (сеть, таймаут, «а точно ли прошло»)
    не платит второй раз — движение с таким токеном уже в книге, и примитив
    вернёт текущий баланс, ничего не начислив."""
    return f"support:{incident}:{currency}:{user_id}"


def _clean_incident(incident: str) -> str:
    """Метка инцидента: латиница, цифры, дефис, подчёркивание.

    Она уезжает в токен операции и в ref_id книги, то есть в append-only
    таблицу, откуда её уже не поправить. Пробел или кавычка в ней означали бы
    строку, которую потом не найти ни одним запросом."""
    clean = "".join(c for c in (incident or "").strip().lower()
                    if c.isalnum() or c in "-_")[:64]
    if not clean:
        raise ValueError("err_incident_required")
    return clean


# ---------- цепочка платежей по игроку ----------

def payment_chain(user_id: int, limit: int = 50) -> list[dict]:
    """`invoice -> payment -> grant -> refund` по каждому платежу игрока.

    Стадии восстанавливаются по строке `purchases` (её статус и отметки
    времени) и по книге операций. Соединение с книгой идёт по ТОКЕНУ ОПЕРАЦИИ,
    а не по времени: токены выдачи и возврата выведены из charge_id
    (`purchase:<charge>` / `refund:<charge>`, см. game_logic), поэтому связь
    однозначна. По времени она была бы догадкой — в ту же секунду у игрока
    могло пройти начисление за заказ.

    Пустой `ledger` у оплаченной покупки — это и есть та самая дыра «звёзды
    списались, товара нет»: строка платежа есть, движения денег нет."""
    rows = db.q("SELECT * FROM purchases WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?", (user_id, int(limit)))
    out = []
    for p in rows:
        charge = p.get("tg_payment_id") or ""
        ledger = db.q(
            "SELECT operation_id, seq, currency, amount, reason, balance_after, "
            "       created_at FROM economy_ledger "
            "WHERE user_id = ? AND (operation_id IN (?, ?) "
            "   OR (ref_type = 'charge' AND ref_id = ?)) "
            "ORDER BY created_at, seq",
            (user_id, f"purchase:{charge}", f"refund:{charge}", charge)) if charge else []
        out.append({
            "purchase_id": p["id"],
            "item_key": p["item_key"],
            "stars": p["stars_amount"],
            "charge_id": charge,
            "status": p["status"],
            # Четыре стадии одной строкой. `done` — стадия пройдена, `missing` —
            # не пройдена, и вот это в глазах разбирающего и есть ответ
            "stages": {
                "invoice": {"at": p["created_at"], "done": True},
                "payment": {"at": p["created_at"],
                            "done": p["status"] in ("paid", "fulfilled",
                                                    "refunded"),
                            "charge_id": charge or None},
                "grant": {"at": p.get("granted_at") or 0,
                          "done": bool(p.get("granted_at")),
                          "payload": p.get("granted_payload")},
                "refund": {"at": p.get("refunded_at") or 0,
                           "done": p["status"] == "refunded",
                           "stars": p.get("refund_stars"),
                           "prior_status": p.get("prior_status")},
            },
            "ledger": ledger,
        })
    return out


def player_card(user_id: int, ledger_limit: int = 100) -> dict:
    """Всё, что нужно для разбора обращения, одним ответом.

    Собрано в одну ручку намеренно: поддержка, которой надо сделать пять
    запросов, делает три и отвечает игроку по неполной картине."""
    user = db.get_user(user_id)
    if not user:
        raise ValueError("err_no_user")
    return {
        "user": {k: user.get(k) for k in (
            "user_id", "username", "first_name", "lang", "level", "cookies",
            "total_earned", "season_earned", "cookie_debt", "prestige_count",
            "total_clicks", "total_merges", "source_code", "referrer_id",
            "created_at", "last_seen_at", "notify_blocked")},
        "payments": payment_chain(user_id),
        # Сверка тут не украшение: расхождение колонки с книгой означает, что
        # этому игроку уже кто-то начислял руками, и разбирать жалобу надо
        # начиная с этого, а не с её текста
        "reconcile": economy.reconcile(user_id),
        "ledger": db.q(
            "SELECT operation_id, seq, currency, amount, reason, ref_type, "
            "       ref_id, balance_after, created_at FROM economy_ledger "
            "WHERE user_id = ? ORDER BY created_at DESC, seq DESC LIMIT ?",
            (user_id, int(ledger_limit))),
        # что игроку уже компенсировали: второй раз за тот же инцидент платить
        # не надо, а спрашивают об этом всегда
        # Шаблон LIKE идёт ПАРАМЕТРОМ, а не в тексте запроса: под PostgreSQL
        # плейсхолдеры переписываются в %s (db._sql), и литеральный процент в
        # SQL сломал бы подстановку. Значением он безопасен на обоих движках.
        # Подчёркивание оставлено одиночным джокером, а не экранировано: ESCAPE
        # ведёт себя по-разному, а игровых причин, начинающихся на "support",
        # не существует — ложных попаданий у шаблона нет
        "compensations": db.q(
            "SELECT operation_id, currency, amount, reason, ref_id, created_at "
            "FROM economy_ledger WHERE user_id = ? AND reason LIKE ? "
            "ORDER BY created_at DESC LIMIT 50", (user_id, "support_%")),
        "flags": flags_for(user_id),
        "analytics_recent": db.q(
            "SELECT event, value, economy_operation_id, platform, source, "
            "       campaign, variant, config_version, created_at "
            "FROM analytics_events WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 50", (user_id,)),
    }


# ---------- компенсация ----------

def compensate(user_id: int, currency: str, amount: float, reason: str,
               incident: str, note: str = "") -> dict:
    """Одна безопасная компенсация. Единственный законный способ доначислить.

    Безопасность здесь — это три «нельзя», и все три вшиты в код, а не в
    инструкцию:

      1. Нельзя мимо книги. Начисляет `add_cookies`/`grant_energy`, которые
         пишут движение той же транзакцией, что и саму колонку. Прямого UPDATE
         в модуле нет.
      2. Нельзя со свободной причиной: `reason` берётся из закрытого списка,
         иначе один инцидент растворится в четырёх формулировках.
      3. Нельзя дважды: токен операции выведен из (инцидент, валюта, игрок),
         повтор ничего не начисляет.

    Отнимать этой ручкой нельзя (`amount > 0`) — снятие выданного делает
    возврат Stars по своей процедуре, а «отнять руками» это тот же прямой
    UPDATE, только со знаком минус."""
    if reason not in COMPENSATION_REASONS:
        raise ValueError("err_bad_reason")
    if currency not in COMPENSATION_CURRENCIES:
        raise ValueError("err_bad_currency")
    amount = float(amount)
    if not (0 < amount <= COMPENSATION_MAX):
        raise ValueError("err_bad_amount")
    incident = _clean_incident(incident)
    if not db.get_user(user_id):
        raise ValueError("err_no_user")

    op = _op_id(incident, user_id, currency)
    already = economy.already_recorded(op, currency)
    if currency == "cookies":
        # count_earned=False: компенсация не заработок. Иначе она поехала бы в
        # total_earned и в сезонный зачёт, то есть подняла бы игроку уровень и
        # место в таблице лидеров за счёт нашей же ошибки
        balance = gl.add_cookies(user_id, amount, count_earned=False,
                                 operation_id=op, reason=reason,
                                 ref_type="incident", ref_id=incident)
    else:
        balance = gl.grant_energy(user_id, amount, reason, operation_id=op)

    if not already:
        # Отдельная строка аналитики: «сколько мы раздали по инциденту» — это
        # продуктовый вопрос, и отвечать на него книгой одной ей неудобно
        gl.track(f"support_compensation:{reason}", user_id, value=amount,
                 operation_id=op, incident=incident, currency=currency,
                 note=note[:200])
        log.warning("компенсация: игрок=%s %s %+.2f причина=%s инцидент=%s %s",
                    user_id, currency, amount, reason, incident, note[:200])
    return {"user_id": user_id, "currency": currency, "amount": amount,
            "reason": reason, "incident": incident, "operation_id": op,
            "balance_after": balance, "already_paid": already}


def cohort(kind: str, value: str = "", since: float = 0, until: float = 0,
           limit: int = COHORT_LIMIT) -> list[int]:
    """Кого задел инцидент. Список игроков, ничего не начисляя.

    Когорта берётся ИЗ ДАННЫХ, а не из выгрузки, собранной руками: список
    «пострадавших», составленный по жалобам, состоит из тех, кто дошёл до
    поддержки, — то есть из меньшинства.

    Виды:
      * `ledger_reason` — у кого в окне есть движение с этой причиной. Основной:
        что бы инцидент ни сломал, в книге это причина начисления или списания.
      * `purchase_status` — у кого платёж застрял в этом статусе (`unmatched`,
        `void`, `paid`). Прямо соответствует таблице из INCIDENTS §4.
      * `analytics_event` — кто попал в событие аналитики в окне. Для того, что
        в книге следа не оставило: неверный ивент, испорченный экран.
    """
    limit = max(1, min(int(limit), COHORT_LIMIT))
    until = until or time.time()
    if kind == "ledger_reason":
        rows = db.q("SELECT DISTINCT user_id FROM economy_ledger "
                    "WHERE reason = ? AND created_at >= ? AND created_at <= ? "
                    "ORDER BY user_id LIMIT ?", (value, since, until, limit))
    elif kind == "purchase_status":
        rows = db.q("SELECT DISTINCT user_id FROM purchases "
                    "WHERE status = ? AND created_at >= ? AND created_at <= ? "
                    "ORDER BY user_id LIMIT ?", (value, since, until, limit))
    elif kind == "analytics_event":
        rows = db.q("SELECT DISTINCT user_id FROM analytics_events "
                    "WHERE event = ? AND created_at >= ? AND created_at <= ? "
                    "AND user_id > 0 ORDER BY user_id LIMIT ?",
                    (value, since, until, limit))
    else:
        raise ValueError("err_bad_cohort")
    return [r["user_id"] for r in rows]


def compensate_bulk(user_ids, currency: str, amount: float, reason: str,
                    incident: str, note: str = "",
                    dry_run: bool = True) -> dict:
    """Массовая выдача по когорте инцидента.

    `dry_run=True` по умолчанию, и это не перестраховка: массовая раздача —
    единственное действие в админке, которое нельзя отменить (книга
    append-only, отнять руками нечем). Поэтому сначала показываем, скольким и
    сколько, и только потом платим.

    Идемпотентность — на уровне одного игрока, не всей партии: каждый платится
    своим токеном из (инцидент, валюта, игрок). Значит, партия, оборвавшаяся на
    середине, доигрывается повторным запуском, а уже заплатившим второй раз не
    достанется. Отдельного «состояния раздачи» для этого не нужно, и хорошо:
    состояние пришлось бы хранить, а хранимое состояние переживает не всякий
    рестарт."""
    ids = list(dict.fromkeys(int(u) for u in user_ids))[:COHORT_LIMIT]
    incident = _clean_incident(incident)
    if reason not in COMPENSATION_REASONS:
        raise ValueError("err_bad_reason")
    if currency not in COMPENSATION_CURRENCIES:
        raise ValueError("err_bad_currency")
    amount = float(amount)
    if not (0 < amount <= COMPENSATION_MAX):
        raise ValueError("err_bad_amount")

    if dry_run:
        return {"dry_run": True, "cohort_size": len(ids), "amount": amount,
                "currency": currency, "reason": reason, "incident": incident,
                "total": amount * len(ids), "sample": ids[:20]}

    paid, skipped, failed = 0, 0, []
    for uid in ids:
        try:
            res = compensate(uid, currency, amount, reason, incident, note)
            if res["already_paid"]:
                skipped += 1
            else:
                paid += 1
        except Exception as e:                       # noqa: BLE001
            # Один игрок не должен ронять раздачу: удалённый аккаунт в списке —
            # обычное дело, а падение посреди партии оставило бы остальных без
            # компенсации и без внятного «на ком остановились»
            failed.append({"user_id": uid, "error": str(e)})
    log.warning("массовая компенсация %s: инцидент=%s %s %.2f — выдано %d, "
                "уже было %d, ошибок %d", reason, incident, currency, amount,
                paid, skipped, len(failed))
    return {"dry_run": False, "cohort_size": len(ids), "paid": paid,
            "already_paid": skipped, "failed": failed[:50],
            "failed_count": len(failed), "incident": incident,
            "total": amount * paid}


# ---------- флаги подозрительных ----------
# Флаг — не приговор и не автоматическая блокировка. Автоматика тут была бы
# хуже отсутствия: правила ниже ловят и честные случаи (щедрый игрок позвал
# семью, кто-то реально играет по десять часов), а блокировка платящего игрока
# по эвристике стоит дороже любого фрода. Поэтому — только повод посмотреть.

# Кликов в час больше, чем физически возможно на серверном потолке CPS. Порог с
# запасом (потолок × 3600 — это НЕПРЕРЫВНЫЙ тап без сна), поэтому превышение
# означает не усердие, а обход лимитера.
CLICKS_PER_HOUR_LIMIT = cfg.MAX_CPS * 3600

# Рефералы, приведённые за час. Живые приглашения так не приходят.
REF_BURST_LIMIT = 10
REF_BURST_WINDOW = 3600

# Сколько возвратов подряд считается схемой, а не передумавшим покупателем.
REFUND_LIMIT = 3


def flags_for(user_id: int) -> list[dict]:
    """Флаги по одному игроку: [{флаг, значение, пояснение}]."""
    user = db.get_user(user_id)
    if not user:
        return []
    out = []
    now = time.time()

    # 1. Баланс не объясняется книгой. Либо потерянный апдейт, либо кто-то
    # уже правил баланс руками — и то, и другое надо знать ДО ответа игроку
    drift = {k: v["drift"] for k, v in economy.reconcile(user_id).items()
             if abs(v["drift"]) > 1e-6}
    if drift:
        out.append({"flag": "ledger_drift", "value": drift,
                    "why": "колонка баланса не сходится с суммой движений книги"})

    # 2. Кликов больше, чем позволяет серверный потолок за всё время жизни
    # аккаунта. Считаем от регистрации, а не от «сегодня»: накрутка обычно
    # разовая и в суточное окно не попадает
    age_h = max((now - float(user.get("created_at") or now)) / 3600.0, 1.0)
    rate = float(user.get("total_clicks") or 0) / age_h
    if rate > CLICKS_PER_HOUR_LIMIT:
        out.append({"flag": "click_rate", "value": round(rate, 1),
                    "why": f"кликов/час за всё время жизни аккаунта выше "
                           f"физического потолка ({CLICKS_PER_HOUR_LIMIT})"})

    # 3. Всплеск приглашений: N рефералов в одно окно
    burst = db.q1(
        "SELECT COUNT(*) c FROM referrals WHERE referrer_id = ? "
        "AND created_at > ?", (user_id, now - REF_BURST_WINDOW))["c"]
    if burst >= REF_BURST_LIMIT:
        out.append({"flag": "referral_burst", "value": burst,
                    "why": f"{burst} приглашений за час"})

    # 4. Приглашённые, которые не сыграли ни одного хода. Ферма ботов выглядит
    # именно так: аккаунты заведены, награда за них получена, играть — некому
    dead = db.q1(
        "SELECT COUNT(*) c FROM referrals r JOIN users u "
        "ON u.user_id = r.referred_id WHERE r.referrer_id = ? "
        "AND u.total_clicks = 0 AND u.total_merges = 0", (user_id,))["c"]
    if dead >= REF_BURST_LIMIT:
        out.append({"flag": "referral_dead", "value": dead,
                    "why": f"{dead} приглашённых не сделали ни клика, ни слияния"})

    # 5. Возвраты Stars: разово это норма, серией — схема
    refunds = db.q1("SELECT COUNT(*) c FROM purchases WHERE user_id = ? "
                    "AND status = 'refunded'", (user_id,))["c"]
    if refunds >= REFUND_LIMIT:
        out.append({"flag": "refund_series", "value": refunds,
                    "why": f"{refunds} возвратов Stars"})

    # 6. Непогашенный долг после возврата: игрок успел потратить выданное
    debt = float(user.get("cookie_debt") or 0)
    if debt > 0:
        out.append({"flag": "refund_debt", "value": debt,
                    "why": "возврат забрал больше, чем было на балансе"})
    return out


def suspicious(limit: int = 50) -> dict:
    """Кандидаты на разбор, собранные по всей базе.

    Каждый список — отдельный SQL по индексу, а не проход `flags_for` по всем
    игрокам: последнее означало бы полный скан книги на каждого, то есть ручку,
    которую нельзя открыть на живой базе.

    Исключение — сверка (`ledger_drift`): она и правда идёт по игрокам, но
    `economy.drift_report` уже написан с собственным пределом."""
    limit = max(1, min(int(limit), 500))
    now = time.time()
    return {
        "ledger_drift": economy.drift_report(limit),
        "click_rate": db.q(
            "SELECT user_id, username, total_clicks, created_at, "
            "       total_clicks / (((? - created_at) / 3600.0) + 1) rate "
            "FROM users WHERE total_clicks > 0 AND created_at > 0 "
            "  AND total_clicks / (((? - created_at) / 3600.0) + 1) > ? "
            "ORDER BY rate DESC LIMIT ?",
            (now, now, CLICKS_PER_HOUR_LIMIT, limit)),
        "referral_burst": db.q(
            "SELECT referrer_id AS user_id, COUNT(*) c FROM referrals "
            "WHERE created_at > ? GROUP BY referrer_id HAVING COUNT(*) >= ? "
            "ORDER BY c DESC LIMIT ?",
            (now - REF_BURST_WINDOW, REF_BURST_LIMIT, limit)),
        "referral_dead": db.q(
            "SELECT r.referrer_id AS user_id, COUNT(*) c FROM referrals r "
            "JOIN users u ON u.user_id = r.referred_id "
            "WHERE u.total_clicks = 0 AND u.total_merges = 0 "
            "GROUP BY r.referrer_id HAVING COUNT(*) >= ? "
            "ORDER BY c DESC LIMIT ?", (REF_BURST_LIMIT, limit)),
        "refund_series": db.q(
            "SELECT user_id, COUNT(*) c FROM purchases WHERE status = 'refunded' "
            "GROUP BY user_id HAVING COUNT(*) >= ? ORDER BY c DESC LIMIT ?",
            (REFUND_LIMIT, limit)),
        "refund_debt": db.q(
            "SELECT user_id, username, cookie_debt FROM users "
            "WHERE cookie_debt > 0 ORDER BY cookie_debt DESC LIMIT ?", (limit,)),
        "thresholds": {
            "clicks_per_hour": CLICKS_PER_HOUR_LIMIT,
            "referrals_per_hour": REF_BURST_LIMIT,
            "refunds": REFUND_LIMIT,
        },
    }
