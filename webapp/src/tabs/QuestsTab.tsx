// Ежедневные задания (3 в день, реролл 1/день) + заказы пекарни
import { useEffect, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxFanfare } from '../sound'
import type { Quest } from '../types'

const METRIC_ICO: Record<string, string> = {
  clicks: '👆', merges: '🧩', spawns: '🍪', buildings: '🏭', earned: '💰', make_item: '⭐',
}
const DIFF_STARS = ['', '★', '★★', '★★★']

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

export default function QuestsTab() {
  const t = useT()
  const te = useTErr()
  const { refresh, toast, flushClicks } = useGame()
  const [quests, setQuests] = useState<Quest[]>([])
  const [rerollLeft, setRerollLeft] = useState(false)
  const [orders, setOrders] = useState<OrdersState | null>(null)

  const load = () => {
    api.get('/api/quests').then((r) => {
      setQuests(r.quests)
      setRerollLeft(!!r.reroll_available)
    })
    api.get('/api/orders').then(setOrders).catch(() => {})
  }
  useEffect(() => {
    load()
  }, [])

  const claim = async (key: string) => {
    try {
      const r = await api.post('/api/quests/claim', { key })
      hapticSuccess()
      sfxFanfare()
      toast(t('quest_reward_got', { n: fmt(r.reward_cookies), x: fmt(r.reward_bp_xp) }))
      setQuests(r.quests)
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const reroll = async (key: string) => {
    try {
      const r = await api.post('/api/quests/reroll', { key })
      sfxBuy()
      setQuests(r.quests)
      setRerollLeft(false)
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

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

  const questText = (q: Quest) => t(`q_${q.metric}` as any, { n: fmt(q.goal) })
  const orderText = (o: Order) => t(`order_${o.template}` as any, { n: fmt(o.goal) })

  const orderCard = (o: Order, isActive: boolean) => (
    <div className="card ach" key={`${o.slot}-${o.template}`}>
      <span className="ico">{METRIC_ICO[o.metric] || '🧾'}</span>
      <div className="grow">
        <b style={{ fontSize: 14 }}>
          {orderText(o)} <span style={{ color: 'var(--accent)' }}>{DIFF_STARS[o.difficulty]}</span>
        </b>
        <div className="hint">🎁 🍪 {fmt(o.reward_cookies)} · 🎖️ {fmt(o.reward_bp_xp)} XP</div>
        {isActive && (
          <div className="progress-bar" style={{ marginTop: 5 }}>
            <div style={{ width: `${Math.min(100, (o.progress / o.goal) * 100)}%` }} />
          </div>
        )}
      </div>
      {isActive ? (
        <button className="claim-chip" disabled={!o.done} onClick={claimOrder}>
          {o.done ? t('order_claim') : `${fmt(o.progress)}/${fmt(o.goal)}`}
        </button>
      ) : (
        <button className="claim-chip" onClick={() => takeOrder(o.slot)}>
          {t('order_take')}
        </button>
      )}
    </div>
  )

  return (
    <div>
      {/* --- заказы пекарни: цель на 3-5 минут, связывает все режимы --- */}
      {orders && (
        <>
          <div className="card">
            <b>{t('orders_title')}</b>
            <div className="hint" style={{ marginTop: 4 }}>
              {orders.left_today > 0
                ? t('orders_hint', { n: orders.left_today })
                : t('orders_limit')}
            </div>
          </div>
          {orders.active && orderCard(orders.active, true)}
          {!orders.active && orders.left_today > 0 && orders.offers.map((o) => orderCard(o, false))}
        </>
      )}

      <div className="card" style={{ marginTop: 14 }}>
        <b>{t('quests_title')}</b>
        <div className="hint" style={{ marginTop: 4 }}>{t('quests_hint')}</div>
      </div>
      {quests.map((q) => (
        <div className="card ach" key={q.key}>
          <span className="ico">{q.claimed ? '✅' : q.done ? '🎁' : METRIC_ICO[q.metric] || '📋'}</span>
          <div className="grow">
            <b style={{ fontSize: 14 }}>{questText(q)}</b>
            <div className="hint">
              🍪 {fmt(q.reward_cookies)} · 🎖️ {fmt(q.reward_bp_xp)} XP
              {rerollLeft && !q.done && !q.claimed && (
                <>
                  {' · '}
                  <span
                    style={{ color: 'var(--accent)', cursor: 'pointer' }}
                    onClick={() => reroll(q.key)}
                  >
                    🎲 {t('quest_reroll')}
                  </span>
                </>
              )}
            </div>
            <div className="progress-bar" style={{ marginTop: 5 }}>
              <div style={{ width: `${(q.progress / q.goal) * 100}%` }} />
            </div>
          </div>
          <button className="claim-chip" disabled={!q.done || q.claimed} onClick={() => claim(q.key)}>
            {q.claimed ? '✓' : `${fmt(q.progress)}/${fmt(q.goal)}`}
          </button>
        </div>
      ))}
    </div>
  )
}
