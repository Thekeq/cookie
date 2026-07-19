import { useEffect, useRef, useState } from 'react'
import { api, haptic, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxClick, sfxError, sfxFanfare } from '../sound'

interface Float {
  id: number
  x: number
  y: number
  text: string
}

export default function ClickerTab() {
  const { state, setState, toast, refresh, liveBalance, bumpBalance } = useGame()
  const t = useT()
  const te = useTErr()
  const [floats, setFloats] = useState<Float[]>([])
  const pending = useRef(0) // клики, ещё не отправленные на сервер
  const floatId = useRef(0)
  const wrapRef = useRef<HTMLDivElement>(null)

  // локальный предикт энергии: рисуем сразу, сервер подтверждает батчем
  // (баланс живёт в App — liveBalance, общий для шапки и этой вкладки)
  const [localEnergy, setLocalEnergy] = useState(state.user.energy)
  const [combo, setCombo] = useState(1)
  const lastTapAt = useRef(0) // для локального затухания комбо
  // золотая печенька: сервер решает когда, клиент рисует и ловит тап
  const [golden, setGolden] = useState(state.golden)
  const [goldenPos] = useState(() => ({ left: 15 + Math.random() * 55, top: 18 + Math.random() * 40 }))

  useEffect(() => {
    setLocalEnergy(state.user.energy)
  }, [state.user.cookies, state.user.energy])

  useEffect(() => {
    setGolden(state.golden)
  }, [state.golden?.active, state.golden?.expires_at])

  // регенерация энергии на клиенте — визуально, той же скоростью, что сервер
  // (сервер шлёт фактический реген с учётом апгрейдов)
  useEffect(() => {
    const regen = state.user.energy_regen ?? 0.45
    const timer = setInterval(() => {
      setLocalEnergy((e) => Math.min(state.user.max_energy, e + regen))
    }, 1000)
    return () => clearInterval(timer)
  }, [state.user.max_energy, state.user.energy_regen])

  // локальное затухание комбо: пауза в тапах > 4с — комбо гаснет сразу на клиенте,
  // не дожидаясь ответа сервера (сервер придёт к тому же выводу по своему окну)
  useEffect(() => {
    const timer = setInterval(() => {
      if (combo > 1 && Date.now() - lastTapAt.current > 4000) setCombo(1)
    }, 400)
    return () => clearInterval(timer)
  }, [combo])

  // батч-отправка кликов раз в 1.5 сек; у каждого батча уникальный id:
  // потерянный ответ ретраится тем же id, сервер не начислит дважды,
  // а батчи с других устройств не конфликтуют (id не глобальный счётчик)
  const retryBatch = useRef<{ id: string; n: number } | null>(null)
  const inflight = useRef(false)
  useEffect(() => {
    const timer = setInterval(async () => {
      if (inflight.current) return // запросы не пересекаются — ответы по порядку
      let batch = retryBatch.current
      if (!batch) {
        const n = pending.current
        if (!n) return
        pending.current = 0
        batch = { id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`, n }
      }
      inflight.current = true
      try {
        const r = await api.post('/api/click', { clicks: batch.n, batch_id: batch.id })
        retryBatch.current = null
        // серверное комбо принимаем, только если игрок ещё тапает —
        // иначе устаревший ответ «воскресит» уже погасшее комбо
        if (Date.now() - lastTapAt.current < 4000) setCombo(r.combo || 1)
        if (r.golden) setGolden(r.golden)
        setState({ ...state, user: { ...state.user, cookies: r.cookies, energy: r.energy, xp: r.xp ?? state.user.xp } })
      } catch {
        /* сеть моргнула — повторим тот же батч, дедуп на сервере */
        retryBatch.current = batch
      } finally {
        inflight.current = false
      }
    }, 1500)
    return () => clearInterval(timer)
  }, [state, setState])

  // тик времени жизни золотой печеньки
  useEffect(() => {
    if (!golden?.active) return
    const timer = setInterval(() => {
      if (Date.now() / 1000 >= golden.expires_at) setGolden({ ...golden, active: false })
    }, 500)
    return () => clearInterval(timer)
  }, [golden])

  const onClick = (e: React.PointerEvent) => {
    if (localEnergy < 1) {
      sfxError()
      toast(t('no_energy'), true)
      return
    }
    haptic('light')
    sfxClick()
    lastTapAt.current = Date.now()
    pending.current += 1
    bumpBalance(state.user.click_power * combo)
    setLocalEnergy((en) => en - 1)

    const rect = wrapRef.current!.getBoundingClientRect()
    const f: Float = {
      id: floatId.current++,
      x: e.clientX - rect.left,
      y: e.clientY - rect.top,
      text: `+${fmt(state.user.click_power * combo)}`,
    }
    setFloats((fs) => [...fs.slice(-14), f])
    setTimeout(() => setFloats((fs) => fs.filter((x) => x.id !== f.id)), 800)
  }

  const onGoldenClick = async () => {
    setGolden({ ...golden, active: false })
    hapticSuccess()
    sfxFanfare()
    try {
      const r = await api.post('/api/golden/claim')
      if (r.effect === 'frenzy') toast(t('golden_frenzy', { n: r.seconds }))
      else toast(t('golden_chain', { n: fmt(r.cookies) }))
      refresh()
    } catch {
      /* исчезла на сервере раньше — не страшно */
    }
  }

  const upgrade = async () => {
    try {
      const s = await api.post('/api/click/upgrade')
      setState(s)
      hapticSuccess()
      sfxBuy()
      toast(t('click_upgraded'))
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const boost = state.boosts.find((b) => b.key === 'click_x2')
  const frenzy = state.boosts.find((b) => b.key === 'golden_frenzy')

  return (
    <div className="clicker" ref={wrapRef} style={{ position: 'relative' }}>
      <div className="energy-wrap">
        <div className="row" style={{ marginBottom: 4 }}>
          <span className="hint">⚡ {t('energy')}</span>
          <span className="hint">
            {Math.floor(localEnergy)} / {state.user.max_energy}
          </span>
        </div>
        <div className="progress-bar">
          <div style={{ width: `${(localEnergy / state.user.max_energy) * 100}%` }} />
        </div>
      </div>

      <div style={{ fontSize: 30, fontWeight: 800, marginBottom: 4 }}>🍪 {fmt(liveBalance)}</div>
      <div className="hint" style={{ marginBottom: 10 }}>
        +{fmt(state.user.click_power * combo)} {t('per_click')}
        {boost && <span style={{ color: 'var(--good)' }}> · {t('boost_active')}</span>}
        {frenzy && <span style={{ color: 'var(--accent)' }}> · 🔥x7</span>}
      </div>

      {/* бейдж комбо всегда в DOM — появление/уход анимируются классом */}
      <div className={'combo-badge' + (combo > 1.05 ? ' show' : '')}>
        🔥 {t('combo')} x{combo.toFixed(1)}
      </div>

      <button
        className={'big-cookie' + (frenzy ? ' frenzy' : '')}
        onPointerDown={onClick}
        aria-label={t('tab_clicker')}
      >
        {state.user.skin_emoji || '🍪'}
      </button>

      {golden?.active && (
        <button
          className="golden-cookie"
          style={{ left: `${goldenPos.left}%`, top: `${goldenPos.top}%` }}
          onPointerDown={onGoldenClick}
        >
          🌟
        </button>
      )}

      {floats.map((f) => (
        <span key={f.id} className="click-float" style={{ left: f.x, top: f.y }}>
          {f.text}
        </span>
      ))}

      <div className="card" style={{ width: '100%', marginTop: 18 }}>
        <div className="row" style={{ marginBottom: 8 }}>
          <div>
            <b>{t('click_power')} · {state.user.click_level}</b>
            <div className="hint">
              {t('next_level_click')}: +{state.user.click_level + 1} {t('per_click')}
            </div>
          </div>
        </div>
        <button className="btn" disabled={liveBalance < state.user.click_upgrade_cost} onClick={upgrade}>
          {t('upgrade_for')} 🍪 {fmt(state.user.click_upgrade_cost)}
        </button>
      </div>
    </div>
  )
}
