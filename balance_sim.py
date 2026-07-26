"""Симуляция прогрессии игрока по конфигу — без БД и сервера.

Модель активного игрока: кликает с реальным CPS ~5, тратит печеньки жадно
(клик-апгрейд -> здания фермы -> доска), сессии по 10 минут с перерывами.
Смотрим: когда первый «затык» (нечего купить > N минут), когда 3-5 уровень,
сколько занимает батл-пасс.
"""
import sys

from server import game_config as cfg

CLICK_CPS = 5.0         # реалистичный темп тапов
SESSION_MIN = 10         # длина сессии
BREAK_MIN = 110          # перерыв между сессиями (6 сессий ~ каждые 2 часа)
# длину прогона можно задать аргументом: `python balance_sim.py 720` — месяц.
# Проверки здоровья считаются только для дефолтных 72ч, иначе они бессмысленны.
SIM_HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 72

state = {
    "cookies": 0.0, "earned": 0.0, "xp": 0.0, "level": 1, "click_level": 1,
    "energy": float(cfg.max_energy(1)), "buildings": {},
    # доска моделируется поимённо: именно на ней держится экономика, и
    # счётчиком «сколько предметов» её проверить нельзя
    "board": [], "best_item": 0,
}
# откуда пришли печеньки — без этой разбивки нельзя отличить гиперинфляцию
# от того, что стенд стал честнее моделировать один из каналов дохода
earned_src = {"farm": 0.0, "board": 0.0, "click": 0.0}
log_events = []


def xp_level_check(t_min):
    while state["level"] < cfg.MAX_LEVEL and \
            state["xp"] >= cfg.xp_for_level(state["level"] + 1):
        state["level"] += 1
        state["cookies"] += cfg.level_reward(state["level"])["cookies"]
        log_events.append((t_min, f"LEVEL {state['level']}"))


def farm_cps():
    return sum(cfg.FARM_BUILDINGS[k]["cps"] * v for k, v in state["buildings"].items())


def board_income_ph():
    return sum(cfg.passive_income_per_hour(l) for l in state["board"])


def base_income_ph():
    """Доход без кликов — от него считается сила клика (как в game_logic)."""
    return farm_cps() * 3600 + board_income_ph()


def max_item_unlocked():
    return max((l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                if cfg.item_unlock_level(l) <= state["level"]), default=1)


def claim_record(new_level):
    """XP за личный рекорд тира — основной источник уровней."""
    if new_level <= state["best_item"]:
        return
    for l in range(max(state["best_item"] + 1, 2), new_level + 1):
        state["xp"] += cfg.first_item_xp(l)
    state["best_item"] = new_level


def merge_board():
    """Жадно сливает всё, что сливается, не выше открытого тира."""
    cap = max_item_unlocked()
    merged = True
    while merged:
        merged = False
        for lvl in sorted(set(state["board"])):
            if lvl + 1 > cap or state["board"].count(lvl) < 2:
                continue
            state["board"].remove(lvl)
            state["board"].remove(lvl)
            state["board"].append(lvl + 1)
            state["xp"] += cfg.merge_reward_xp(lvl + 1)
            claim_record(lvl + 1)
            merged = True
            break


