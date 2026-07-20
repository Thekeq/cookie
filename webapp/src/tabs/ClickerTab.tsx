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
  // очередь кликов, батчи и комбо живут в App (GameCtx): переживают смену
  // вкладок, а покупки могут дождаться flushClicks() перед списанием
  const { state, setState, toast, refresh, liveBalance, combo, tapClick, flushClicks } = useGame()
  const t = useT()
  const te = useTErr()
  const [floats, setFloats] = useState<Float[]>([])
  const floatId = useRef(0)
  const wrapRef = useRef<HTMLDivElement>(null)

  // локальный предикт энергии: рисуем сразу, сервер подтверждает батчем
  const [localEnergy, setLocalEnergy] = useState(state.user.energy)
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
    tapClick(state.user.click_power * combo)
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
      await flushClicks() // сервер должен знать про все тапы до проверки цены
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
