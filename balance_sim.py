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
    "energy": 500.0, "buildings": {}, "board_items": 0, "board_max": 0,
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
    """Жадная стратегия: клик-апгрейд если окупается, иначе лучшее здание по cps/цене."""
    spent_something = True
    while spent_something:
        spent_something = False
        # клик-апгрейд — первые уровни очень выгодны
        up_cost = cfg.click_upgrade_cost(state["click_level"])
        if state["click_level"] < 12 and state["cookies"] >= up_cost:
            state["cookies"] -= up_cost
            state["click_level"] += 1
            spent_something = True
            continue
        # лучшее доступное здание по cps на печеньку
        best, best_ratio = None, 0
        for key, b in cfg.FARM_BUILDINGS.items():
            if state["level"] < b["req_level"]:
                continue
            cost = cfg.building_cost(key, state["buildings"].get(key, 0))
            ratio = b["cps"] / cost
            if state["cookies"] >= cost and ratio > best_ratio:
                best, best_ratio, best_cost = key, ratio, cost
        if best:
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
bp_days = (cfg.BP_MAX_LEVEL * cfg.BP_XP_PER_LEVEL) / max(1, state["xp"] / (SIM_HOURS / 24) + 750)
print(f"\nБатл-пасс (30 ур. x {cfg.BP_XP_PER_LEVEL} XP): ~{bp_days:.1f} дней "
      f"такого темпа (сезон {cfg.SEASON_LENGTH_DAYS} дн.)")
