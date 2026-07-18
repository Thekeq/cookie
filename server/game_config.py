"""Весь игровой баланс в одном месте — крутить числа тут."""

# ---------- Кликер ----------
MAX_CPS = 15                    # серверный потолок кликов/сек (анти-чит)
ENERGY_PER_CLICK = 1
# 1.2/сек: при тапе ~5/сек энергия тает медленно, сессия тапа живёт минуты,
# а полный бак восстанавливается за ~15-20 мин — «зайди ещё разок» вместо часа ожидания
ENERGY_REGEN_PER_SEC = 1.2

def max_energy(user_level: int) -> int:
    return 1000 + (user_level - 1) * 100

def click_power(click_level: int) -> float:
    """Сколько cookies даёт один клик"""
    return click_level * 1.0

def click_upgrade_cost(click_level: int) -> float:
    """Цена прокачки клика с текущего уровня на следующий"""
    return 100 * (1.8 ** (click_level - 1))

# ---------- Merge ----------
BOARD_SIZE = 25                 # 5x5
MAX_ITEM_LEVEL = 12

def spawn_cost(items_on_board: int) -> float:
    """Цена спавна печеньки lvl1, растёт от заполненности доски"""
    return 50 * (1.15 ** items_on_board)

def merge_reward_xp(new_level: int) -> float:
    """XP за создание печеньки уровня new_level"""
    return 10 * (2 ** (new_level - 2))

def passive_income_per_hour(item_level: int) -> float:
    """Пассивный доход cookies/час от печеньки на доске"""
    if item_level < 3:
        return 0
    return 20 * (2.2 ** (item_level - 3))

PASSIVE_CAP_HOURS = 3           # оффлайн-доход копится максимум 3 часа

def item_unlock_level(item_level: int) -> int:
    """С какого уровня игрока доступна печенька item_level (спавн/merge выше — нельзя)"""
    unlocks = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 5, 7: 7, 8: 10, 9: 13, 10: 16, 11: 20, 12: 25}
    return unlocks.get(item_level, 99)

# ---------- Уровни (тропинка) ----------
MAX_LEVEL = 30

def xp_for_level(level: int) -> float:
    """Сколько всего XP нужно, чтобы достичь уровня level"""
    if level <= 1:
        return 0
    return 200 * (level - 1) ** 1.9

def level_reward(level: int) -> dict:
    """Награда за достижение уровня. Растёт быстрее линейного, чтобы поздние
    уровни оставались событием; full_refill — левел-ап заливает энергию до полного."""
    return {"cookies": int(150 * level ** 1.5), "energy_bonus": 50, "full_refill": True}

# ---------- Сезоны ----------
# Всё сезонное (батл-пасс, лидерборд) живёт циклами SEASON_LENGTH_DAYS.
# Номер сезона считается от SEASON_EPOCH, ролловер происходит лениво при первом
# запросе нового сезона (см. game_logic.current_season / ensure_season).
SEASON_EPOCH = 1783900800        # 2026-07-13 00:00 UTC, понедельник
SEASON_LENGTH_DAYS = 14

# Награды топ-10 лидерборда в конце сезона (место -> печеньки)
SEASON_TOP_REWARDS = {
    1: 100_000, 2: 60_000, 3: 40_000,
    4: 25_000, 5: 20_000, 6: 15_000, 7: 12_000, 8: 10_000, 9: 8_000, 10: 6_000,
}

# ---------- Батл-пасс ----------
BP_MAX_LEVEL = 30
# 2500/ур.: активный игрок (~9-10k XP/день) закрывает 30 уровней за ~8 дней
# из 14 дней сезона — премиум есть смысл покупать, но и халявщик успевает
BP_XP_PER_LEVEL = 2500
BP_PREMIUM_STARS = 100          # цена premium-пасса в Stars

def bp_reward(bp_level: int, premium: bool) -> dict:
    if premium:
        return {"cookies": 400 * bp_level, "energy": 100 if bp_level % 5 == 0 else 0}
    return {"cookies": 150 * bp_level, "energy": 0}

# ---------- Рефералка ----------
REF_REWARD_REFERRER = 1000      # награда пригласившему
REF_REWARD_REFERRED = 500       # награда приглашённому

# Milestone-награды за N приглашённых (разовые, забираются в профиле)
# key -> (кол-во друзей, тип награды, значение)
REF_MILESTONES = {
    "refs_boost":   {"count": 3,  "type": "boost",      "hours": 24,       "title_key": "ref_ms_boost"},
    "refs_skin":    {"count": 10, "type": "skin",       "skin": "royal",   "title_key": "ref_ms_skin"},
    "refs_premium": {"count": 25, "type": "bp_premium",                    "title_key": "ref_ms_premium"},
}
# эксклюзивный скин за 10 друзей — нет в магазине (cost None = не продаётся)
REF_EXCLUSIVE_SKIN = {"royal": {"emoji": "👑", "req_level": 1}}

# ---------- Ежедневная награда (стрик) ----------
# День стрика (1..7+) -> печеньки; после 7-го дня цикл держится на day-7 награде
DAILY_REWARDS = {1: 500, 2: 1000, 3: 2000, 4: 3500, 5: 5000, 6: 7500, 7: 15000}

