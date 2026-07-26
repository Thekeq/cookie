"""Весь игровой баланс в одном месте — крутить числа тут."""

# ---------- Кликер ----------
MAX_CPS = 15                    # серверный потолок кликов/сек (анти-чит)
ENERGY_PER_CLICK = 1
# 0.45/сек: полный бак (400) за ~15 мин. Энергия должна КОНЧАТЬСЯ в сессии
# тапа (80 сек при 5 cps) — иначе апгрейды энергии бессмысленны (фидбек игроков)
ENERGY_REGEN_PER_SEC = 0.45

def max_energy(user_level: int) -> int:
    """Тесный бак: на 1 lvl хватает на ~80 сек тапа, качается уровнями и
    апгрейдами energy_cap — теперь их есть смысл покупать."""
    return 400 + (user_level - 1) * 50

# Сила клика РАСТЁТ ЭКСПОНЕНЦИАЛЬНО. Раньше она была линейной (level * 1.0)
# при цене 1.8^n: апгрейд с 20-го уровня окупался 219 часов, с 30-го — 52 000
# часов, и главная механика игры умирала к 12 уровню.
# 1.55 против цены 1.8 — окупаемость мягко растёт (x1.16 за уровень), поэтому
# ветка остаётся живой, но не превращается в бесконечный источник инфляции.
CLICK_POWER_GROWTH = 1.55
CLICK_COST_GROWTH = 1.8

def click_power(click_level: int) -> float:
    """Сколько cookies даёт один клик"""
    return CLICK_POWER_GROWTH ** (click_level - 1)

def click_upgrade_cost(click_level: int) -> float:
    """Цена прокачки клика с текущего уровня на следующий"""
    return 100 * (CLICK_COST_GROWTH ** (click_level - 1))

# Потолок прокачки клика по уровню игрока — тот же принцип, что req_level у
# зданий: одних денег мало, нужен прогресс. Без него жадная стратегия качает
# клик бесконечно и за 72 часа выносит 77 миллиардов (проверено симуляцией).
def click_max_level(user_level: int) -> int:
    return 4 + 2 * user_level

# ---------- Merge ----------
BOARD_SIZE = 25                 # сетка 5x5 (рисуется целиком, но не вся открыта)
MAX_ITEM_LEVEL = 24

# Клетки — дефицит (фидбек: «нет опасности, что закончатся клетки»).
# Базово открыто MERGE_BASE_CELLS, остальные — за уровни и приглашённых друзей.
MERGE_BASE_CELLS = 12
MERGE_CELL_LEVELS = (3, 5, 7, 9, 11, 13, 15, 18, 21)   # каждый уровень = +1 клетка
MERGE_CELL_REFS = (1, 3, 5, 10)                        # каждый порог друзей = +1 клетка

def merge_cells_unlocked(user_level: int, refs: int) -> int:
    cells = MERGE_BASE_CELLS
    cells += sum(1 for lvl in MERGE_CELL_LEVELS if user_level >= lvl)
    cells += sum(1 for need in MERGE_CELL_REFS if refs >= need)
    return min(cells, BOARD_SIZE)

# Цены доски. Раньше база спавна была income/40, где income включал ферму:
# ферма растёт экспоненциально, доход печеньки — константа, поэтому окупаемость
# предмета уезжала с 8 часов до 46 миллионов часов, и доска умирала.
#
# Ключевая математика: путь слияния до тира L стоит 2^(L-1) спавнов. Значит
# цена L1-спавна ОБЯЗАНА быть почти абсолютной — любая привязка к доходу
# умножается на 2^L и делает слияние в тысячи раз дороже прямой покупки, то
# есть убивает основной цикл. Поэтому:
#   L1-спавн   — константа x надбавка за заполненность доски;
#   прямая покупка тира — привязана к тому, СКОЛЬКО ПРЕДМЕТ ПРОИЗВОДИТ.
# Дефицит при этом создают КЛЕТКИ (их 12 базовых), а не бесконечная цена.
SPAWN_COST_GROWTH = 1.08        # каждая занятая клетка делает следующую дороже
SPAWN_L1_BASE = 50.0            # база спавна печеньки 1 уровня
ITEM_PAYBACK_HOURS = 8.0        # за столько часов окупается купленный тир

