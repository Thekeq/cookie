// Пекарня: заказы — полноценный режим игры со своей вкладкой.
// Сцена печи: idle (выбор заказа) -> baking (печенья едут в печь) -> ready (сундук).
import { useEffect, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxFanfare } from '../sound'

const METRIC_ICO: Record<string, string> = {
  clicks: '👆', merges: '🧩', spawns: '🍪', buildings: '🏭', earned: '💰', make_item: '⭐',
}
const DIFF_STARS = ['', '★', '★★', '★★★']
const DIFF_CHEST = ['', '🎁', '🧰', '🏆'] // сундук растёт со сложностью

interface Order {
  slot: number
  template: string
  metric: string
  goal: number
  progress: number
  done: boolean
  reward_cookies: number
  reward_bp_xp: number
  difficulty: number
}

interface OrdersState {
  active: Order | null
  offers: Order[]
  left_today: number
  per_day: number
}

export default function BakeryTab() {
  const t = useT()
  const te = useTErr()
  const { refresh, toast, flushClicks } = useGame()
  const [orders, setOrders] = useState<OrdersState | null>(null)

  const load = () => {
    api.get('/api/orders').then(setOrders).catch(() => {})
  }
  useEffect(() => {
    load()
    // прогресс заказа растёт в других вкладках — держим сцену свежей
    const timer = setInterval(load, 10_000)
    return () => clearInterval(timer)
  }, [])

  const takeOrder = async (slot: number) => {
    try {
      await api.post('/api/orders/take', { slot })
      sfxBuy()
      load()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const claimOrder = async () => {
    try {
      await flushClicks() // тапы должны долететь до сервера (метрика clicks)
      const r = await api.post('/api/orders/claim')
      hapticSuccess()
      sfxFanfare()
      toast(t('order_done_toast', { n: fmt(r.reward_cookies), m: fmt(r.reward_bp_xp) }))
      setOrders(r.orders)
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const orderText = (o: Order) => t(`order_${o.template}` as any, { n: fmt(o.goal) })

  if (!orders) return null
  const active = orders.active
  const mode = active ? (active.done ? 'ready' : 'baking') : 'idle'

  return (
    <div>
      <div className="card">
        <b>{t('orders_title')}</b>
        <div className="hint" style={{ marginTop: 4 }}>
          {orders.left_today > 0
            ? t('orders_hint', { n: orders.left_today })
            : t('orders_limit')}
        </div>
      </div>

      {/* --- сцена печи --- */}
      <div className={'oven-scene ' + mode}>
        {/* конвейер печенек к печи — крутится, пока заказ печётся */}
        {mode === 'baking' && (
          <div className="oven-belt">
            {[0, 1, 2].map((i) => (
              <span key={i} className="belt-cookie" style={{ animationDelay: `${i * 1.1}s` }}>
                🍪
              </span>
            ))}
          </div>
        )}
        <div className="oven-body">
          <div className="oven-door">
            {mode === 'ready' ? (
              <span className="oven-chest" onClick={claimOrder}>
                {DIFF_CHEST[active!.difficulty]}
              </span>
            ) : (
              <span className="oven-fire">{mode === 'baking' ? '🔥' : '💤'}</span>
            )}
          </div>
        </div>
        <div className="oven-caption">
          {mode === 'idle' && t('bakery_pick')}
          {mode === 'baking' && t('bakery_baking')}
          {mode === 'ready' && t('bakery_ready')}
        </div>
      </div>

      {/* --- активный заказ: прогресс + сдача --- */}
      {active && (
        <div className="card ach">
          <span className="ico">{METRIC_ICO[active.metric] || '🧾'}</span>
          <div className="grow">
            <b style={{ fontSize: 14 }}>
              {orderText(active)}{' '}
              <span style={{ color: 'var(--accent)' }}>{DIFF_STARS[active.difficulty]}</span>
            </b>
            <div className="hint">
              🎁 🍪 {fmt(active.reward_cookies)} · 🎖️ {fmt(active.reward_bp_xp)} XP
            </div>
            <div className="progress-bar" style={{ marginTop: 5 }}>
              <div style={{ width: `${Math.min(100, (active.progress / active.goal) * 100)}%` }} />
            </div>
          </div>
          <button className="claim-chip" disabled={!active.done} onClick={claimOrder}>
            {active.done ? t('order_claim') : `${fmt(active.progress)}/${fmt(active.goal)}`}
          </button>
        </div>
      )}

      {/* --- офферы: три заказа разной сложности --- */}
      {!active && orders.left_today > 0 &&
        orders.offers.map((o) => (
          <div className="card ach offer-card" key={`${o.slot}-${o.template}`}>
            <span className="ico">{METRIC_ICO[o.metric] || '🧾'}</span>
            <div className="grow">
              <b style={{ fontSize: 14 }}>
                {orderText(o)}{' '}
                <span style={{ color: 'var(--accent)' }}>{DIFF_STARS[o.difficulty]}</span>
              </b>
              <div className="hint">
                {DIFF_CHEST[o.difficulty]} 🍪 {fmt(o.reward_cookies)} · 🎖️ {fmt(o.reward_bp_xp)} XP
              </div>
            </div>
            <button className="claim-chip" onClick={() => takeOrder(o.slot)}>
              {t('order_take')}
            </button>
          </div>
        ))}
    </div>
  )
}
