export interface UserState {
  user_id: number
  username: string
  first_name: string
  cookies: number
  level: number
  xp: number
  xp_next: number | null
  energy: number
  max_energy: number
  energy_regen?: number
  click_level: number
  click_power: number
  /** сила следующего уровня — считает сервер, кривая экспоненциальная */
  click_power_next?: number
  click_upgrade_cost: number
  total_clicks: number
  total_merges: number
  bp_xp: number
  bp_premium: boolean
  active_skin: string
  skin_emoji: string
}

export interface FarmBuilding {
  key: string
  owned: number
  cps_each: number
  cost: number
  req_level: number
  req_record: number
  max_copies: number
  maxed: boolean
  unlocked: boolean
  lock: 'level' | 'record' | null
}

export interface FarmUpgrade {
  key: string
  cost: number
  effect: string
  value: number
  req_level: number
  unlocked: boolean
  owned: boolean
}

export interface FarmSkin {
  key: string
  cost: number
  emoji: string
  req_level: number
  unlocked: boolean
  owned: boolean
  active: boolean
}

export interface FarmState {
  collected: number
  cps: number
  cookies: number
  buildings: FarmBuilding[]
  upgrades: FarmUpgrade[]
  skins: FarmSkin[]
  offline_cap_hours: number
}

export interface BoardCell {
  cell: number
  item_level: number
}

export interface DailyState {
  can_claim: boolean
  streak: number
  next_streak: number
  next_reward: number
  rewards: { day: number; cookies: number }[]
}

export interface Quest {
  key: string
  metric: string
  goal: number
  reward_cookies: number
  reward_bp_xp: number
  progress: number
  done: boolean
  claimed: boolean
}

export interface RefMilestone {
  key: string
  count: number
  type: string
  progress: number
  done: boolean
  claimed: boolean
}

export interface GameState {
  /** версии состояния; клиент возвращает board обратно на ходах по доске */
  revision?: { user: number; board: number }
  user: UserState
  farm: { buildings: Record<string, number>; cps: number }
  upgrades_owned: string[]
  skins_owned: string[]
  board: BoardCell[]
  board_cells: {
    unlocked: number
    /** заслуженные клетки (уровни+друзья); unlocked может быть больше на
     *  старых досках — уже занятые клетки не отбираем */
    earned: number
    total: number
    next_unlock_level: number | null
    ref_cells: { friends: number; done: boolean }[]
    refs: number
    trash_refund: number
  }
  spawn_cost: number
  spawn_direct: { max_level: number; costs: Record<string, number> }
  orders_claimable: boolean
  /** потолок прокачки клика по уровню игрока */
  click_max_level?: number
  /** закваска: что печётся, пока игрока нет */
  recipe?: { key: string | null; state: string; mult: number
             ready_at?: number; spoils_at?: number }
  /** ивент выходных (детерминирован календарём, может быть null) */
  event?: { key: string; mult: number; title_key: string
            started_at: number; ends_at: number } | null
  passive_per_hour: number
  trash_refund?: number // ответ /api/merge/trash: сколько вернули
  passive_collected?: number
  boosts: { key: string; expires_at: number }[]
  claimable_level: number | null
  max_item_unlocked: number
  just_registered?: boolean
  season: { id: number; ends_at: number }
  daily: DailyState
  quests_claimable: number
  golden: { active: boolean; effect: string | null; expires_at: number }
  combo: { mult: number; max_mult: number }
  tutorial?: {
    steps: { key: string; done: boolean }[]
    all_done: boolean
    claimed: boolean
    reward: number
  } | null
  merged_level?: number
  shiny?: boolean
  /** уровень, попавший в альбом (не всегда равен merged_level) */
  shiny_level?: number | null
  /** личный рекорд тира взят впервые — основная награда XP за мердж */
  record?: { level: number; xp: number; cookies: number } | null
  prestige: {
    points: number
    count: number
    multiplier: number
    gain_available: number
    min_earned: number
    can_prestige: boolean
    mult_per_point: number
    /** что останется после перерождения — видно ДО нажатия кнопки */
    kept_level?: number
    next_multiplier?: number
  }
}

export interface LevelNode {
  level: number
  xp_required: number
  reward: { cookies: number; max_energy_up: number }
  unlocks_items: number[]
  reached: boolean
}

export interface Achievement {
  key: string
  title: string
  desc: string
  progress: number
  goal: number
  reward: number
  done: boolean
  claimed: boolean
}

/** Награда уровня паса. Ключи приходят всегда, нулевые части просто не рисуем. */
export interface BPReward {
  cookies: number
  energy: number
  /** +клетки доски на сезон (0 или 1) */
  cells: number
  /** +часы потолка оффлайн-дохода на сезон */
  offline_hours: number
  /** ключ эксклюзивного скина, например "golden" */
  skin: string | null
  /** клетку сверх потолка доски сервер меняет на печенье (только в ответе claim) */
  cookies_substituted?: boolean
}

export interface BPLevel {
  level: number
  free: BPReward
  premium: BPReward
  reached: boolean
  free_claimed: boolean
  premium_claimed: boolean
}

/** Сумма premium-наград на уже взятых уровнях: ровно это выдадут в момент
 *  покупки (ретроактивный клейм умеет сервер). У владельца премиума levels = 0. */
export interface BPLockedPremium {
  levels: number
  cookies: number
  energy: number
  cells: number
  offline_hours: number
  skins: number
}

export interface ShopItem {
  key: string
  title: string
  desc: string
  stars: number
  amount?: number // персональная сумма пачки (часы дохода покупателя)
  owned?: boolean // постоянный апгрейд уже куплен
}