def daily_reward(streak_day: int) -> int:
    return DAILY_REWARDS.get(min(streak_day, 7), DAILY_REWARDS[7])

# ---------- Ежедневные задания ----------
# Пул заданий; каждый день детерминированно выбираются DAILY_QUESTS_PER_DAY штук.
# metric — что копим за день, goal — цель, reward_cookies + reward_bp_xp — награда.
DAILY_QUESTS_PER_DAY = 3
DAILY_QUEST_POOL = {
    "clicks_200":   {"metric": "clicks",    "goal": 200, "reward_cookies": 1500, "reward_bp_xp": 200},
    "clicks_500":   {"metric": "clicks",    "goal": 500, "reward_cookies": 3000, "reward_bp_xp": 350},
    "merges_5":     {"metric": "merges",    "goal": 5,   "reward_cookies": 1200, "reward_bp_xp": 200},
    "merges_15":    {"metric": "merges",    "goal": 15,  "reward_cookies": 3000, "reward_bp_xp": 350},
    "spawn_10":     {"metric": "spawns",    "goal": 10,  "reward_cookies": 1000, "reward_bp_xp": 150},
    "buy_2":        {"metric": "buildings", "goal": 2,   "reward_cookies": 2000, "reward_bp_xp": 250},
    "earn_5k":      {"metric": "earned",    "goal": 5000, "reward_cookies": 2500, "reward_bp_xp": 300},
}

# ---------- Подписка на канал ----------
CHANNEL_REWARD = 2000           # разовая награда за подписку (если CHANNEL_USERNAME задан)

# ---------- Золотая печенька (случайное событие) ----------
# Сервер решает, когда появится; клиент только рисует и репортит тап.
# Появляется раз в GOLDEN_MIN..MAX сек активной игры, живёт GOLDEN_LIFETIME сек.
GOLDEN_MIN_INTERVAL = 180
GOLDEN_MAX_INTERVAL = 420
GOLDEN_LIFETIME = 12
# Эффекты (выбираются с весами): frenzy = клики x7 на 25 сек, chain = мгновенный
# бонус печенек (доля от часового пассивного дохода, минимум по уровню)
GOLDEN_EFFECTS = {
    "frenzy": {"weight": 6, "mult": 7.0, "seconds": 25},
    "chain":  {"weight": 4, "passive_hours": 0.75, "min_per_level": 300},
}

# ---------- Комбо за непрерывный тап ----------
# Держишь темп >= COMBO_MIN_CPS — множитель растёт до x2; пауза сбрасывает.
# Считается на сервере по батчам кликов (окно между батчами <= COMBO_WINDOW сек).
COMBO_MIN_CPS = 3.0
COMBO_WINDOW = 4.0              # сек тишины, после которых комбо сгорает
COMBO_STEP = 0.1                # +10% за каждый непрерывный батч
COMBO_MAX_MULT = 2.0

# ---------- Престиж ----------
# Сброс прогресса (кроме Stars-покупок, скинов, стрика, рефералов) за постоянный
# множитель ко ВСЕМУ доходу. Очки престижа = sqrt(total_earned / PRESTIGE_BASE).
PRESTIGE_MIN_EARNED = 10_000_000    # раньше — кнопка неактивна
PRESTIGE_BASE = 1_000_000
PRESTIGE_MULT_PER_POINT = 0.02      # +2% дохода за очко

def prestige_points(total_earned: float) -> int:
    if total_earned < PRESTIGE_MIN_EARNED:
        return 0
    return int((total_earned / PRESTIGE_BASE) ** 0.5)

def prestige_multiplier(points: float) -> float:
    return 1.0 + points * PRESTIGE_MULT_PER_POINT

# ---------- Уведомления от бота ----------
NOTIFY_MIN_INTERVAL_H = 20      # не чаще одного пуша в ~20 часов
NOTIFY_SKIP_ACTIVE_H = 3        # не пушим тем, кто был онлайн последние 3 часа

# ---------- Достижения ----------
# key: (название, описание, поле-счётчик, цель, награда cookies)
ACHIEVEMENTS = {
    "clicks_100":    ("Разминка",        "Сделай 100 кликов",            "total_clicks", 100,     500),
    "clicks_1000":   ("Кликер",          "Сделай 1 000 кликов",          "total_clicks", 1000,    2000),
    "clicks_10000":  ("Кликомашина",     "Сделай 10 000 кликов",         "total_clicks", 10000,   10000),
    "merges_10":     ("Первые слияния",  "Сделай 10 слияний",            "total_merges", 10,      500),
    "merges_100":    ("Кондитер",        "Сделай 100 слияний",           "total_merges", 100,     3000),
    "merges_500":    ("Фабрика печенек", "Сделай 500 слияний",           "total_merges", 500,     15000),
    "earned_10k":    ("Богач",           "Заработай 10 000 печенек",     "total_earned", 10000,   1000),
    "earned_100k":   ("Магнат",          "Заработай 100 000 печенек",    "total_earned", 100000,  8000),
    "level_5":       ("Путешественник",  "Достигни 5 уровня",            "level",        5,       2000),
    "level_10":      ("Первопроходец",   "Достигни 10 уровня",           "level",        10,      8000),
    "refs_1":        ("Друг",            "Пригласи 1 друга",             "_refs",        1,       1000),
    "refs_3":        ("Компания",        "Пригласи 3 друзей",            "_refs",        3,       3000),
    "refs_10":       ("Лидер мнений",    "Пригласи 10 друзей",          "_refs",        10,      15000),
}

