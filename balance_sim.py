"""Симуляция прогрессии игрока по конфигу — без БД и сервера.

Стенд отвечает на два разных вопроса, и их нельзя смешивать:

  ИНВАРИАНТЫ — то, что обязано выполняться при любой настройке. Их нарушение
  это баг конфига: доход обгоняет цены, ветка клика мертва, доска мертва,
  потолок клика не работает. Проверяются assert'ом и роняют прогон.

  ЦЕЛЕВЫЕ КРИВЫЕ — то, к чему игру ТЮНИНГУЮТ: сколько уровней у какой когорты
  на D0/D1/D3/D7/D14/D30. Промах по кривой — это не баг, а незакрытая задача
  баланса, поэтому он печатается таблицей, а не роняет прогон. Уронить можно
  флагом --strict (для CI, когда кривые будут сведены).

Запуск:
    python balance_sim.py                 # casual, 72ч
    python balance_sim.py 336             # casual, 14 дней
    python balance_sim.py 336 active      # конкретный профиль
    python balance_sim.py 720 all         # все профили, месяц
    python balance_sim.py 336 all --strict  # промах по кривой = ошибка
"""
import sys

from server import game_config as cfg

# Оффлайн-кап у фермы и у доски одинаковый (FARM_OFFLINE_CAP_HOURS ==
# PASSIVE_CAP_HOURS == 3). Раньше стенд его вообще не моделировал и капал доход
# каждую минуту вечно: для casual с перерывом 110 мин это случайно совпадало с
# правдой, но профиль «возвращенец» с паузой в четверо суток такая модель
# завышала в десятки раз.
OFFLINE_CAP_H = min(cfg.FARM_OFFLINE_CAP_HOURS, cfg.PASSIVE_CAP_HOURS)

# ---------------------------------------------------------------------------
# Профили игроков
# ---------------------------------------------------------------------------
# cps      — реальный темп тапов в секунду
# session  — длина захода в минутах
# pause    — перерыв между заходами в минутах
# cycle    — (дней играет, дней не заходит) для возвращенца
# offline_bonus_h / click_mult / head_start — эффекты покупок за Stars
PROFILES = {
    "newbie": dict(
        label="новичок: 5 мин дважды в день, тапает неуверенно",
        cps=3.0, session=5, pause=715),
    "casual": dict(
        label="casual: 10 мин раз в 2 часа",
        cps=5.0, session=10, pause=110),
    "active": dict(
        label="активный: 15 мин раз в час",
        cps=8.0, session=15, pause=45),
    "payer": dict(
        label="платящий: casual + оффлайн-кап 12ч, буст x2, ящик печенек",
        cps=5.0, session=10, pause=110,
        offline_bonus_h=9.0,          # offline_cap_12h: +9ч к базовым 3ч
        click_mult=cfg.BOOST_CLICK_X2_MULT,
        head_start_income_h=10.0),    # cookies_crate: 10 часов дохода сразу
    "returning": dict(
        label="возвращенец: 3 дня играет, 4 дня нет",
        cps=5.0, session=10, pause=110, cycle=(3, 4)),
    "exploiter": dict(
        label="абузер: тапает на серверном потолке CPS круглосуточно",
        cps=float(cfg.MAX_CPS), session=60, pause=0),
}

