"""Проверка версии состояния: действие применяется к тому экрану, с которого
его отправили.

Зачем. Перетаскивание на доске адресуется НОМЕРАМИ клеток, а не самими
печеньками. Игрок, у которого приложение открыто и на телефоне, и на десктопе
(в Telegram это одно нажатие), отправляет «слей 3 и 4» с раскладки, которой
уже нет: в клетках 3 и 4 лежит другое, и слияние происходит — просто не то,
которое он видел. Ни один условный UPDATE это не ловит: запрос корректен,
неверна картинка, из которой он родился.

Как. Каждый ответ full_state несёт `revision: {user, board}`; клиент возвращает
увиденное значение заголовком, и расхождение превращается в 409 со свежим
состоянием вместо молча применённого чужого хода.

Заголовка нет — проверки нет. Старые сборки Mini App живут в чатах вечно и
обязаны продолжать работать; их поведение не меняется ни на байт.

Про X-User-Revision. Механика поддержана симметрично, но НАШ клиент его не
шлёт, и это не недоделка: user_revision двигает каждый батч кликов и каждый
сбор дохода фермы, то есть он меняется под игроком сам, без его участия.
Отбивать по нему покупки означало бы 409 посреди нормальной игры. Баланс и без
того защищён условными списаниями (`spend_cookies`, `WHERE cookies >= ?`) —
там проверять нечего. Заголовок остаётся для сервис-клиентов и отладки.

Здесь же живёт X-Op-Id — соседняя по смыслу защита: версия отбивает действие
со старого экрана, токен операции склеивает повтор ОДНОГО И ТОГО ЖЕ действия
в один. Первое про «ход устарел», второе про «ответ не доехал».

И общий лимитер (`rate_limit`) — он тоже про «кто и что имеет право сделать»,
только считает не версии, а частоту. Точечные лимиты на дорогих ручках
(`gl.check_rate_limit` в роутерах) остаются на месте: они знают цену КОНКРЕТНОЙ
ручки. Здесь — потолок сверху, который есть у каждой ручки без исключения.
"""
import re

from fastapi import Depends, Header, HTTPException, Request, Response

from server import cache, obs, settings
from server.auth import tg_user
from server.economy import ConflictError
from server.game_logic import db

# заголовок -> колонка в users
REVISION_COLUMNS = {"user": "user_revision", "board": "board_revision"}


def _wanted(raw: str) -> int | None:
    """Версия из заголовка или None, если её нет.

    Мусор («abc», пустая строка, отрицательное) трактуется как «не прислали»:
    сломанный или подменённый заголовок не должен превращаться в неубиваемый
    409 — тогда испорченный клиент терял бы возможность играть совсем."""
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


# в токене оставляем только безобидные символы: он идёт в SQL параметром, но
# ещё и в логи, и в ключ таблицы — незачем пускать туда перевод строки
_OP_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


async def op_token(
        tg: dict = Depends(tg_user),
        x_op_id: str = Header(default="", alias="X-Op-Id"),
) -> str:
    """Токен идемпотентности денежного действия из заголовка X-Op-Id.

    Один токен = одно нажатие кнопки. Клиент генерирует его перед запросом и
    повторяет БЕЗ ИЗМЕНЕНИЙ, если ответ не доехал; сервер по нему отдаёт
    сохранённый ответ первой попытки вместо второй награды или err_claimed.

    Токен всегда приклеивается к игроку. economy_ops.operation_id уникален по
    всей базе, поэтому без префикса чужой заранее известный токен занимал бы
    операцию соседа — и тот получил бы 409 на свой законный клейм, а то и
    чужой ответ. Префикс `op:` отделяет клиентские токены от служебных
    (`order_claim:{id}`, `farm:{uid}:{ms}`), которые примитивы ставят сами.

    Пусто — значит проверки нет: старые сборки Mini App заголовка не шлют."""
    raw = _OP_SAFE.sub("", (x_op_id or "").strip())[:64]
    return f"op:{tg['id']}:{raw}" if raw else ""