def spawn_cost(items_on_board: int, board_income_per_hour: float = 0.0) -> float:
    """Цена спавна печеньки lvl1. board_income_per_hour оставлен в сигнатуре
    для совместимости вызовов и будущих правок — на цену L1 он не влияет."""
    return SPAWN_L1_BASE * (SPAWN_COST_GROWTH ** items_on_board)

def item_base_price(level: int) -> float:
    """Сколько предмет производит за ITEM_PAYBACK_HOURS часов."""
    return max(SPAWN_L1_BASE,
               passive_income_per_hour(level) * ITEM_PAYBACK_HOURS)

# топ-тиры только слиянием: напрямую можно купить максимум unlocked - 3
SPAWN_DIRECT_GAP = 3

def direct_spawn_cost(level: int, items_on_board: int,
                      board_income_per_hour: float = 0.0) -> float:
    """Прямая покупка тира: окупается за ITEM_PAYBACK_HOURS часов + та же
    надбавка за заполненность. Всегда дороже пути слияния — слияние выгоднее."""
    if level <= 1:
        return spawn_cost(items_on_board, board_income_per_hour)
    return item_base_price(level) * (SPAWN_COST_GROWTH ** items_on_board)

# Мусорка/печь: перетащил печеньку — клетка освободилась, вернулась часть цены.
# Возврат считается от ФАКТИЧЕСКИ вложенного (board.paid), а не от текущей цены:
# иначе доска, собранная в бедности, переплавлялась бы по ценам богатого игрока
TRASH_REFUND = 0.10

def legacy_item_value(level: int) -> float:
    """Оценка вложенного для строк доски без paid (созданы до миграции).
    Считаем по пути слияния на пустой доске — щедрее не надо."""
    return SPAWN_L1_BASE * (2 ** (level - 1))

def merge_reward_xp(new_level: int) -> float:
    """XP уровня игрока за создание печеньки new_level. После 10 lvl рост гасим."""
    if new_level <= 10:
        return 10 * (2 ** (new_level - 2))
    return 10 * (2 ** 8) * (1.25 ** (new_level - 10))

# XP батл-пасса за мердж считается ОТДЕЛЬНО и жёстко ограничен: полный пасс
# стоит bp_total_xp(30) = 74 400, а один мердж 24 lvl давал 58 208 XP — 78%
# сезона за одно слияние (фидбек). Кап держит топ-мердж в пределах ~2% пасса.
MERGE_BP_XP_CAP = 1200

def merge_reward_bp_xp(new_level: int) -> float:
    return min(merge_reward_xp(new_level), MERGE_BP_XP_CAP)

# Рост дохода печеньки за уровень. ОБЯЗАН быть > 2.0: слияние съедает две
# печеньки, и при базе 1.7 каждый мердж выше 12 уровня УМЕНЬШАЛ доход на 15%
# (2 x L12 = 217k/ч -> 1 x L13 = 185k/ч) — основной цикл игры работал против
# игрока. 2.15 даёт честные +7.5% за слияние на всём диапазоне.
PASSIVE_GROWTH = 2.15
PASSIVE_BASE = 90.0

def passive_income_per_hour(item_level: int) -> float:
    """Пассивный доход cookies/час от печеньки на доске.
    Единая экспонента без излома: доска маленькая (12-25 клеток) и жёстко
    ограничена уровнем игрока, поэтому разогнаться она не может физически."""
    if item_level < 3:
        return 0
    return PASSIVE_BASE * (PASSIVE_GROWTH ** (item_level - 3))

PASSIVE_CAP_HOURS = 3           # базовый кап оффлайн-дохода; Stars-покупка
                                # offline_cap_* добавляет часы навсегда

def item_unlock_level(item_level: int) -> int:
    """С какого уровня игрока доступна печенька item_level (спавн/merge выше — нельзя).
    Щедрее ранних анлоков: над текущим потолком всегда есть 2-3 непокорённых тира."""
    unlocks = {1: 1, 2: 1, 3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 7, 9: 9, 10: 11,
               11: 13, 12: 15, 13: 17, 14: 19, 15: 21, 16: 22, 17: 23, 18: 24,
               19: 25, 20: 26, 21: 27, 22: 28, 23: 29, 24: 30}
    return unlocks.get(item_level, 99)

# ---------- Уровни (тропинка) ----------
MAX_LEVEL = 30
# На потолке уровней XP больше некуда девать — переливаем долю в батл-пасс,
# иначе весь поздний прогресс уходил в пустоту
MAXLEVEL_XP_TO_BP = 0.25

