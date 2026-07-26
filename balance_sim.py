"""Симуляция прогрессии игрока по конфигу — без БД и сервера.

Модель активного игрока: кликает с реальным CPS ~5, тратит печеньки жадно
(клик-апгрейд -> здания фермы -> доска), сессии по 10 минут с перерывами.
Смотрим: когда первый «затык» (нечего купить > N минут), когда 3-5 уровень,
сколько занимает батл-пасс.
"""
from server import game_config as cfg

CLICK_CPS = 5.0          # реалистичный темп тапов
SESSION_MIN = 10         # длина сессии
BREAK_MIN = 110          # перерыв между сессиями (6 сессий ~ каждые 2 часа)
SIM_HOURS = 72

state = {
    "cookies": 0.0, "earned": 0.0, "xp": 0.0, "level": 1, "click_level": 1,
    "energy": float(cfg.max_energy(1)), "buildings": {}, "board_items": 0, "board_max": 0,
}
log_events = []


def xp_level_check(t_min):
    while state["level"] < cfg.MAX_LEVEL and \
            state["xp"] >= cfg.xp_for_level(state["level"] + 1):
        state["level"] += 1
        state["cookies"] += cfg.level_reward(state["level"])["cookies"]
        log_events.append((t_min, f"LEVEL {state['level']}"))


def farm_cps():
    return sum(cfg.FARM_BUILDINGS[k]["cps"] * v for k, v in state["buildings"].items())


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
        up_cost = cfg.click_upgrade_cost(state["click_level"])
        gain_cps = (cfg.click_power(state["click_level"] + 1)
                    - cfg.click_power(state["click_level"])) * CLICK_CPS
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
        # доска: спавним и «мерджим» — грубо, каждые 2 спавна = 1 мердж
        sc = cfg.spawn_cost(state["board_items"])
        if state["board_items"] < 20 and state["cookies"] >= sc:
            state["cookies"] -= sc
            state["board_items"] += 1
            if state["board_items"] % 2 == 0:
                lvl = min(2 + state["board_items"] // 4, state["level"] + 2)
                state["xp"] += cfg.merge_reward_xp(min(lvl, 6))
                state["board_max"] = max(state["board_max"], lvl)
            spent_something = True


stuck_since = None
worst_stuck = 0
minute = 0
first_wall = None
while minute < SIM_HOURS * 60:
    in_session = (minute % (SESSION_MIN + BREAK_MIN)) < SESSION_MIN
    # ферма капает всегда (кап 3ч перекрывается перерывом 110 мин — ок)
    state["cookies"] += farm_cps() * 60
    state["earned"] += farm_cps() * 60
    if in_session:
        regen = cfg.ENERGY_REGEN_PER_SEC * 60
        state["energy"] = min(cfg.max_energy(state["level"]), state["energy"] + regen)
        clicks = min(CLICK_CPS * 60, state["energy"])
        state["energy"] -= clicks
        gain = clicks * cfg.click_power(state["click_level"])
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
for t, e in log_events[:12]:
    print(f"  {t // 60:>2}ч {t % 60:>2}м  {e}")
print(f"\nИтог: уровень {state['level']}, клик-lvl {state['click_level']}, "
      f"заработано {state['earned']:,.0f}")
print(f"Здания: {state['buildings']}")
print(f"Ферма: {farm_cps():.0f} cps")
print(f"Худший затык без покупок: {worst_stuck} мин")
if first_wall:
    print(f"Первая «стена» (>=15 мин без покупок): на {first_wall[0] // 60}ч "
          f"{first_wall[0] % 60}м, длилась {first_wall[1]} мин")

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
assert state["earned"] < 1e9, f"гиперинфляция: {state['earned']:,.0f} за {SIM_HOURS}ч"
# ветка клика обязана оставаться живой: жадный игрок качал её только до 12,
# потому что сила была линейной при экспоненциальной цене
assert state["click_level"] >= 15, (
    f"ветка клика снова мертва: жадная стратегия бросила её на {state['click_level']}")
# и не должна разгонять инфляцию: без потолка по уровню она давала 77 млрд
assert state["click_level"] <= cfg.click_max_level(state["level"]), "потолок клика не работает"
print("assertions: OK")
