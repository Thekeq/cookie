"""Стенд: живые сессии игроков через настоящий API, но в ускоренном времени.

Зачем отдельно от balance_sim.py. Тот считает экономику формулами напрямую и
отвечает на вопрос «сходятся ли числа». Здесь наоборот: игрок ходит через
реальные HTTP-ручки со своим initData, поэтому ловится то, чего в формулах не
видно совсем, — 500-ки, рассинхрон ответа и состояния, ручки, которые молча
ничего не делают, залипшие таймеры, недостижимые условия.

Как устроено ускорение времени. В серверном коде нет ни `from time import`,
ни `datetime.now()` — всё идёт через `time.time()`. Поэтому подмены одной
функции в модуле `time` достаточно, чтобы для всей игры прошли сутки:
регенерация энергии, оффлайн-доход, закваска, дейлики, дуэли, сезоны и
лимиты запросов живут по одному и тому же таймеру.

Патчим ТОЛЬКО time.time(). monotonic/perf_counter не трогаем: на них висят
таймауты httpx и внутренности asyncio, и прыжок вперёд там ломает не игру,
а сам стенд.

Использование:

    import simplay
    sim = simplay.Sim("newbie")
    p = sim.player(1)
    p.auth()
    p.click(50)
    sim.advance(hours=8)
    p.state()
    sim.report()          # печатает найденные аномалии и возвращает их списком
"""
import hashlib
import hmac
import json
import os
import tempfile
import time as _time
import traceback
from urllib.parse import urlencode

# --- 1. управляемые часы -----------------------------------------------------
# Ставим ДО импорта приложения: так даже значения, посчитанные на импорте,
# увидят наше время, а не настоящее.
_real_time = _time.time
_offset = 0.0


def _fake_time() -> float:
    return _real_time() + _offset


_time.time = _fake_time


def advance_seconds(seconds: float) -> None:
    """Двигает игровое время вперёд. Назад нельзя — половина игры считает
    дельты и от отрицательных чисел ведёт себя непредсказуемо."""
    global _offset
    if seconds < 0:
        raise ValueError("время назад не двигаем: дельты в игре этого не ждут")
    _offset += seconds


def now() -> float:
    return _fake_time()


# --- 2. окружение ------------------------------------------------------------
BOT_TOKEN = "123456789:AAtestTOKENtestTOKENtestTOKENtest12"
os.environ.setdefault("BOT_TOKEN", BOT_TOKEN)
BOT_TOKEN = os.environ["BOT_TOKEN"]


def _fresh_db(tag: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        f"cookie_sim_{tag}_{os.getpid()}.db")


class Anomaly:
    """Найденная странность. severity: bug | suspect | note."""

    def __init__(self, severity: str, what: str, detail: str = ""):
        self.severity = severity
        self.what = what
        self.detail = detail

    def __repr__(self):
        d = f" — {self.detail}" if self.detail else ""
        return f"[{self.severity}] {self.what}{d}"


_sim_created = False