async def require_revision(
        tg: dict = Depends(tg_user),
        x_user_revision: str = Header(default="", alias="X-User-Revision"),
        x_board_revision: str = Header(default="", alias="X-Board-Revision"),
) -> dict:
    """Тот же tg_user, но с проверкой версии. Возвращает tg, поэтому в ручке
    достаточно заменить Depends(tg_user) на Depends(require_revision)."""
    wanted = {col: v for col, v in
              (("user_revision", _wanted(x_user_revision)),
               ("board_revision", _wanted(x_board_revision))) if v is not None}
    if not wanted:
        return tg
    # имена колонок — из константы выше, не из запроса: в f-string попадает
    # только то, что написано в этом файле
    row = db.q1(f"SELECT {', '.join(wanted)} FROM users WHERE user_id = ?", (tg["id"],))
    if row is None:
        return tg                    # игрока нет — пусть ручка отдаст свой 404
    if any(row[col] != v for col, v in wanted.items()):
        raise ConflictError(tg["id"])
    return tg


# ---------- общий лимитер ----------
#
# Значения читаются из server.settings, если они там появятся, иначе берутся
# отсюда: settings — единственная точка чтения окружения, а дефолт должен
# лежать рядом с кодом, который его применяет.
#
# Числа выбраны как ПОТОЛОК, а не как рабочая частота. Нормальный клиент шлёт
# /api/state раз в 30 секунд и батчи кликов раз в пару секунд; точечные лимиты
# в роутерах (STATE_PER_MINUTE=40, PROMO_ATTEMPTS_PER_HOUR=10 и прочие) режут
# сильно раньше. Задача этого слоя другая — не дать обойти их перебором ручек
# и не дать анониму молотить в дверь.
def _cfg(name: str, default):
    return type(default)(getattr(settings, name, default))


RATE_LIMIT_ENABLED = _cfg("RATE_LIMIT_ENABLED", True)
# авторизованный игрок: на одну ручку и суммарно по всем
RATE_LIMIT_USER_PER_MINUTE = _cfg("RATE_LIMIT_USER_PER_MINUTE", 240)
RATE_LIMIT_USER_TOTAL_PER_MINUTE = _cfg("RATE_LIMIT_USER_TOTAL_PER_MINUTE", 1200)
# аноним (подпись не сошлась или заголовка нет вовсе) — считаем по IP
RATE_LIMIT_ANON_PER_MINUTE = _cfg("RATE_LIMIT_ANON_PER_MINUTE", 60)
RATE_LIMIT_ANON_TOTAL_PER_MINUTE = _cfg("RATE_LIMIT_ANON_TOTAL_PER_MINUTE", 300)
RATE_LIMIT_WINDOW = _cfg("RATE_LIMIT_WINDOW", 60.0)

# Ручки, по которым принимают решения машины, а не игроки. /livez и /readyz
# опрашивает балансировщик несколько раз в секунду, и 429 на них означает
# вывод здорового процесса из ротации; webhook — это трафик Telegram, там
# лимит превращается в потерянные апдейты (в том числе о платежах).
RATE_LIMIT_EXEMPT = frozenset({
    "/livez", "/readyz", "/healthz", "/metrics", settings.WEBHOOK_PATH,
})