# ---------- Магазин за Stars ----------
# item_key: (название, описание, цена Stars, эффект)
SHOP_ITEMS = {
    "energy_full":   ("Полная энергия",  "Мгновенно восстановить энергию",        25,  {"type": "energy_full"}),
    "boost_x2_1h":   ("Буст x2 (1 час)", "Клики дают x2 печенек 1 час",           50,  {"type": "boost", "key": "click_x2", "hours": 1}),
    "boost_x2_24h":  ("Буст x2 (24 ч)",  "Клики дают x2 печенек 24 часа",         200, {"type": "boost", "key": "click_x2", "hours": 24}),
    "cookies_5k":    ("5 000 печенек",   "Пачка печенек на счёт",                 75,  {"type": "cookies", "amount": 5000}),
    "cookies_25k":   ("25 000 печенек",  "Большая пачка печенек",                 300, {"type": "cookies", "amount": 25000}),
    "bp_premium":    ("Premium Пасс",    "Открывает premium-награды батл-пасса",  BP_PREMIUM_STARS, {"type": "bp_premium"}),
}

BOOST_CLICK_X2_MULT = 2.0

# ---------- Ферма (здания с автофармом, покупка за печеньки) ----------
# key: (базовая цена, cookies/сек с одного, требуемый уровень игрока)
FARM_BUILDINGS = {
    "cursor":   {"base_cost": 100,      "cps": 0.5,   "req_level": 1},
    "granny":   {"base_cost": 1_000,    "cps": 4,     "req_level": 2},
    "bakery":   {"base_cost": 8_000,    "cps": 20,    "req_level": 4},
    "factory":  {"base_cost": 50_000,   "cps": 90,    "req_level": 7},
    "mine":     {"base_cost": 250_000,  "cps": 350,   "req_level": 10},
    "portal":   {"base_cost": 1_500_000,"cps": 1500,  "req_level": 15},
    "timelab":  {"base_cost": 9_000_000,"cps": 7000,  "req_level": 20},
    "moonbase": {"base_cost": 60_000_000,  "cps": 35000,  "req_level": 24},
    "singularity": {"base_cost": 400_000_000, "cps": 180000, "req_level": 28},
}
FARM_COST_GROWTH = 1.15          # цена растёт x1.15 за каждое купленное здание
FARM_OFFLINE_CAP_HOURS = 3       # оффлайн-фарм копится максимум 3 часа

def building_cost(key: str, owned: int) -> float:
    return FARM_BUILDINGS[key]["base_cost"] * (FARM_COST_GROWTH ** owned)

# ---------- Апгрейды за печеньки (одноразовые) ----------
# key: (цена, тип эффекта, значение, требуемый уровень)
COOKIE_UPGRADES = {
    "click_mult_2":    {"cost": 5_000,      "effect": "click_mult",  "value": 2.0,  "req_level": 2},
    "click_mult_4":    {"cost": 100_000,    "effect": "click_mult",  "value": 2.0,  "req_level": 8},
    "farm_mult_2":     {"cost": 25_000,     "effect": "farm_mult",   "value": 2.0,  "req_level": 5},
    "farm_mult_4":     {"cost": 500_000,    "effect": "farm_mult",   "value": 2.0,  "req_level": 12},
    "energy_cap_250":  {"cost": 10_000,     "effect": "energy_cap",  "value": 250,  "req_level": 3},
    "energy_cap_500":  {"cost": 150_000,    "effect": "energy_cap",  "value": 500,  "req_level": 9},
    "energy_regen_2":  {"cost": 50_000,     "effect": "energy_regen","value": 0.5,  "req_level": 6},
    "passive_mult_2":  {"cost": 75_000,     "effect": "passive_mult","value": 2.0,  "req_level": 7},
}

# ---------- Скины большой печеньки (за печеньки) ----------
# key: (цена, эмодзи, требуемый уровень)
COOKIE_SKINS_SHOP = {
    "classic":  {"cost": 0,          "emoji": "🍪", "req_level": 1},
    "donut":    {"cost": 20_000,     "emoji": "🍩", "req_level": 3},
    "cupcake":  {"cost": 60_000,     "emoji": "🧁", "req_level": 5},
    "pancakes": {"cost": 150_000,    "emoji": "🥞", "req_level": 8},
    "cake":     {"cost": 400_000,    "emoji": "🎂", "req_level": 11},
    "pizza":    {"cost": 1_000_000,  "emoji": "🍕", "req_level": 14},
    "planet":   {"cost": 5_000_000,  "emoji": "🪐", "req_level": 18},
    "diamond":  {"cost": 25_000_000, "emoji": "💎", "req_level": 24},
}