def try_spend(t_min):
    """Жадная стратегия: покупаем то, что даёт больше всего cps на печеньку.

    Клик и здания сравниваются ОДНОЙ метрикой. Раньше тут стоял костыль
    `click_level < 12`: сила клика была линейной при экспоненциальной цене,
    и качать её дальше 12 не имело смысла ни при каких числах. Теперь ветка
    клика живая, и симуляция обязана выбирать честно — иначе она врёт."""
    spent_something = True
    while spent_something:
        spent_something = False
        best, best_ratio, best_cost, best_kind = None, 0.0, 0.0, None

        # клик: прирост дохода считаем по реальному темпу тапа в сессии
        base = base_income_ph()
        up_cost = cfg.click_upgrade_cost(state["click_level"], base)
        gain_cps = (cfg.click_power(state["click_level"] + 1, base)
                    - cfg.click_power(state["click_level"], base)) * CLICK_CPS
        # клик работает только пока игрок в сессии — режем долей активного времени
        gain_cps *= SESSION_MIN / (SESSION_MIN + BREAK_MIN)
        if state["click_level"] >= cfg.click_max_level(state["level"]):
            up_cost, gain_cps = float("inf"), 0.0   # упёрлись в потолок уровня
        if state["cookies"] >= up_cost and gain_cps / up_cost > best_ratio:
            best, best_ratio, best_cost, best_kind = "click", gain_cps / up_cost, up_cost, "click"

        # лучшее доступное здание по cps на печеньку
        for key, b in cfg.FARM_BUILDINGS.items():
            if state["level"] < b["req_level"]:
                continue
            cost = cfg.building_cost(key, state["buildings"].get(key, 0))
            ratio = b["cps"] / cost
            if state["cookies"] >= cost and ratio > best_ratio:
                best, best_ratio, best_cost, best_kind = key, ratio, cost, "building"

        if best_kind == "click":
            state["cookies"] -= best_cost
            state["click_level"] += 1
            spent_something = True
            continue
        if best_kind == "building":
            state["cookies"] -= best_cost
            state["buildings"][best] = state["buildings"].get(best, 0) + 1
            spent_something = True
            continue
        # доска: сначала СЛИВАЕМ (именно заполненная доска и требует слияния —
        # иначе стенд замирал с полной доской неслитых предметов), потом
        # покупаем самый высокий тир, который можем себе позволить
        merge_board()
        cells = cfg.merge_cells_unlocked(state["level"], 0)
        if len(state["board"]) < cells:
            direct_cap = max(1, max_item_unlocked() - cfg.SPAWN_DIRECT_GAP)
            for lvl in range(direct_cap, 0, -1):
                cost = cfg.direct_spawn_cost(lvl, len(state["board"]))
                if state["cookies"] >= cost:
                    state["cookies"] -= cost
                    state["board"].append(lvl)
                    claim_record(lvl)
                    merge_board()
                    spent_something = True
                    break


stuck_since = None
worst_stuck = 0
minute = 0
first_wall = None
while minute < SIM_HOURS * 60:
    in_session = (minute % (SESSION_MIN + BREAK_MIN)) < SESSION_MIN
    # ферма и доска капают всегда (кап 3ч перекрывается перерывом 110 мин — ок)
    farm_tick, board_tick = farm_cps() * 60, board_income_ph() / 60
    tick = farm_tick + board_tick
    earned_src["farm"] += farm_tick
    earned_src["board"] += board_tick
    state["cookies"] += tick
    state["earned"] += tick
    if in_session:
        regen = cfg.ENERGY_REGEN_PER_SEC * 60
        state["energy"] = min(cfg.max_energy(state["level"]), state["energy"] + regen)
        clicks = min(CLICK_CPS * 60, state["energy"])
        state["energy"] -= clicks
        gain = clicks * cfg.click_power(state["click_level"], base_income_ph())
        earned_src["click"] += gain
        state["cookies"] += gain
        state["earned"] += gain
        state["xp"] += clicks * 0.5
        before = state["cookies"]
        try_spend(minute)
        xp_level_check(minute)
        # «затык» = не смог ничего купить целую сессию
        if state["cookies"] == before and before > 0:
            if stuck_since is None:
                stuck_since = minute
        else:
            if stuck_since is not None:
                dur = minute - stuck_since
                worst_stuck = max(worst_stuck, dur)
                if first_wall is None and dur >= 15:
                    first_wall = (stuck_since, dur)
                stuck_since = None
    else:
        state["energy"] = min(cfg.max_energy(state["level"]),
                              state["energy"] + cfg.ENERGY_REGEN_PER_SEC * 60)
    minute += 1

print(f"=== Симуляция {SIM_HOURS}ч (сессии {SESSION_MIN} мин каждые ~2ч) ===")
for t, e in log_events:
    print(f"  {t // 60:>3}ч {t % 60:>2}м  {e}")
print(f"\nИтог: уровень {state['level']}, клик-lvl {state['click_level']}, "
      f"заработано {state['earned']:,.0f}")
_src = " | ".join(f"{k} {v / max(1, state['earned']) * 100:.0f}%"
                  for k, v in earned_src.items())
print(f"Источники дохода: {_src}")
print(f"Здания: {state['buildings']}")
print(f"Ферма: {farm_cps():.0f} cps")
print(f"Доска: {sorted(state['board'], reverse=True)[:8]} "
      f"({len(state['board'])} шт), рекорд тира {state['best_item']}, "
      f"доход {board_income_ph():,.0f}/ч")
