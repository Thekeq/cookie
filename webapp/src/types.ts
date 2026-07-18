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
  click_level: number
  click_power: number
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
  unlocked: boolean
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
  user: UserState
  farm: { buildings: Record<string, number>; cps: number }
  upgrades_owned: string[]
  skins_owned: string[]
  board: BoardCell[]
  spawn_cost: number
  spawn_direct: { max_level: number; costs: Record<string, number> }
  passive_per_hour: number
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
  prestige: {
    points: number
    count: number
    multiplier: number
    gain_available: number
    min_earned: number
    can_prestige: boolean
    mult_per_point: number
  }
}

export interface LevelNode {
  level: number
  xp_required: number
  reward: { cookies: number; energy_bonus: number }
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

export interface BPLevel {
  level: number
  free: { cookies: number; energy: number }
  premium: { cookies: number; energy: number }
  reached: boolean
  free_claimed: boolean
  premium_claimed: boolean
}

export interface ShopItem {
  key: string
  title: string
  desc: string
  stars: number
  amount?: number // персональная сумма пачки (часы дохода покупателя)
}
