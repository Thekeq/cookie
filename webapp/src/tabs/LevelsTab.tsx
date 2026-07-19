import { useEffect, useRef, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxError, sfxFanfare } from '../sound'
import type { LevelNode } from '../types'
import { COOKIE_SKINS } from '../cookieSkins'

export default function LevelsTab() {
  const { state, setState, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const [path, setPath] = useState<LevelNode[] | null>(null)
  const [claimable, setClaimable] = useState<number | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  const load = () =>
    api.get('/api/levels').then((r) => {
      setPath(r.path)
      setClaimable(r.claimable)
    })

  useEffect(() => {
    load()
  }, [])

  // скроллим к текущему уровню
  useEffect(() => {
    if (path && wrapRef.current) {
      const el = wrapRef.current.querySelector('.level-node.current')
      el?.scrollIntoView({ block: 'center' })
    }
  }, [path])

  const claim = async () => {
    try {
      const s = await api.post('/api/levels/claim')
      setState(s)
      hapticSuccess()
      sfxFanfare()
      if (s.level_up)
        toast(`${t('level_up', { n: s.level_up.level })} +${fmt(s.level_up.reward.cookies)} 🍪`)
      load()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  if (!path)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  const nextXp = state.user.xp_next

  return (
    <div>
      <div className="card">
        <div className="row" style={{ marginBottom: 6 }}>
          <b>{t('level')} {state.user.level}</b>
          <span className="hint">
            {fmt(state.user.xp)}
            {nextXp ? ` / ${fmt(nextXp)} XP` : ` ${t('xp_max')}`}
          </span>
        </div>
        {nextXp && (
          <div className="progress-bar">
            <div style={{ width: `${Math.min(100, (state.user.xp / nextXp) * 100)}%` }} />
          </div>
        )}
        {claimable && (
          <button className="btn" style={{ marginTop: 10 }} onClick={claim}>
            {t('claim_level', { n: claimable })}
          </button>
        )}
      </div>

      {/* тропинка: ноды зигзагом, соединённые пунктирной линией */}
      <div className="path-wrap" ref={wrapRef}>
        <svg className="path-svg" preserveAspectRatio="none">
          {path.map((n, i) => {
            if (i === path.length - 1) return null
            const y1 = i * 110 + 42
            const y2 = (i + 1) * 110 + 42
            const x1 = xForIndex(i)
            const x2 = xForIndex(i + 1)
            return (
              <line
                key={n.level}
                x1={`${x1}%`}
                y1={y1}
                x2={`${x2}%`}
                y2={y2}
                stroke={path[i + 1].reached ? 'var(--accent)' : 'rgba(255,255,255,0.15)'}
                strokeWidth="3"
                strokeDasharray="6 6"
              />
            )
          })}
        </svg>
        {path.map((n, i) => (
          <div
            key={n.level}
            className={
              'level-node' +
              (n.reached ? ' reached' : '') +
              (n.level === state.user.level ? ' current' : '') +
              (claimable === n.level ? ' claimable' : '')
            }
            style={{ marginLeft: `calc(${xForIndex(i)}% - 32px)` }}
            onClick={() => {
              if (claimable === n.level) claim()
              else if (n.unlocks_items.length)
                toast(
                  `${t('unlocks', { n: n.level })} ` +
                    n.unlocks_items.map((x) => `${COOKIE_SKINS[x]} ${x}`).join(', '),
                )
            }}
          >
            <span className="num">{n.reached ? '✓' : n.level}</span>
            {n.unlocks_items.length > 0 && (
              <span className="sub">{COOKIE_SKINS[n.unlocks_items[0]]}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

// зигзаг тропинки: 25% → 50% → 75% → 50% → 25% ...
function xForIndex(i: number): number {
  const seq = [25, 50, 75, 50]
  return seq[i % 4]
}