def xp_for_level(level: int) -> float:
    """Сколько всего XP нужно, чтобы достичь уровня level.
    250*(n-1)^2.1: круче прежних 200^1.9 — фидбек «изи дошёл до 16 за день»;
    теперь активный игрок берёт ~8 lvl за 3 дня, дальше каждый уровень — событие."""
    if level <= 1:
        return 0
    return 250 * (level - 1) ** 2.1

def level_reward(level: int) -> dict:
    """Награда за достижение уровня. Растёт быстрее линейного, чтобы поздние
    уровни оставались событием; full_refill — левел-ап заливает энергию до полного."""
    # max_energy_up — насколько вырастет ПОТОЛОК энергии (см. max_energy);
    # раньше поле звалось energy_bonus и читалось как «выдадим 50 энергии»
    return {"cookies": int(150 * level ** 1.5), "max_energy_up": 50, "full_refill": True}

# ---------- Сезоны ----------
# Всё сезонное (батл-пасс, лидерборд) живёт циклами SEASON_LENGTH_DAYS.
# Номер сезона считается от SEASON_EPOCH, ролловер происходит лениво при первом
# запросе нового сезона (см. game_logic.current_season / ensure_season).
SEASON_EPOCH = 1783900800        # 2026-07-13 00:00 UTC, понедельник
SEASON_LENGTH_DAYS = 14

# Награды топ-10 лидерборда: доля от season_earned победителя (масштабируется
# с инфляцией сама) с абсолютным минимумом для холодного старта.
# место -> (доля от собственного заработка за сезон, минимум печенек)
SEASON_TOP_REWARDS = {
    1: (0.30, 100_000), 2: (0.25, 60_000), 3: (0.20, 40_000),
    4: (0.15, 25_000), 5: (0.12, 20_000), 6: (0.10, 15_000),
    7: (0.08, 12_000), 8: (0.07, 10_000), 9: (0.06, 8_000), 10: (0.05, 6_000),
}

def season_reward(rank: int, earned: float) -> float:
    if rank not in SEASON_TOP_REWARDS:
        return 0
    share, minimum = SEASON_TOP_REWARDS[rank]
    return max(earned * share, minimum)

# ---------- Батл-пасс ----------
BP_MAX_LEVEL = 30
BP_PREMIUM_STARS = 100          # цена premium-пасса в Stars
# Купленный премиум сгорал на ролловере сезона: покупка за 100⭐ вечером перед
# сменой сезона обнулялась утром. Если до конца сезона осталось меньше — премиум
# переносится и на следующий сезон (bp_premium_next).
BP_PREMIUM_GRACE_DAYS = 3

def bp_xp_for_level(level: int) -> float:
    """Сколько XP стоит взять уровень level (не кумулятивно).
    level*160: после энерго-нерфа кликовый XP просел вдвое; полный пасс
    (74.4k суммарно) — ~12.5 дней активной игры из 14 дней сезона."""
    return level * 160

def bp_total_xp(level: int) -> float:
    """Кумулятивный XP до уровня level включительно."""
    return 80 * level * (level + 1)  # сумма арифм. прогрессии x160

def bp_level_for_xp(xp: float) -> int:
    lvl = 0
    while lvl < BP_MAX_LEVEL and xp >= bp_total_xp(lvl + 1):
        lvl += 1
    return lvl

# Дневной кап XP за клики: после CLICK_XP_SOFT_CAP кликов в день клик даёт
# вчетверо меньше XP — мягкий тормоз для автокликеров, честным не мешает
CLICK_XP_SOFT_CAP = 10_000
CLICK_XP_RATE = 0.5
CLICK_XP_RATE_CAPPED = 0.125

# Награды пасса тоже в часах дохода: весь премиум-трек за 30 уровней давал
# 186 000 печенек — на 30-й день это 0.04 секунды дохода, то есть Premium Пасс
# за 100⭐ был в 190 000 раз хуже пачки печенек за 75⭐.
BP_REWARD_HOURS_FREE = 0.10
BP_REWARD_HOURS_PREMIUM = 0.28