print(f"Худший затык без покупок: {worst_stuck} мин")
if first_wall:
    print(f"Первая «стена» (>=15 мин без покупок): на {first_wall[0] // 60}ч "
          f"{first_wall[0] % 60}м, длилась {first_wall[1]} мин")

def best_farm_payback_hours():
    """Предельная окупаемость лучшей доступной покупки фермы, в часах.

    Именно с ней конкурирует доска: у фермы цена растёт на 1.22 за штуку,
    поэтому её базовая окупаемость (0.3-1.1ч) ничего не говорит — считать надо
    на текущем количестве зданий."""
    best = None
    for key, b in cfg.FARM_BUILDINGS.items():
        if state["level"] < b["req_level"]:
            continue
        h = cfg.building_cost(key, state["buildings"].get(key, 0)) / (b["cps"] * 3600)
        best = h if best is None else min(best, h)
    return best


def board_payback_hours():
    """Окупаемость следующей покупки на доску, в часах."""
    lvl = max(1, max_item_unlocked() - cfg.SPAWN_DIRECT_GAP)
    inc = cfg.passive_income_per_hour(lvl)
    if inc <= 0:
        return None
    return cfg.direct_spawn_cost(lvl, len(state["board"])) / inc


_farm_h, _board_h = best_farm_payback_hours(), board_payback_hours()
print(f"\nОкупаемость на конец прогона: ферма {_farm_h:.1f}ч, "
      f"доска {_board_h:.1f}ч" if _farm_h and _board_h else "")

# батл-пасс: xp игрока ~= bp_xp (клики+мерджи капают в оба) + квесты ~750/день
bp_total = cfg.bp_total_xp(cfg.BP_MAX_LEVEL)
xp_per_day = state["xp"] / (SIM_HOURS / 24) + 750
bp_days = bp_total / max(1, xp_per_day)
print(f"\nБатл-пасс ({cfg.BP_MAX_LEVEL} ур., всего {bp_total:,.0f} XP): "
      f"~{bp_days:.1f} дней такого темпа (сезон {cfg.SEASON_LENGTH_DAYS} дн.)")

# --- проверки здоровья баланса: упадут, если конфиг разъехался ---
assert bp_days <= cfg.SEASON_LENGTH_DAYS + 1, (
    f"батл-пасс не успевается за сезон: {bp_days:.1f}д > {cfg.SEASON_LENGTH_DAYS}д")
assert state["level"] <= 12, f"прогрессия слишком быстрая: lvl {state['level']} за {SIM_HOURS}ч"
# Инфляция. Абсолютного потолка тут больше нет и быть не может: цены теперь
# сами привязаны к доходу (окупаемость предмета в часах, цена клика в часах
# дохода), поэтому «миллиард печенек» ничего не означает — значение имеет только
# отношение накопленного к текущему темпу. У здоровой экспоненты итог = десятки
# часов терминального дохода (сейчас ~30ч): бОльшая часть заработана под конец.
# Если отношение улетает — значит ранний доход был не по кривой, это и есть
# гиперинфляция.
_final_ph = farm_cps() * 3600 + board_income_ph()
_hours_of_income = state["earned"] / max(1.0, _final_ph)
print(f"Накоплено = {_hours_of_income:.0f}ч терминального дохода")
assert _hours_of_income < 60, (
    f"гиперинфляция: накоплено {_hours_of_income:.0f}ч дохода — ранний заработок "
    f"обгоняет кривую цен")
# доска обязана оставаться живой веткой: если её окупаемость сильно хуже фермы,
# мердж превращается в декорацию (обратная сторона правки «мердж слишком дёшев»)
assert _board_h <= _farm_h * 3, (
    f"доска мертва: окупаемость {_board_h:.1f}ч против фермы {_farm_h:.1f}ч")
# ветка клика обязана оставаться живой: жадный игрок качал её только до 12,
# потому что сила была линейной при экспоненциальной цене
assert state["click_level"] >= 15, (
    f"ветка клика снова мертва: жадная стратегия бросила её на {state['click_level']}")
# и не должна разгонять инфляцию: без потолка по уровню она давала 77 млрд
assert state["click_level"] <= cfg.click_max_level(state["level"]), "потолок клика не работает"
print("assertions: OK")