# ---------------------------------------------------------------------------
# Целевые кривые: (часы, уровень_минимум, уровень_максимум)
# ---------------------------------------------------------------------------
# Нижняя граница так же важна, как верхняя: слишком медленно — игрок уходит,
# слишком быстро — кончается контент. Пусто = для этого профиля на этом
# горизонте цель не задана.
#
# Опорные требования продукта:
#   - потолок уровня НЕ должен браться за один сезон (14 дней) даже активным;
#   - батл-пасс занимает 60-80% сезона у целевой активной когорты;
#   - на первом получасе игрок обязан увидеть прогресс (D0 >= 2 уровня).
HORIZONS = (0.5, 24, 72, 168, 336, 720)
TARGET_LEVELS = {
    "newbie":    {0.5: (2, 3), 24: (3, 5),  72: (5, 8),   168: (8, 12),  336: (12, 17), 720: (18, 25)},
    "casual":    {0.5: (2, 3), 24: (4, 7),  72: (8, 10),  168: (13, 16), 336: (18, 22), 720: (26, 30)},
    "active":    {0.5: (2, 4), 24: (6, 9),  72: (10, 13), 168: (16, 20), 336: (22, 26), 720: (29, 30)},
    "payer":     {0.5: (2, 4), 24: (5, 8),  72: (9, 12),  168: (14, 18), 336: (20, 24), 720: (27, 30)},
    "returning": {0.5: (2, 3), 24: (4, 7),  72: (8, 10),  168: (11, 15), 336: (15, 20), 720: (22, 28)},
    # Абузер — не цель тюнинга, а граница: насколько он обгоняет активного.
    # Проверяется отдельно, не по этой таблице.
    "exploiter": {},
}
# Во сколько раз абузеру позволено обгонять честного активного игрока.
EXPLOIT_MAX_ADVANTAGE = 2.5