def bp_reward(bp_level: int, premium: bool, income_per_hour: float = 0.0) -> dict:
    """Награда уровня пасса: базовая сумма ИЛИ доля часового дохода — что больше."""
    if premium:
        base = 400 * bp_level
        scaled = income_per_hour * BP_REWARD_HOURS_PREMIUM * bp_level / BP_MAX_LEVEL
        return {"cookies": max(base, scaled),
                "energy": 100 if bp_level % 5 == 0 else 0}
    base = 150 * bp_level
    scaled = income_per_hour * BP_REWARD_HOURS_FREE * bp_level / BP_MAX_LEVEL
    return {"cookies": max(base, scaled), "energy": 0}


# ---------- Масштабирование фиксированных наград ----------
# Весь слой констант (дейлик, ачивки, уровни, туториал, канал, рефералка)
# умирал за 48 часов: на 10-й день дейлик в 15 000 печенек = 0.34 секунды
# дохода. Каждая награда теперь max(константа, доля часового дохода).
REWARD_INCOME_HOURS = {
    "daily": 0.5,
    "achievement": 1.0,
    "level": 1.5,
    "tutorial": 0.25,
    "channel": 0.5,
    "referrer": 0.75,
    "referred": 0.4,
    "season_top": 0.0,   # сезонные призы уже считаются долей season_earned
}

def scaled_reward(base: float, kind: str, income_per_hour: float) -> float:
    """max(константа, доля часового дохода) — награда не обесценивается."""
    return max(base, income_per_hour * REWARD_INCOME_HOURS.get(kind, 0.0))

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
# reward_hours — доля часового дохода; задания, которые ТРАТЯТ печеньки
# (спавн, мердж), платят больше, чем бесплатные кликовые, иначе они были чистым
# убытком (merges_15 стоил 1.56ч дохода, а платил 0.5ч) и игрок их реролил.
DAILY_QUESTS_PER_DAY = 3
DAILY_QUEST_POOL = {
    "clicks_200":   {"metric": "clicks",    "goal": 200, "reward_cookies": 1500, "reward_bp_xp": 200, "reward_hours": 0.35},
    "clicks_500":   {"metric": "clicks",    "goal": 500, "reward_cookies": 3000, "reward_bp_xp": 350, "reward_hours": 0.5},
    "merges_5":     {"metric": "merges",    "goal": 5,   "reward_cookies": 1200, "reward_bp_xp": 200, "reward_hours": 0.8},
    "merges_15":    {"metric": "merges",    "goal": 15,  "reward_cookies": 3000, "reward_bp_xp": 350, "reward_hours": 1.8},
    "spawn_10":     {"metric": "spawns",    "goal": 10,  "reward_cookies": 1000, "reward_bp_xp": 150, "reward_hours": 1.2},
    "buy_2":        {"metric": "buildings", "goal": 2,   "reward_cookies": 2000, "reward_bp_xp": 250, "reward_hours": 0.6},
    "earn_5k":      {"metric": "earned",    "goal": 5000, "reward_cookies": 2500, "reward_bp_xp": 300, "reward_hours": 0.5},
}
# Цель «заработай N» обязана масштабироваться: константа 5000 при доходе 2e9/ч
# выполняется за 0.009 секунды и платит миллиард.
QUEST_EARN_GOAL_HOURS = 0.5

def quest_goal(quest_key: str, income_per_hour: float) -> float:
    """Цель задания с поправкой на доход (для метрики earned)."""
    q = DAILY_QUEST_POOL[quest_key]
    if q["metric"] == "earned":
        return max(q["goal"], round(income_per_hour * QUEST_EARN_GOAL_HOURS))
    return q["goal"]

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
# Порог растёт с каждым престижем: N-й требует 10M * 15^N — каждый престиж событие.
PRESTIGE_MIN_EARNED = 10_000_000
PRESTIGE_THRESHOLD_GROWTH = 15
PRESTIGE_BASE = 1_000_000
# Было +2% за очко: первый престиж давал +6% и стирал уровень, ферму, доску и
# апгрейды — перерождаться не имело смысла никогда, оптимально было не
# престижить вообще. 0.30 делает первый престиж примерно x1.9.
PRESTIGE_MULT_PER_POINT = 0.30      # +30% дохода за очко
# Престиж сохраняет часть уровня: полный откат на 1-й уровень означал заново
# проходить все req_level зданий и предметов, а множитель этого не ускорял.
PRESTIGE_KEEP_LEVEL_SHARE = 0.4

def prestige_threshold(prestige_count: int) -> float:
    """Сколько total_earned нужно для престижа номер prestige_count+1."""
    return PRESTIGE_MIN_EARNED * (PRESTIGE_THRESHOLD_GROWTH ** prestige_count)