# Кому верим, когда он представляется чужим адресом. Заголовок с адресом
# ставит кто угодно: приняв его от произвольного клиента, мы бы дали ему
# бесконечный запас «разных IP» — то есть отключили бы лимит для анонимов ровно
# там, где он единственный.
#
# Локальный сосед по машине — это либо туннель Cloudflare (cloudflared держит
# соединение наружу и ходит к нам на 127.0.0.1), либо сам сервис: снаружи так
# не представишься, пакет с чужого адреса до петлевого интерфейса не доедет.
# Всё остальное — только с явным TRUSTED_PROXY (см. settings).
TRUSTED_PROXIES = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def client_ip(request: Request) -> str:
    """Адрес игрока с точки зрения лимитера.

    За Cloudflare `request.client.host` — это адрес прокси, а не игрока, и без
    заголовка все игроки схлопываются в один счётчик: анонимный лимит они
    выжигают друг другу.

    Порядок источников не косметический. `CF-Connecting-IP` Cloudflare
    ПЕРЕЗАПИСЫВАЕТ своим значением, а `X-Forwarded-For` — дописывает: пришедшее
    от клиента остаётся в начале списка, поэтому первый элемент выбирает сам
    отправитель, а последний ставит прокси. Отсюда `[-1]`, а не `[0]` — со
    старым `[0]` строка `X-Forwarded-For: 1.2.3.4` давала свежий IP на каждый
    запрос. `X-Real-IP` не читаем вовсе: его ставил nginx, которого больше нет,
    а Cloudflare чужой заголовок с таким именем пропускает как есть.
    """
    peer = (request.client.host if request.client else "") or "-"
    if settings.TRUSTED_PROXY or peer in TRUSTED_PROXIES:
        fwd = request.headers.get("cf-connecting-ip", "").strip()
        if not fwd:
            hops = [p.strip() for p
                    in request.headers.get("x-forwarded-for", "").split(",")]
            hops = [p for p in hops if p]
            fwd = hops[-1] if hops else ""
        if fwd:
            return fwd[:64]
    return peer[:64]


def endpoint_key(request: Request) -> str:
    """Метка ручки — ШАБЛОН пути (`/api/user/{uid}`), а не сам путь.

    С сырым путём каждая ссылка заводила бы отдельный ключ в Redis, и лимит
    перестал бы что-либо ограничивать: у любого запроса счётчик был бы свой."""
    route = request.scope.get("route")
    return getattr(route, "path", None) or "other"


async def rate_limit(
        request: Request,
        response: Response,
        authorization: str = Header(default=""),
        x_lang: str = Header(default="en", alias="X-Lang"),
) -> None:
    """Потолок частоты на КАЖДУЮ ручку API. Вешается на приложение целиком
    (`FastAPI(dependencies=[Depends(rate_limit)])`), поэтому новую ручку нельзя
    забыть прикрыть — это и есть причина не делать его декоратором.

    Ключи: user + IP + endpoint. Игрок опознаётся ТОЛЬКО по проверенной подписи:
    возьми мы user_id из непроверенной initData, любой желающий выжигал бы
    чужой лимит, подставляя чужой номер, — то есть лимитер сам стал бы оружием.
    Не опознанные запросы считаются по одному IP на всех.

    IP входит в ключ вместе с user_id намеренно: за одним адресом оператора
    сидят тысячи игроков, и общий на них счётчик отбивал бы законную игру, а
    для авторизованного user_id уже уникален сам по себе.

    Middleware'ом это быть не может: шаблон маршрута известен только после
    роутинга, а до него ключом стал бы сырой путь.
    """
    if not RATE_LIMIT_ENABLED:
        return
    path = endpoint_key(request)
    if path in RATE_LIMIT_EXEMPT:
        return

    # Мягкое опознание: отказ здесь не наш — его отдаст сама ручка со своим
    # текстом. Результат (и успех, и отказ) оседает на request.state, поэтому
    # подпись проверяется один раз на запрос, а не дважды.
    try:
        uid = (await tg_user(request, response, authorization, x_lang))["id"]
    except HTTPException:
        uid = 0

    ip = client_ip(request)
    if uid:
        checks = ((f"rl:{uid}:{ip}:{path}", RATE_LIMIT_USER_PER_MINUTE),
                  (f"rl:{uid}:{ip}", RATE_LIMIT_USER_TOTAL_PER_MINUTE))
    else:
        checks = ((f"rl:anon:{ip}:{path}", RATE_LIMIT_ANON_PER_MINUTE),
                  (f"rl:anon:{ip}", RATE_LIMIT_ANON_TOTAL_PER_MINUTE))

    for key, limit in checks:
        allowed, _ = cache.incr_window(key, limit, RATE_LIMIT_WINDOW)
        if not allowed:
            obs.inc("rate_limited_total", path=path,
                    kind="user" if uid else "anon")
            # Retry-After — не вежливость: без него клиент повторяет сразу и
            # продлевает себе окно, потому что счётчик считает и отбитые тоже
            raise HTTPException(429, "err_too_fast",
                                headers={"Retry-After": str(int(RATE_LIMIT_WINDOW))})
