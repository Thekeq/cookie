import { useEffect, useRef, useState } from 'react'
import { api, haptic, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxClick, sfxError, sfxFanfare } from '../sound'
import { useBusy } from '../useBusy'
import Spinner from './Spinner'

interface Float {
  id: number
  x: number
  y: number
  text: string
  born: number
}

// Сколько живёт всплывающее «+N» и как часто их подметает общий таймер.
// Раньше на КАЖДЫЙ тап заводился свой setTimeout: на удержании это десятки
// отложенных задач одновременно, и каждая будила рендер отдельно. Один
// сборщик снимает всё просроченное разом и заводится только когда есть что
// снимать. Шаг подметания добавляет к жизни цифры максимум 200 мс — на глаз
// это незаметно, а таймеров становится один вместо N.
const FLOAT_TTL_MS = 800
const FLOAT_SWEEP_MS = 200

export default function ClickerTab() {
  // очередь кликов, батчи и комбо живут в App (GameCtx): переживают смену
  // вкладок, а покупки могут дождаться flushClicks() перед списанием
  const { state, setState, toast, refresh, liveBalance, combo, tapClick, flushClicks } = useGame()
  const t = useT()
  const te = useTErr()
  const [floats, setFloats] = useState<Float[]>([])
  const floatId = useRef(0)
  const wrapRef = useRef<HTMLDivElement>(null)
  // тап по кнопке с запросом не должен уходить дважды
  const { busy, run } = useBusy()

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

  // один сборщик просроченных «+N» вместо таймера на каждый тап. Зависимость
  // ровно на «есть ли что подметать»: пока цифр нет, таймера тоже нет, и
  // вкладка в простое не будит вебвью.
  useEffect(() => {
    if (floats.length === 0) return
    const timer = setInterval(() => {
      const cutoff = Date.now() - FLOAT_TTL_MS
      setFloats((fs) => (fs.some((f) => f.born <= cutoff)
        ? fs.filter((f) => f.born > cutoff)
        : fs))          // нечего снимать — возвращаем ТОТ ЖЕ массив, иначе
    }, FLOAT_SWEEP_MS)  // setState на каждый тик перерисовывал бы вкладку впустую
    return () => clearInterval(timer)
  }, [floats.length === 0])

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
      born: Date.now(),
    }
    setFloats((fs) => [...fs.slice(-14), f])
  }

  const onGoldenClick = () => run('golden', async () => {
    setGolden({ ...golden, active: false })
    hapticSuccess()
    sfxFanfare()
    try {
      const r = await api.post('/api/golden/claim')
      // r.bonus — сам бонус; r.cookies это баланс после начисления,
      // раньше в тост уходил именно он и выглядело как «начислили весь баланс»
      if (r.effect === 'frenzy') toast(t('golden_frenzy', { n: r.seconds }))
      else toast(t('golden_chain', { n: fmt(r.bonus) }))
      refresh()
    } catch {
      /* исчезла на сервере раньше — не страшно */
    }
  })

  const upgrade = () => run('click_up', async () => {
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
  })

  const claimTutorial = () => run('tutorial', async () => {
    try {
      const r = await api.post('/api/tutorial/claim')
      hapticSuccess()
      sfxFanfare()
      toast(`+${fmt(r.reward)} 🍪`)
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  const boost = state.boosts.find((b) => b.key === 'click_x2')
  const frenzy = state.boosts.find((b) => b.key === 'golden_frenzy')
  const tut = state.tutorial

  // потолок прокачки клика привязан к уровню игрока — кнопка должна
  // объяснять это заранее, а не отдавать ошибку по тапу
  const clickCapped = state.click_max_level != null
    && state.user.click_level >= state.click_max_level

  const ev = state.event
  const evLeft = ev ? Math.max(0, ev.ends_at - Date.now() / 1000) : 0

  return (
    <div className="clicker" ref={wrapRef} style={{ position: 'relative' }}>
      {/* Ивент выходных: повод зайти в конкретный день, а не «когда-нибудь».
          Расписание детерминировано календарём — ни таблицы, ни джобы. */}
      {ev && (
        <div className="event-banner">
          <span className="event-spark">✨</span>
          <div className="grow">
            <b>{t(ev.title_key as any)}</b>
            <div className="hint">{t('event_banner', { m: ev.mult.toFixed(1) })}</div>
          </div>
          <span className="event-left">
            {t('event_ends', {
              n: evLeft >= 3600
                ? t('time_hm', { h: Math.floor(evLeft / 3600), m: Math.floor((evLeft % 3600) / 60) })
                : t('time_m', { m: Math.floor(evLeft / 60) }),
            })}
          </span>
        </div>
      )}
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
          disabled={busy === 'golden'}
          onPointerDown={onGoldenClick}
        >
          {busy === 'golden' ? <Spinner /> : '🌟'}
        </button>
      )}

      {floats.map((f) => (
        <span key={f.id} className="click-float" style={{ left: f.x, top: f.y }}>
          {f.text}
        </span>
      ))}

      {/* стартовый чеклист: ведёт новичка руками через все режимы */}
      {tut && !tut.claimed && (
        <div className="card" style={{ width: '100%', marginTop: 18 }}>
          <b>{t('tut_title')}</b>
          <div style={{ marginTop: 8, display: 'flex', flexDirection: 'column', gap: 6 }}>
            {tut.steps.map((s) => (
              <div key={s.key} style={{ fontSize: 14, opacity: s.done ? 0.55 : 1 }}>
                {s.done ? '✅' : '⬜'} {t(`step_${s.key}` as any)}
              </div>
            ))}
          </div>
          {tut.all_done && (
            <button className="btn" style={{ marginTop: 10 }}
                    disabled={busy === 'tutorial'} onClick={claimTutorial}>
              {busy === 'tutorial' ? <Spinner /> : t('tut_claim', { n: fmt(tut.reward) })}
            </button>
          )}
        </div>
      )}

      <div className="card" style={{ width: '100%', marginTop: 18 }}>
        <div className="row" style={{ marginBottom: 8 }}>
          <div>
            <b>{t('click_power')} · {state.user.click_level}</b>
            <div className="hint">
              {/* показываем реальную силу следующего уровня, а не «+N»:
                  она растёт экспоненциально, и +1 было прямой дезинформацией */}
              {clickCapped
                ? t('click_capped')
                : `${t('next_level_click')}: 🍪 ${fmt(state.user.click_power_next ?? 0)} ${t('per_click')}`}
            </div>
          </div>
        </div>
        <button
          className="btn"
          disabled={clickCapped || busy === 'click_up'
            || liveBalance < state.user.click_upgrade_cost}
          onClick={upgrade}
        >
          {busy === 'click_up'
            ? <Spinner />
            : clickCapped
              ? t('click_capped_btn')
              : `${t('upgrade_for')} 🍪 ${fmt(state.user.click_upgrade_cost)}`}
        </button>
      </div>
    </div>
  )
}