def prestige_points(total_earned: float) -> int:
    if total_earned < PRESTIGE_MIN_EARNED:
        return 0
    return int((total_earned / PRESTIGE_BASE) ** 0.5)

def prestige_multiplier(points: float) -> float:
    return 1.0 + points * PRESTIGE_MULT_PER_POINT

# ---------- Заказы пекарни ----------
# Игроку предлагаются 3 заказа разной сложности, он берёт один. Награда-сундук
# масштабируется от дохода (часы дохода с минимумом) — не обесценивается.
ORDER_TEMPLATES = {
    "warmup":    {"metric": "clicks",    "goal": 80,  "difficulty": 1},   # разогрей печь
    "delivery":  {"metric": "spawns",    "goal": 6,   "difficulty": 1},   # закупи тесто
    "batch":     {"metric": "merges",    "goal": 8,   "difficulty": 2},   # партия выпечки
    "shopping":  {"metric": "buildings", "goal": 2,   "difficulty": 2},   # расширь цех
    "profit":    {"metric": "earned",    "goal": 0,   "difficulty": 2},   # выручка (goal = час дохода)
    "special":   {"metric": "make_item", "goal": 0,   "difficulty": 3},   # печенье уровня N
    "marathon":  {"metric": "clicks",    "goal": 250, "difficulty": 3},   # клик-марафон
}
# Награда — ПО ШАБЛОНУ, а не по сложности: заказ, который тратит печеньки
# (спавн/мердж), обязан отбивать затраты, иначе он чистый убыток. Бесплатные
# кликовые заказы платят мало, поэтому суточный максимум чистыми ~1.75ч дохода
# вместо прежних 5.5ч (это было больше, чем весь оффлайн-кап).
ORDER_REWARD_HOURS = {
    "warmup": 0.15, "delivery": 0.75, "batch": 1.1, "shopping": 0.4,
    "profit": 0.5, "special": 1.0, "marathon": 0.35,
}
ORDER_REWARD_MIN = {1: 800, 2: 2200, 3: 5500}   # минимум печенек (холодный старт)
ORDER_BP_XP = {1: 60, 2: 140, 3: 300}
ORDERS_PER_DAY = 5                               # анти-гринд: заказов в день
# Заказ считается «мёртвым», если цель недостижима после сброса прогресса
# (престиж): цель по печеньке выше открытого максимума, либо цель по заработку
# больше ORDER_STALE_FACTOR часов нынешнего дохода. Такой заказ снимается.
ORDER_STALE_FACTOR = 5

# ---------- Стартовый чеклист (интерактивный первый сеанс) ----------
# Шаги проверяются по счётчикам профиля; финал — первый заказ.
TUTORIAL_STEPS = ["clicks10", "merge1", "building1", "order1"]
TUTORIAL_REWARD = 1500

# ---------- Коллекция блестящих печенек ----------
SHINY_CHANCE = 0.06        # шанс блестяшки при мердже
SHINY_PITY = 25            # гарантированная блестяшка после N мерджей без неё
# Наборы по 4 уровня вместо 6: прежние (13,18) и (19,24) требовали слияния до
# L18 и L24 (131 072 и 8 388 608 спавнов) — половина коллекции была физически
# недостижима, и реальный потолок бонуса составлял +6% вместо заявленных +12%.
COLLECTION_SETS = [(1, 4), (5, 8), (9, 12), (13, 16)]
COLLECTION_SET_BONUS = 0.03  # +3% ко всему доходу за набор

# ---------- QoL: квесты и стрик ----------
QUEST_REROLLS_PER_DAY = 1    # бесплатный реролл одного квеста в день
STREAK_FREEZE_PER_WEEK = 1   # пропуск ровно одного дня раз в неделю не сжигает стрик
# catch-up: если BP-уровень отстаёт от темпа сезона больше чем на 2 — квесты дают x2 BP XP
BP_CATCHUP_MULT = 2.0
BP_CATCHUP_LAG = 2

# ---------- Лиги ----------
# (ключ, минимальный уровень); рейтинг и сезонные призы — внутри своей лиги,
# у новичка реальный шанс на топ, а не таблица недостижимых ветеранов
LEAGUES = [("bronze", 1), ("silver", 6), ("gold", 12), ("diamond", 20)]