def simulate(name: str, sim_hours: float) -> dict:
    """Прогоняет один профиль и возвращает итоговое состояние."""
    prof = PROFILES[name]
    cps = prof["cps"]
    session_min, pause_min = prof["session"], prof["pause"]
    cycle = prof.get("cycle")
    offline_cap_h = OFFLINE_CAP_H + prof.get("offline_bonus_h", 0.0)
    click_mult = prof.get("click_mult", 1.0)

    st = {
        "cookies": 0.0, "earned": 0.0, "xp": 0.0, "level": 1, "click_level": 1,
        "energy": float(cfg.max_energy(1)), "buildings": {},
        # доска моделируется поимённо: именно на ней держится экономика, и
        # счётчиком «сколько предметов» её проверить нельзя
        "board": [], "best_item": 0,
        "maxed_at_h": None, "earned_at_max": None, "online_at_max": None,
    }
    earned_src = {"farm": 0.0, "board": 0.0, "click": 0.0}
    click_xp_day = [-1, 0.0]      # [номер суток, кликов за эти сутки]
    daily_done = [-1]             # сутки, за которые уже забран дневной контент
    log_events = []
    level_at = {}          # уровень -> минута, когда взят
    earned_at_level = {}   # уровень -> сколько всего заработано к этому моменту

    def farm_cps():
        return sum(cfg.FARM_BUILDINGS[k]["cps"] * v for k, v in st["buildings"].items())

    def board_income_ph():
        # премия за рекорд тира — то, ради чего вообще имеет смысл сливать
        return (sum(cfg.passive_income_per_hour(l) for l in st["board"])
                * cfg.record_multiplier(st["best_item"]))

    def base_income_ph():
        """Доход без кликов — от него считается сила клика (как в game_logic)."""
        return farm_cps() * 3600 + board_income_ph()

    def max_item_unlocked():
        return max((l for l in range(1, cfg.MAX_ITEM_LEVEL + 1)
                    if cfg.item_unlock_level(l) <= st["level"]), default=1)

    def xp_level_check(t_min):
        while st["level"] < cfg.MAX_LEVEL and \
                st["xp"] >= cfg.xp_for_level(st["level"] + 1):
            st["level"] += 1
            st["cookies"] += cfg.level_reward(st["level"])["cookies"]
            level_at[st["level"]] = t_min
            earned_at_level[st["level"]] = st["earned"]
            log_events.append((t_min, f"LEVEL {st['level']}"))
            if st["level"] >= cfg.MAX_LEVEL:
                # Контент кончился. Снимаем слепок: всё, что накопится дальше,
                # к балансу прогрессии отношения не имеет — покупать уже нечего.
                st["maxed_at_h"] = t_min / 60.0
                st["earned_at_max"] = st["earned"]
                st["online_at_max"] = online_min

    def claim_record(new_level):
        """XP за личный рекорд тира — основной источник уровней."""
        if new_level <= st["best_item"]:
            return
        for l in range(max(st["best_item"] + 1, 2), new_level + 1):
            st["xp"] += cfg.first_item_xp(l)
        st["best_item"] = new_level

    def merge_board():
        """Жадно сливает всё, что сливается, не выше открытого тира."""
        cap = max_item_unlocked()
        merged = True
        while merged:
            merged = False
            for lvl in sorted(set(st["board"])):
                if lvl + 1 > cap or st["board"].count(lvl) < 2:
                    continue
                st["board"].remove(lvl)
                st["board"].remove(lvl)
                st["board"].append(lvl + 1)
                st["xp"] += cfg.merge_reward_xp(lvl + 1)
                claim_record(lvl + 1)
                merged = True
                break

    def try_spend():
        """Жадная стратегия: покупаем то, что даёт больше всего cps на печеньку.

        Клик и здания сравниваются ОДНОЙ метрикой. Раньше тут стоял костыль
        `click_level < 12`: сила клика была линейной при экспоненциальной цене,
        и качать её дальше 12 не имело смысла ни при каких числах. Теперь ветка
        клика живая, и симуляция обязана выбирать честно — иначе она врёт."""
        spent_something = True
        while spent_something:
            spent_something = False
            best, best_ratio, best_cost, best_kind = None, 0.0, 0.0, None
            # Сливаем ДО оценки покупок: слияние освобождает клетку, и без него
            # доска выглядела бы полной, а покупка на неё — невозможной.
            merge_board()

            # клик: прирост дохода считаем по реальному темпу тапа в сессии
            base = base_income_ph()
            up_cost = cfg.click_upgrade_cost(st["click_level"], base)
            gain_cps = (cfg.click_power(st["click_level"] + 1, base)
                        - cfg.click_power(st["click_level"], base)) * cps * click_mult
            # клик работает только пока игрок в сессии — режем долей активного времени
            gain_cps *= session_min / max(1, session_min + pause_min)
            if st["click_level"] >= cfg.click_max_level(st["level"]):
                up_cost, gain_cps = float("inf"), 0.0   # упёрлись в потолок уровня
            if st["cookies"] >= up_cost and gain_cps / up_cost > best_ratio:
                best, best_ratio, best_cost, best_kind = "click", gain_cps / up_cost, up_cost, "click"

            # лучшее доступное здание по cps на печеньку
            for key, b in cfg.FARM_BUILDINGS.items():
                if st["level"] < b["req_level"]:
                    continue
                if st["best_item"] < b.get("req_record", 0):
                    continue      # ферма выше середины требует прогресса доски
                owned = st["buildings"].get(key, 0)
                if owned >= cfg.FARM_MAX_COPIES:
                    continue      # здание упёрлось в потолок копий
                cost = cfg.building_cost(key, owned)
                ratio = b["cps"] / cost
                if st["cookies"] >= cost and ratio > best_ratio:
                    best, best_ratio, best_cost, best_kind = key, ratio, cost, "building"

            # Доска сравнивается ТОЙ ЖЕ метрикой, что ферма и клик: прирост
            # дохода на печеньку. Раньше она стояла в ветке «если денег не
            # хватило больше ни на что», то есть стенд по построению не мог
            # показать её долю выше остатков — а именно долю доски мы и меряем.
            cells = cfg.merge_cells_unlocked(st["level"], 0)
            if len(st["board"]) < cells:
                direct_cap = max(1, max_item_unlocked() - cfg.SPAWN_DIRECT_GAP)
                for lvl in range(direct_cap, 0, -1):
                    cost = cfg.direct_spawn_cost(lvl, len(st["board"]),
                                                 record=st["best_item"])
                    gain_cps = (cfg.passive_income_per_hour(lvl)
                                * cfg.record_multiplier(st["best_item"]) / 3600.0)
                    if st["cookies"] >= cost and gain_cps / cost > best_ratio:
                        best, best_ratio = lvl, gain_cps / cost
                        best_cost, best_kind = cost, "board"
                        break        # ниже тир — хуже отношение, дальше не смотрим

            if best_kind == "click":
                st["cookies"] -= best_cost
                st["click_level"] += 1
                spent_something = True
                continue
            if best_kind == "building":
                st["cookies"] -= best_cost
                st["buildings"][best] = st["buildings"].get(best, 0) + 1
                spent_something = True
                continue
            if best_kind == "board":
                st["cookies"] -= best_cost
                st["board"].append(best)
                claim_record(best)
                merge_board()
                spent_something = True
                continue

    def online_at(minute: int) -> bool:
        """Игрок в приложении? Учитывает и суточный цикл возвращенца."""
        if cycle:
            play_d, away_d = cycle
            day = (minute // 1440) % (play_d + away_d)
            if day >= play_d:
                return False
        if pause_min <= 0:
            return True
        return (minute % (session_min + pause_min)) < session_min

    # ---- разгон покупателя: ящик печенек даёт часы дохода, но доход в начале
    # игры нулевой, поэтому применяем его на первом же заходе как min_amount ----
    head_start_h = prof.get("head_start_income_h", 0.0)
    head_start_done = not head_start_h

    # Затык меряется в МИНУТАХ В ИГРЕ, а не по календарю. Прошлая версия стенда
    # считала по календарю и отрапортовала «первая стена на 6ч, длилась 111 мин»
    # — из которых 110 игрок просто не открывал приложение. Пугающее число,
    # означающее ровно ничего. Игроку больно только то время, которое он смотрит
    # на экран и не может ничего купить.
    stuck_since, worst_stuck, first_wall = None, 0, None
    online_min = 0
    last_online = 0
    minute = 0
    # Снимок накопленного по МИНУТАМ В ИГРЕ. По календарю мерить нельзя дважды:
    # во-первых, вывести терминальный доход из farm+board не выйдет — у активных
    # 68% дохода даёт клик, которого в base_income_ph нет по построению (иначе
    # рекурсия). Во-вторых, у возвращенца последняя пятая часть календаря
    # приходится на дни отсутствия, и «форма кривой» выходила нулевой. Время в
    # игре убирает расписание из метрики целиком.
    earned_by_online = []
    total_min = int(sim_hours * 60)
    while minute < total_min:
        in_session = online_at(minute)
        if in_session:
            # Сбор оффлайн-дохода: сервер начисляет за прошедший интервал,
            # обрезанный капом, — а не за всё время отсутствия.
            gap_h = min((minute - last_online) / 60.0, offline_cap_h)
            if gap_h > 0:
                farm_gain = farm_cps() * 3600 * gap_h
                board_gain = board_income_ph() * gap_h
                earned_src["farm"] += farm_gain
                earned_src["board"] += board_gain
                st["cookies"] += farm_gain + board_gain
                st["earned"] += farm_gain + board_gain
            last_online = minute
            online_min += 1
            earned_by_online.append(st["earned"])

            # Дневной контент: три задания и пять заказов. Моделируем как
            # выполненные за первый заход суток — цели у них мелкие (80-500
            # кликов, 5-15 мерджей), пятиминутной сессии хватает.
            #
            # Награда печеньками намеренно НЕ моделируется: она считается в
            # часах текущего дохода и у прокачанного игрока это заметная сумма,
            # но здесь мы мерим кривую УРОВНЕЙ, и подмешивать в неё ещё один
            # источник дохода значит смешать два вопроса в одном числе.
            day_now = int(minute // 1440)
            if day_now != daily_done[0]:
                # «отдых»: пропущенные дни дневного контента нельзя доиграть,
                # без этой ручки возвращенца не вытянуть ничем общим
                rest = cfg.rest_mult(day_now - daily_done[0]) if daily_done[0] >= 0 else 1.0
                daily_done[0] = day_now
                st["xp"] += cfg.DAILY_QUESTS_PER_DAY * cfg.quest_reward_xp(st["level"]) * rest
                st["xp"] += cfg.ORDERS_PER_DAY * cfg.order_reward_xp(2, st["level"]) * rest

            if not head_start_done:
                boost = max(25_000.0, base_income_ph() * head_start_h)
                st["cookies"] += boost
                st["earned"] += boost
                earned_src["farm"] += boost   # покупка, но учитываем как приход
                head_start_done = True

            regen = cfg.ENERGY_REGEN_PER_SEC * 60
            st["energy"] = min(cfg.max_energy(st["level"]), st["energy"] + regen)
            clicks = min(cps * 60, st["energy"])
            st["energy"] -= clicks
            # Комбо: держишь >= COMBO_MIN_CPS — множитель дорастает до максимума
            # примерно за 15 секунд, а сессии здесь минимум пятиминутные, поэтому
            # держим его на потолке. Раньше стенд комбо не моделировал вовсе и
            # занижал доход с кликов вдвое.
            combo = cfg.COMBO_MAX_MULT if cps >= cfg.COMBO_MIN_CPS else 1.0
            gain = clicks * cfg.click_power(st["click_level"], base_income_ph()) \
                * combo * click_mult
            earned_src["click"] += gain
            st["cookies"] += gain
            st["earned"] += gain
            # XP за клики — с дневным софт-капом, как в game_logic. Плоские 0.5
            # за клик завышали именно тех, кого кап и придуман сдерживать:
            # casual нащёлкивает 36 000 кликов в сутки, то есть 18 000 XP вместо
            # 8 250. Стенд показывал 30 уровень там, где игра даёт 22.
            day = int(minute // 1440)
            if day != click_xp_day[0]:
                click_xp_day[0], click_xp_day[1] = day, 0.0
            before_cap = max(0.0, cfg.CLICK_XP_SOFT_CAP - click_xp_day[1])
            cheap = min(clicks, before_cap)
            st["xp"] += cheap * cfg.CLICK_XP_RATE \
                + (clicks - cheap) * cfg.CLICK_XP_RATE_CAPPED
            click_xp_day[1] += clicks

            before = st["cookies"]
            try_spend()
            xp_level_check(minute)
            # «затык» = не смог ничего купить целую сессию
            if st["cookies"] == before and before > 0:
                if stuck_since is None:
                    stuck_since = (online_min, minute)
            else:
                if stuck_since is not None:
                    dur = online_min - stuck_since[0]
                    worst_stuck = max(worst_stuck, dur)
                    if first_wall is None and dur >= 15:
                        first_wall = (stuck_since[1], dur)
                    stuck_since = None
        else:
            st["energy"] = min(cfg.max_energy(st["level"]),
                               st["energy"] + cfg.ENERGY_REGEN_PER_SEC * 60)
        minute += 1

    def best_farm_payback_hours():
        """Предельная окупаемость лучшей доступной покупки фермы, в часах.

        Именно с ней конкурирует доска: у фермы цена растёт на 1.22 за штуку,
        поэтому её базовая окупаемость (0.3-1.1ч) ничего не говорит — считать
        надо на текущем количестве зданий."""
        best = None
        for key, b in cfg.FARM_BUILDINGS.items():
            if st["level"] < b["req_level"] or st["best_item"] < b.get("req_record", 0):
                continue
            owned = st["buildings"].get(key, 0)
            if owned >= cfg.FARM_MAX_COPIES:
                continue
            h = cfg.building_cost(key, owned) / (b["cps"] * 3600)
            best = h if best is None else min(best, h)
        return best

    def board_payback_hours():
        """Окупаемость следующей покупки на доску, в часах."""
        lvl = max(1, max_item_unlocked() - cfg.SPAWN_DIRECT_GAP)
        inc = cfg.passive_income_per_hour(lvl) * cfg.record_multiplier(st["best_item"])
        if inc <= 0:
            return None
        return cfg.direct_spawn_cost(lvl, len(st["board"]),
                                     record=st["best_item"]) / inc

    # Форма кривой заработка: какая доля всего заработка ушла на ПОСЛЕДНЮЮ ПЯТУЮ
    # ЧАСТЬ ВЗЯТЫХ УРОВНЕЙ. Ось — прогресс, а не время, и это принципиально:
    # по календарю метрику ломает расписание (у возвращенца последние дни —
    # дни отсутствия), по времени в игре — оффлайн-доход (новичок и возвращенец
    # зарабатывают в основном пока НЕ играют: 2ч и 12ч в игре за две недели).
    # Уровни свободны и от того, и от другого: вопрос «обгоняет ли доход кривую
    # цен» — это вопрос о том, сколько стоит продвижение, а не сколько прошло
    # часов.
    st["live_online_h"] = online_min / 60.0
    st["earned_live"] = st["earned_at_max"] if st["maxed_at_h"] is not None else st["earned"]
    top = st["level"]
    # Проверка про форму ВСЕЙ кривой, поэтому нужно пройти хотя бы 40% уровней.
    # На коротком прогоне «последняя пятая часть» — это один-два уровня, и
    # метрика меряет шум: на 0.5ч выходило «уровни 3-3 стоили 24%», на 6ч —
    # «уровни 7-8 стоили 13%». Ни то, ни другое не говорит ничего о кривой цен.
    if top >= 12:
        cut = max(2, int(round(1 + 0.8 * (top - 1))))
        base_earned = earned_at_level.get(cut, 0.0)
        st["tail_share"] = (st["earned_live"] - base_earned) / max(1.0, st["earned_live"])
        st["tail_from_level"] = cut
    else:
        st["tail_share"] = None    # прогресса не было — мерить нечего
        st["tail_from_level"] = None

    st.update({
        "profile": name, "hours": sim_hours, "src": earned_src, "events": log_events,
        "level_at": level_at, "worst_stuck": worst_stuck, "first_wall": first_wall,
        "farm_cps": farm_cps(), "board_ph": board_income_ph(),
        "final_ph": base_income_ph(),
        "farm_payback_h": best_farm_payback_hours(),
        "board_payback_h": board_payback_hours(),
    })
    return st


# ---------------------------------------------------------------------------
# Отчёт
# ---------------------------------------------------------------------------
def report(st: dict, verbose: bool):
    name, hours = st["profile"], st["hours"]
    print(f"\n=== {name} — {PROFILES[name]['label']} ({hours:g}ч) ===")
    if verbose:
        for t, e in st["events"]:
            print(f"  {t // 60:>4}ч {t % 60:>2}м  {e}")
    src = " | ".join(f"{k} {v / max(1.0, st['earned']) * 100:.0f}%"
                     for k, v in st["src"].items())
    print(f"  уровень {st['level']}, клик-lvl {st['click_level']}, "
          f"заработано {st['earned']:,.0f}")
    if st["tail_share"] is not None:
        print(f"  форма кривой: уровни {st['tail_from_level']}-{st['level']} стоили "
              f"{st['tail_share'] * 100:.0f}% всего заработка (норма >50%)")
    print(f"  время в игре: {st['live_online_h']:.0f}ч за {hours:g}ч календаря")
    print(f"  источники: {src}")
    print(f"  ферма {st['farm_cps']:,.0f} cps | доска {st['board_ph']:,.0f}/ч "
          f"= {board_share(st) * 100:.1f}% дохода (рекорд тира {st['best_item']})")
    if st["maxed_at_h"] is not None:
        print(f"  ПОТОЛОК {cfg.MAX_LEVEL} взят на {st['maxed_at_h'] / 24:.1f}-й день "
              f"— дальше {(hours - st['maxed_at_h']) / 24:.1f} дн. без цели")
    if st["farm_payback_h"] and st["board_payback_h"]:
        print(f"  окупаемость: ферма {st['farm_payback_h']:.1f}ч, "
              f"доска {st['board_payback_h']:.1f}ч")
    print(f"  худший затык без покупок: {st['worst_stuck']} мин В ИГРЕ")
    if st["first_wall"]:
        print(f"  первая «стена» (>=15 мин в игре): началась на {st['first_wall'][0] // 60}ч "
              f"{st['first_wall'][0] % 60}м, длилась {st['first_wall'][1]} мин в игре")


def check_invariants(st: dict) -> list:
    """Нарушения того, что обязано выполняться при любой настройке."""
    bad = []
    name = st["profile"]

    # Инфляция. Абсолютного потолка тут быть не может: цены сами привязаны к
    # доходу (окупаемость предмета в часах, цена клика в часах дохода), поэтому
    # «миллиард печенек» ничего не означает — значение имеет только отношение
    # накопленного к текущему темпу. У здоровой экспоненты итог = десятки часов
    # терминального дохода: бОльшая часть заработана под конец. Если отношение
    # улетает — ранний доход шёл не по кривой, это и есть гиперинфляция.
    #
    # Считаем ТОЛЬКО живую фазу — до потолка уровня. После него покупать нечего,
    # игрок копит впустую, и отношение растёт линейно с длиной хвоста: у active
    # на 336ч выходило 562ч «инфляции», хотя это просто неделя игры без цели.
    # Мерить надо прогрессию, а не простой.
    if st["tail_share"] is not None and st["tail_share"] < 0.5:
        bad.append(f"[{name}] гиперинфляция: последняя пятая часть уровней "
                   f"(с {st['tail_from_level']} по {st['level']}) стоила лишь "
                   f"{st['tail_share'] * 100:.0f}% заработка — ранний доход обгоняет "
                   f"кривую цен")

    # Ветка клика обязана оставаться живой в поздней игре: раньше сила клика
    # была линейной при экспоненциальной цене, и жадный игрок бросал её на 12.
    # Сравнивать надо с ПОТОЛКОМ (click_max_level от уровня игрока), а не с
    # абсолютным числом: у новичка клик 6 — это не мёртвая ветка, а упор в
    # его 4-й уровень. Порог «с 12-го уровня» тоже не с потолка: до него жадная
    # стратегия честно предпочитает ферму (доля 0.38-0.75), а с 12-го держит клик
    # на 0.96-1.00 от разрешённого. Ровно на 10-м мерить нельзя — потолок растёт
    # вместе с уровнем, и сразу после левелапа доля проваливается на один тик.
    #
    # Спрашиваем это только у того, кто вообще тапает. Симуляция режет прирост
    # от клика долей времени в сессии (см. gain_cps *= session/(session+pause)),
    # и у профиля, который проводит в игре меньше 5% календаря, клик по
    # построению стоит в двадцать раз меньше номинала — предпочесть ему ферму
    # это не мёртвая ветка, а верный ответ. Ловим мы обратное: игрок тапает
    # часами, а качать клик всё равно невыгодно.
    cap = cfg.click_max_level(st["level"])
    taps_enough = st["live_online_h"] >= 0.05 * st["hours"]
    if taps_enough and st["level"] >= 12 and st["click_level"] < cap * 0.9:
        bad.append(f"[{name}] ветка клика мертва: {st['click_level']} при "
                   f"разрешённых {cap} на {st['level']}-м уровне")

    if st["click_level"] > cap:
        bad.append(f"[{name}] потолок клика не работает: {st['click_level']} > {cap}")

    return bad


def board_share(st: dict) -> float:
    """Доля доски в терминальном доходе."""
    return st["board_ph"] / max(1.0, st["final_ph"])


def check_targets(st: dict) -> list:
    """Промахи по целевой кривой. Не баг — незакрытая задача баланса."""
    name, hours = st["profile"], st["hours"]
    out = []

    band = (TARGET_LEVELS.get(name) or {}).get(hours)
    if band:
        lo, hi = band
        lvl = st["level"]
        if lvl < lo:
            out.append(f"[{name}] {hours:g}ч: уровень {lvl}, цель {lo}-{hi} — СЛИШКОМ МЕДЛЕННО")
        elif lvl > hi:
            out.append(f"[{name}] {hours:g}ч: уровень {lvl}, цель {lo}-{hi} — СЛИШКОМ БЫСТРО")

    # Потолок уровня не должен браться за один сезон — иначе оставшиеся дни
    # сезона игроку нечего делать, а это и есть отток.
    season_h = cfg.SEASON_LENGTH_DAYS * 24
    if st["maxed_at_h"] is not None and st["maxed_at_h"] <= season_h:
        out.append(f"[{name}] потолок {cfg.MAX_LEVEL} взят за {st['maxed_at_h'] / 24:.1f} дн. "
                   f"— раньше конца сезона ({cfg.SEASON_LENGTH_DAYS} дн.)")

    # Доска обязана быть веткой дохода, а не декорацией. Проверять окупаемость
    # предмета бессмысленно: она в порядке (ITEM_PAYBACK_HOURS), но доска
    # ограничена 25 клетками, а ферма не ограничена ничем — поэтому её вклад и
    # вырождается. Мерим то, что важно на самом деле: долю в доходе.
    share = board_share(st)
    if share < 0.10:
        out.append(f"[{name}] доска даёт {share * 100:.1f}% дохода — мердж стал "
                   f"декорацией (потолок 25 клеток против безлимитной фермы)")
    return out


def battle_pass_days(st: dict) -> float:
    """За сколько дней такого темпа закрывается пасс (+ ~750 XP/день с квестов)."""
    xp_per_day = st["xp"] / max(1e-9, st["hours"] / 24) + 750
    return cfg.bp_total_xp(cfg.BP_MAX_LEVEL) / max(1.0, xp_per_day)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    strict = "--strict" in sys.argv
    hours = float(args[0]) if args else 72.0
    which = args[1] if len(args) > 1 else "casual"
    names = list(PROFILES) if which == "all" else [which]
    if any(n not in PROFILES for n in names):
        sys.exit(f"неизвестный профиль; доступны: {', '.join(PROFILES)}, all")

    runs = {}
    for n in names:
        st = simulate(n, hours)
        runs[n] = st
        report(st, verbose=(len(names) == 1))

    problems, misses = [], []
    for st in runs.values():
        problems += check_invariants(st)
        misses += check_targets(st)

    # Батл-пасс: цель 60-80% сезона у активной когорты.
    #
    # Считаем ВСЕГДА на горизонте сезона, какой бы горизонт ни попросили в
    # аргументах. Темп XP разгоняется вместе с игроком, поэтому средний темп за
    # первые сутки экстраполируется в 17 дней, а за две недели — в 9; вопрос
    # «влезает ли пасс в сезон» имеет ровно один осмысленный горизонт — сам сезон.
    ref_name = "active" if "active" in runs else next(iter(runs))
    season = cfg.SEASON_LENGTH_DAYS
    ref = (runs[ref_name] if runs[ref_name]["hours"] == season * 24
           else simulate(ref_name, season * 24))
    bp_days = battle_pass_days(ref)
    print(f"\nБатл-пасс ({cfg.BP_MAX_LEVEL} ур., {cfg.bp_total_xp(cfg.BP_MAX_LEVEL):,.0f} XP): "
          f"~{bp_days:.1f} дн. темпом «{ref['profile']}» при сезоне {season} дн. "
          f"({bp_days / season * 100:.0f}% сезона, цель 60-80%)")
    if bp_days > season + 1:
        problems.append(f"батл-пасс не успевается за сезон: {bp_days:.1f}д > {season}д")
    elif not 0.6 * season <= bp_days <= 0.8 * season:
        misses.append(f"батл-пасс занимает {bp_days / season * 100:.0f}% сезона, цель 60-80%")

    # Абузер: во сколько раз обгоняет честного активного игрока.
    #
    # Требование про УСТОЙЧИВЫЙ отрыв, поэтому меряем от суток и дальше. На
    # получасовом горизонте отношение задано не экономикой, а расписанием:
    # абузер провёл в игре все 30 минут, активный — свои 15, и стартовый запас
    # энергии обоим налит полным. Никакая ручка энергии этого не убирает, так
    # что порог там ловил бы календарь профиля, а не дыру в балансе.
    if "exploiter" in runs and "active" in runs and hours >= 24:
        adv = runs["exploiter"]["earned"] / max(1.0, runs["active"]["earned"])
        print(f"Абузер обгоняет активного в {adv:.1f} раза "
              f"(предел {EXPLOIT_MAX_ADVANTAGE})")
        if adv > EXPLOIT_MAX_ADVANTAGE:
            problems.append(f"абузер обгоняет активного в {adv:.1f} раза — "
                            f"энергия и CPS-лимит его не держат")

    if misses:
        print("\n--- промахи по целевой кривой (задачи баланса, не баги) ---")
        for m in misses:
            print(f"  MISS {m}")
    if problems:
        print("\n--- НАРУШЕНИЯ ИНВАРИАНТОВ ---")
        for p in problems:
            print(f"  FAIL {p}")
        sys.exit(1)
    if misses and strict:
        sys.exit(f"\n--strict: {len(misses)} промахов по целевой кривой")
    print("\ninvariants: OK" + (f", targets: {len(misses)} MISS" if misses else ", targets: OK"))


if __name__ == "__main__":
    main()
