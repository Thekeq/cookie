// Пекарня: заказы — полноценный режим игры со своей вкладкой.
// Сцена печи: idle (пусто) -> baking (тесто румянится) -> ready (дверца открыта).
// Печь и есть индикатор прогресса: --bake = progress/goal ведёт жар, тесто,
// стрелку термостата и пар, поэтому отдельная полоска прогресса не нужна.
import { CSSProperties, useEffect, useState } from 'react'
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
  /** id и версия строки: сервер сверяет их и отбивает действие, посчитанное
   *  по заказу, которого уже нет (вторая сессия успела его сменить) */
  id: number
  version: number
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
  /** сервер снял недостижимый заказ и вернул вложенное в него */
  compensated?: number
}

export default function BakeryTab() {
  const t = useT()
  const te = useTErr()
  const { refresh, toast, flushClicks } = useGame()
  const [orders, setOrders] = useState<OrdersState | null>(null)

  const load = () => {
    api.get('/api/orders').then((r: OrdersState) => {
      setOrders(r)
      // заказ снял сервер (цель стала недостижимой) — говорим, за что деньги
      if (r.compensated) {
        toast(t('order_compensated', { n: fmt(r.compensated) }))
        refresh()
      }
    }).catch(() => {})
  }
  useEffect(() => {
    load()
    // прогресс заказа растёт в других вкладках — держим сцену свежей
    const timer = setInterval(load, 10_000)
    return () => clearInterval(timer)
  }, [])

  const takeOrder = async (o: Order) => {
    try {
      await api.post('/api/orders/take', { slot: o.slot, id: o.id })
      sfxBuy()
      load()
    } catch (e: any) {
      sfxError()
      // офферы пересобрались, пока экран висел открытым: перечитываем, иначе
      // игрок жал бы кнопку заказа, которого уже нет
      load()
      toast(te(e.detail), true)
    }
  }

  const abandonOrder = async () => {
    if (!orders?.active) return
    try {
      const r = await api.post('/api/orders/abandon',
        { id: orders.active.id, version: orders.active.version })
      setOrders(r.orders)
    } catch (e: any) {
      sfxError()
      load()
      toast(te(e.detail), true)
    }
  }

  const claimOrder = async () => {
    try {
      await flushClicks() // тапы должны долететь до сервера (метрика clicks)
      const r = await api.post('/api/orders/claim',
        active ? { id: active.id, version: active.version } : undefined)
      hapticSuccess()
      sfxFanfare()
      toast(t('order_done_toast', { n: fmt(r.reward_cookies), m: fmt(r.reward_bp_xp) }))
      setOrders(r.orders)
      refresh()
    } catch (e: any) {
      sfxError()
      load()
      toast(te(e.detail), true)
    }
  }

  const orderText = (o: Order) => t(`order_${o.template}` as any, { n: fmt(o.goal) })

  if (!orders) return null
  const active = orders.active
  const mode = active ? (active.done ? 'ready' : 'baking') : 'idle'
  // степень готовности: 0 — сырое тесто, 1 — печенье готово
  const bake = active ? (active.done ? 1 : Math.min(1, active.progress / active.goal)) : 0

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

      {/* --- сцена печи: готовая печь тапабельна целиком --- */}
      <div
        className={'oven-scene ' + mode}
        style={{ '--bake': bake } as CSSProperties}
        role={mode === 'ready' ? 'button' : undefined}
        tabIndex={mode === 'ready' ? 0 : undefined}
        onClick={mode === 'ready' ? claimOrder : undefined}
        onKeyDown={(e) => {
          if (mode === 'ready' && (e.key === 'Enter' || e.key === ' ')) claimOrder()
        }}
      >
        <div className="oven-unit">
          <div className="oven-cavity">
            <div className="oven-rack" />
            {active && (
              <div className="bake-item">
                <span className="bake-baked" />
                <span className="bake-raw" />
                <span className="bake-chips" />
              </div>
            )}
            <div className="oven-heat">
              {[
                { left: '20%', animationDelay: '0s' },
                { left: '47%', animationDelay: '1.1s' },
                { left: '72%', animationDelay: '2.2s' },
              ].map((st, i) => (
                <i key={i} style={st} />
              ))}
            </div>
          </div>
          <div className="oven-glass" />
          <div className="oven-panel">
            <span className="oven-dial" />
            {active && <span className="oven-tier">{DIFF_CHEST[active.difficulty]}</span>}
            <span className="oven-readout">
              {mode === 'idle' ? '—' : `${Math.round(bake * 100)}%`}
            </span>
          </div>
        </div>
        <div className="oven-spill" />
        {mode === 'ready' && (
          <div className="oven-steam">
            {[
              { left: '44%', animationDelay: '0s' },
              { left: '50%', animationDelay: '0.9s' },
              { left: '56%', animationDelay: '1.7s' },
            ].map((st, i) => (
              <i key={i} style={st} />
            ))}
          </div>
        )}
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
            {/* прогресс показывает сама печь — здесь только награда */}
            <div className="hint">
              🎁 🍪 {fmt(active.reward_cookies)} · 🎖️ {fmt(active.reward_bp_xp)} XP
            </div>
          </div>
          <button className="claim-chip" disabled={!active.done} onClick={claimOrder}>
            {active.done ? t('order_claim') : `${fmt(active.progress)}/${fmt(active.goal)}`}
          </button>
        </div>
      )}

      {/* не сложился заказ — можно отказаться ценой одной попытки из лимита */}
      {active && !active.done && (
        <button className="btn secondary" style={{ fontSize: 13 }} onClick={abandonOrder}>
          {t('order_abandon')}
        </button>
      )}

      {/* --- офферы: три заказа разной сложности --- */}
      {!active && orders.left_today > 0 &&
        orders.offers.map((o) => (
          <div className="card ach offer-card" key={o.id}>
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
            <button className="claim-chip" onClick={() => takeOrder(o)}>
              {t('order_take')}
            </button>
          </div>
        ))}
    </div>
  )
}