def league_of(level: int) -> tuple[str, int, int | None]:
    """(ключ лиги, мин. уровень, макс. уровень включительно или None)."""
    for i in range(len(LEAGUES) - 1, -1, -1):
        key, lo = LEAGUES[i]
        if level >= lo:
            hi = LEAGUES[i + 1][1] - 1 if i + 1 < len(LEAGUES) else None
            return key, lo, hi
    return LEAGUES[0][0], 1, LEAGUES[1][1] - 1

# ---------- Уведомления от бота ----------
NOTIFY_MIN_INTERVAL_H = 20      # не чаще одного пуша в ~20 часов
NOTIFY_SKIP_ACTIVE_H = 3        # не пушим тем, кто был онлайн последние 3 часа

# ---------- Аналитика ----------
# Таблица events росла без ограничений (session на каждое открытие приложения).
# Админка смотрит окна максимум 7 дней — месяца истории с запасом хватает.
EVENTS_TTL_DAYS = 30

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
# Пачки печенек масштабируются под доход покупателя: amount игнорируется, если
# задан income_hours — реальная сумма = max(min_amount, часовой доход * hours).
# Часовой доход = ферма + пассивка доски + клики (см. game_logic.hourly_income).
# item_key: (название, описание, цена Stars, эффект)
SHOP_ITEMS = {
    "energy_full":   ("Полная энергия",  "Мгновенно восстановить энергию",        25,  {"type": "energy_full"}),
    "boost_x2_1h":   ("Буст x2 (1 час)", "Клики дают x2 печенек 1 час",           50,  {"type": "boost", "key": "click_x2", "hours": 1}),
    "boost_x2_24h":  ("Буст x2 (24 ч)",  "Клики дают x2 печенек 24 часа",         200, {"type": "boost", "key": "click_x2", "hours": 24}),
    "cookies_pack":  ("Пачка печенек",   "2 часа твоего дохода мгновенно",        75,  {"type": "cookies", "income_hours": 2, "min_amount": 5000}),
    "cookies_crate": ("Ящик печенек",    "10 часов твоего дохода мгновенно",      300, {"type": "cookies", "income_hours": 10, "min_amount": 25000}),
    "bp_premium":    ("Premium Пасс",    "Открывает premium-награды батл-пасса",  BP_PREMIUM_STARS, {"type": "bp_premium"}),
    # Постоянное расширение оффлайн-капа (ферма + доска). Имба — потому дорого.
    # hours = ДОБАВКА к базовым 3ч; применяется как max(текущий бонус, hours)
    "offline_cap_6h":  ("Оффлайн 6 часов",  "Оффлайн-доход копится 6 часов. Навсегда!",  400, {"type": "offline_cap", "hours": 3}),
    "offline_cap_12h": ("Оффлайн 12 часов", "Оффлайн-доход копится 12 часов. Навсегда!", 900, {"type": "offline_cap", "hours": 9}),
}

BOOST_CLICK_X2_MULT = 2.0

# ---------- Ферма (здания с автофармом, покупка за печеньки) ----------
# key: (базовая цена, cookies/сек с одного, требуемый уровень игрока)
# Окупаемость здания растёт с тиром: 17 минут у первого, ~65 минут у последнего.
# Было 3.3 минуты (cursor: 100 печенек -> 1800/ч) — ферма окупалась почти
# мгновенно, поэтому съедала весь капитал и делала клик и доску бессмысленными.
FARM_BUILDINGS = {
    "cursor":   {"base_cost": 500,      "cps": 0.5,   "req_level": 1},
    "granny":   {"base_cost": 5_000,    "cps": 4,     "req_level": 2},
    "bakery":   {"base_cost": 32_000,   "cps": 20,    "req_level": 4},
    "factory":  {"base_cost": 180_000,  "cps": 90,    "req_level": 7},
    "mine":     {"base_cost": 600_000,  "cps": 250,   "req_level": 10},
    "portal":   {"base_cost": 2_500_000,"cps": 900,   "req_level": 15},
    "timelab":  {"base_cost": 12_000_000,"cps": 3800, "req_level": 20},
    "moonbase": {"base_cost": 56_000_000,  "cps": 16000,  "req_level": 24},
    "singularity": {"base_cost": 300_000_000, "cps": 75000, "req_level": 28},
}
FARM_COST_GROWTH = 1.22          # цена растёт за каждое купленное здание
FARM_OFFLINE_CAP_HOURS = 3       # базовый кап оффлайн-фарма (+ Stars-бонус offline_cap_*)

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