class Sim:
    """Одна песочница: своя база, свои игроки, свои часы.

    Ровно один Sim на процесс: путь к базе читается на импорте модуля db, и
    второй Sim молча сел бы на базу первого. Нужны две песочницы — запускай
    два процесса."""

    def __init__(self, tag: str = "sim", verbose: bool = True):
        global _sim_created
        if _sim_created:
            raise RuntimeError(
                "второй Sim в одном процессе сядет на базу первого — "
                "запусти отдельный процесс")
        _sim_created = True
        os.environ["DATABASE_PATH"] = _fresh_db(tag)
        self.verbose = verbose
        self.anomalies: list[Anomaly] = []
        self.calls = 0
        self.tag = tag
        self._elapsed = 0.0

        from fastapi.testclient import TestClient  # импорт после DATABASE_PATH
        from main import app
        from server.game_logic import db
        import server.game_logic as gl
        import server.game_config as cfg

        self.client = TestClient(app)
        self.db = db
        self.gl = gl
        self.cfg = cfg
        self._players: dict[int, "Player"] = {}
        self._base_uid = 950_000_000 + (os.getpid() % 10_000) * 100

    # --- время ---
    def advance(self, hours: float = 0, minutes: float = 0, seconds: float = 0,
                days: float = 0) -> None:
        dt = days * 86400 + hours * 3600 + minutes * 60 + seconds
        advance_seconds(dt)
        self._elapsed += dt
        self.log(f"~ время +{_human(dt)} (всего {_human(self._elapsed)})")

    @property
    def elapsed_hours(self) -> float:
        return self._elapsed / 3600

    # --- игроки ---
    def player(self, n: int = 1, name: str = "") -> "Player":
        if n not in self._players:
            self._players[n] = Player(self, self._base_uid + n,
                                      name or f"P{n}")
        return self._players[n]

    # --- журнал ---
    def log(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    def flag(self, severity: str, what: str, detail: str = "") -> Anomaly:
        a = Anomaly(severity, what, detail)
        self.anomalies.append(a)
        self.log(f"  !! {a}")
        return a

    def report(self) -> list[Anomaly]:
        print(f"\n=== {self.tag}: {self.calls} запросов, "
              f"игрового времени {_human(self._elapsed)} ===")
        if not self.anomalies:
            print("аномалий не найдено")
        for a in self.anomalies:
            print(f"  {a}")
        return self.anomalies


class Player:
    """Игрок, который ходит через настоящие ручки со своим initData."""

    def __init__(self, sim: Sim, uid: int, name: str):
        self.sim = sim
        self.uid = uid
        self.name = name
        self.last: dict = {}
        self._batch = 0

    # --- транспорт ---
    def _headers(self, start_param: str = "") -> dict:
        data = {"user": json.dumps({"id": self.uid, "username": self.name.lower(),
                                    "first_name": self.name}),
                "auth_date": str(int(now()))}
        if start_param:
            data["start_param"] = start_param
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        data["hash"] = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
        return {"Authorization": "tma " + urlencode(data)}

    def call(self, method: str, path: str, body=None, start_param: str = "",
             expect_ok: bool = True):
        """Любой 5xx — это баг по определению: клиент такого не заслуживает.
        4xx возвращаем как есть: отказ по правилам игры — нормальный ответ."""
        self.sim.calls += 1
        try:
            fn = self.sim.client.get if method == "GET" else self.sim.client.post
            kw = {"headers": self._headers(start_param)}
            if body is not None:
                kw["json"] = body
            r = fn(path, **kw)
        except Exception as e:                      # исключение сквозь ручку
            self.sim.flag("bug", f"{method} {path} упал исключением",
                          f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}")
            return None
        if r.status_code >= 500:
            self.sim.flag("bug", f"{method} {path} -> {r.status_code}",
                          r.text[:400])
            return None
        if r.status_code >= 400:
            if expect_ok:
                self.sim.log(f"  .. {self.name} {method} {path} отказ "
                             f"{r.status_code}: {_detail(r)}")
            return {"__error__": _detail(r), "__status__": r.status_code}
        data = r.json()
        if isinstance(data, dict) and "user" in data:
            self.last = data
            self.check(data)
        elif isinstance(data, dict) and self.last.get("user"):
            # Частичные ответы (клик, золотое печенье) полного состояния не
            # шлют. Без этого кэш устаревал молча, и сценарий делал вывод
            # «тапы не приносят печенек», хотя приносят.
            for k in ("cookies", "energy", "xp"):
                if isinstance(data.get(k), (int, float)):
                    self.last["user"][k] = data[k]
        return data

    def get(self, path: str, **kw):
        return self.call("GET", path, **kw)

    def post(self, path: str, body=None, **kw):
        return self.call("POST", path, body, **kw)

    # --- инварианты, которые не должны нарушаться НИКОГДА ---
    def check(self, st: dict) -> None:
        cfg, u = self.sim.cfg, st.get("user", {})
        f = self.sim.flag
        c = u.get("cookies")
        if c is None or c != c or c in (float("inf"), float("-inf")):
            f("bug", "cookies не число", str(c))
        elif c < 0:
            f("bug", "cookies ушли в минус", f"{c}")
        e, me = u.get("energy", 0), u.get("max_energy", 0)
        if e < 0 or (me and e > me + 0.001):
            f("bug", "энергия вне границ", f"{e} из {me}")
        lvl = u.get("level", 1)
        if not (1 <= lvl <= cfg.MAX_LEVEL):
            f("bug", "уровень вне диапазона", f"{lvl}")
        xp, xpn = u.get("xp", 0), u.get("xp_next")
        if xpn is not None and xp > xpn:
            f("bug", "xp перевалил за порог уровня, а уровень не выдан",
              f"{xp} > {xpn} на уровне {lvl}")
        if u.get("click_power", 1) <= 0:
            f("bug", "сила клика не положительна", str(u.get("click_power")))
        sc = st.get("spawn_cost")
        if sc is not None and (sc <= 0 or sc != sc):
            f("bug", "цена спавна невалидна", str(sc))
        cells = st.get("board_cells") or {}
        if cells and len(st.get("board", [])) > cells.get("unlocked", 0):
            f("bug", "печенек на доске больше, чем открытых клеток",
              f"{len(st.get('board', []))} > {cells.get('unlocked')}")
        pr = st.get("prestige") or {}
        if pr and pr.get("multiplier", 1) < 1:
            f("bug", "множитель престижа меньше единицы", str(pr.get("multiplier")))

    # --- действия игрока ---
    def auth(self, ref: str = "") -> dict:
        return self.post("/api/auth", start_param=ref) or {}

    def state(self) -> dict:
        return self.get("/api/state") or {}

    def click(self, n: int = 30) -> dict:
        """Один батч тапов. batch_id уникален, как у честного клиента."""
        self._batch += 1
        return self.post("/api/click", {"clicks": min(200, n),
                                        "batch_id": f"{self.name}-b{self._batch}"}) or {}

    def click_upgrade(self) -> dict:
        return self.post("/api/click/upgrade") or {}

    def spawn(self, level: int = 1) -> dict:
        return self.post("/api/merge/spawn", {"level": level}) or {}

    def move(self, a: int, b: int) -> dict:
        return self.post("/api/merge/move", {"from_cell": a, "to_cell": b}) or {}

    def trash(self, cell: int) -> dict:
        return self.post("/api/merge/trash", {"cell": cell}) or {}

    def board(self) -> dict[int, int]:
        st = self.last or self.state()
        return {b["cell"]: b["item_level"] for b in st.get("board", [])}

    def merge_once(self) -> bool:
        """Сливает первую найденную пару. False — сливать нечего."""
        board = self.board()
        by_level: dict[int, list[int]] = {}
        for cell, lvl in board.items():
            by_level.setdefault(lvl, []).append(cell)
        for lvl, cells in sorted(by_level.items()):
            if len(cells) >= 2 and lvl < self.sim.cfg.MAX_ITEM_LEVEL:
                self.move(cells[0], cells[1])
                return True
        return False

    def merge_all(self, limit: int = 60) -> int:
        n = 0
        while n < limit and self.merge_once():
            n += 1
        return n

    def buy_building(self, key: str) -> dict:
        return self.post("/api/farm/buy_building", {"key": key}) or {}

    def farm(self) -> dict:
        return self.get("/api/farm") or {}

    def daily(self) -> dict:
        return self.post("/api/daily/claim") or {}

    def quests(self) -> dict:
        return self.get("/api/quests") or {}

    def claim_quest(self, key: str) -> dict:
        return self.post("/api/quests/claim", {"key": key}) or {}

    def levels(self) -> dict:
        return self.get("/api/levels") or {}

    def claim_level(self) -> dict:
        return self.post("/api/levels/claim") or {}

    def orders(self) -> dict:
        return self.get("/api/orders") or {}

    def recipes(self) -> dict:
        return self.get("/api/recipes") or {}

    def set_recipe(self, key: str) -> dict:
        return self.post("/api/recipes/set", {"key": key}) or {}

    def duel(self) -> dict:
        return self.get("/api/duel") or {}

    def prestige(self) -> dict:
        return self.post("/api/prestige") or {}

    # --- удобства для сценариев ---
    def refresh(self) -> dict:
        """Перечитать состояние с сервера. cookies()/level() отдают КЭШ —
        после сдвига часов он врёт, пока не сходишь за свежим."""
        return self.state()

    def cookies(self) -> float:
        return (self.last or self.state()).get("user", {}).get("cookies", 0)

    def level(self) -> int:
        return (self.last or self.state()).get("user", {}).get("level", 1)

    def grant(self, cookies: float) -> None:
        """Выдать печенек напрямую — чтобы дойти до поздней игры за секунды,
        не имитируя недели тапов. Экономику этим не проверяют, только механики."""
        u = self.sim.db.get_user(self.uid)
        self.sim.db.update_user(self.uid, cookies=u["cookies"] + cookies)


def _detail(r) -> str:
    try:
        return str(r.json().get("detail", r.text[:200]))
    except Exception:
        return r.text[:200]


def _human(sec: float) -> str:
    if sec >= 86400:
        return f"{sec / 86400:.1f} сут"
    if sec >= 3600:
        return f"{sec / 3600:.1f} ч"
    if sec >= 60:
        return f"{sec / 60:.0f} мин"
    return f"{sec:.0f} с"


if __name__ == "__main__":
    # смоук: стенд сам должен работать, прежде чем им что-то проверять
    sim = Sim("smoke")
    p = sim.player(1, "Smoke")
    st = p.auth()
    assert st.get("user"), st
    print(f"уровень {p.level()}, печенек {p.cookies():.0f}")
    p.click(50)
    before = p.cookies()
    sim.advance(hours=6)
    st = p.state()
    print(f"после 6 ч оффлайна: печенек {p.cookies():.0f} (было {before:.0f}), "
          f"энергия {st['user']['energy']:.0f}/{st['user']['max_energy']}")
    assert st["user"]["energy"] >= 1, "энергия не восстановилась за 6 часов"
    sim.report()
