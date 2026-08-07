import { useEffect, useRef, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxError, sfxFanfare } from '../sound'
import type { LevelNode } from '../types'
import { COOKIE_SKINS } from '../cookieSkins'
import { useBusy } from '../useBusy'
import Spinner from './Spinner'

export default function LevelsTab() {
  const { state, setState, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const [path, setPath] = useState<LevelNode[] | null>(null)
  const [claimable, setClaimable] = useState<number | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)
  const clipRef = useRef<SVGRectElement>(null)
  // награду за уровень тапают и с кнопки, и с узла тропинки — гасим оба разом
  const { busy, run } = useBusy()

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

  // Анимация «глазурь льётся» сделана ОБРЕЗКОЙ, а не штрихами.
  //
  // Через stroke-dasharray она ломалась: при vector-effect="non-scaling-stroke"
  // браузер считает штрихи в экранных пикселях, а длина пути живёт в
  // координатах viewBox, который растянут по горизонтали и не растянут по
  // вертикали. Числа не сходились, и полив обрывался, не доходя до текущего
  // уровня. Прямоугольник обрезки живёт в тех же user-координатах, что и
  // тропинка, поэтому от растяжения не зависит вовсе.
  useEffect(() => {
    const el = clipRef.current
    if (!el) return
    const h = trailHeight(path?.length || 0)
    el.style.transition = 'none'
    el.style.transform = `translateY(${-h}px)`
    void el.getBoundingClientRect()   // форсируем layout между состояниями
    el.style.transition = 'transform 1.1s ease-out'
    el.style.transform = 'translateY(0)'
  }, [path, state.user.level, state.user.xp])

  const claim = () => run('claim', async () => {
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
  })

  if (!path)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  const nextXp = state.user.xp_next
  // Докуда дотёк полив: индекс текущего уровня + доля XP ВНУТРИ этого уровня.
  // Раньше здесь стояло xp / xp_next, но xp_for_level кумулятивен: у игрока,
  // только что взявшего 8-й уровень, эта дробь равна 0.76, а не 0 — полив
  // уезжал почти к следующему узлу, не отражая реальный прогресс.
  const curIdx = Math.max(0, path.findIndex((n) => n.level === state.user.level))
  const curXp = path[curIdx]?.xp_required ?? 0
  const nextNodeXp = path[curIdx + 1]?.xp_required
  const xpFrac = nextNodeXp && nextNodeXp > curXp
    ? Math.min(1, Math.max(0, (state.user.xp - curXp) / (nextNodeXp - curXp)))
    : 1
  const trail = buildTrail(path.length, curIdx + xpFrac)
  // прогресс внутри уровня — тот же расчёт, что и у полива, чтобы полоска
  // в карточке и тропинка не показывали разные числа
  const xpPct = Math.round(xpFrac * 100)

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
            <div style={{ width: `${xpPct}%` }} />
          </div>
        )}
        {claimable && (
          <button className="btn" style={{ marginTop: 10 }}
                  disabled={busy === 'claim'} onClick={claim}>
            {busy === 'claim' ? <Spinner /> : t('claim_level', { n: claimable })}
          </button>
        )}
      </div>

      {/* Тропинка: печенья на противне, политые глазурью ровно до текущего
          места. Полив льётся не до узла, а до точки между узлами — по XP. */}
      <div className="path-wrap" ref={wrapRef}>
        <svg
          className="path-svg"
          width="100%"
          height={trailHeight(path.length)}
          viewBox={`0 0 100 ${trailHeight(path.length)}`}
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <linearGradient id="glaze-pour" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" style={{ stopColor: 'var(--accent2)' }} />
              <stop offset="1" style={{ stopColor: 'var(--glaze)' }} />
            </linearGradient>
            {/* окно обрезки едет сверху вниз — глазурь как будто наливается */}
            <clipPath id="glaze-clip" clipPathUnits="userSpaceOnUse">
              <rect ref={clipRef} x={-20} y={0} width={140}
                    height={trailHeight(path.length)} />
            </clipPath>
          </defs>
          <path className="path-trail" d={trail.full} vectorEffect="non-scaling-stroke" />
          <path className="path-pour" d={trail.poured} clipPath="url(#glaze-clip)"
                vectorEffect="non-scaling-stroke" />
        </svg>
        {/* капля на острие полива — «ты здесь» */}
        <span className="path-drip"
              style={{ left: `${trail.tip.x}%`, top: `${trail.tip.y}px` }} />

        {path.map((n, i) => {
          const opens = n.unlocks_items.length > 0
          const canTap = claimable === n.level || opens
          return (
            <div
              key={n.level}
              className={
                'level-node' +
                (n.reached ? ' reached' : '') +
                (n.level === state.user.level ? ' current' : '') +
                (claimable === n.level ? ' claimable' : '')
              }
              style={{ marginLeft: `calc(${xForIndex(i)}% - 32px)` }}
              role={canTap ? 'button' : undefined}
              tabIndex={canTap ? 0 : undefined}
              onClick={() => {
                if (claimable === n.level) claim()
                else if (opens)
                  toast(
                    `${t('unlocks', { n: n.level })} ` +
                      n.unlocks_items.map((x) => `${COOKIE_SKINS[x]} ${x}`).join(', '),
                  )
              }}
            >
              <span className="num">
                {busy === 'claim' && claimable === n.level ? <Spinner /> : n.level}
              </span>
              {opens && <span className="sub">{COOKIE_SKINS[n.unlocks_items[0]]}</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

// --- геометрия тропинки ---------------------------------------------------
// Узел 64px + отступ 46px, свои 10px сверху у .path-wrap: центр i-го узла
// лежит на y = 42 + i * 110. По x тропинка вьётся змейкой по трём колонкам.
const STRIDE = 110
const FIRST_Y = 42
const COLS = [22, 50, 78, 50]

function xForIndex(i: number): number {
  return COLS[i % 4]
}
function yForIndex(i: number): number {
  return FIRST_Y + i * STRIDE
}
function trailHeight(count: number): number {
  return 40 + count * STRIDE
}

interface P { x: number; y: number }

// Разрез кубической кривой в точке t (Де Кастельжо): нужен, чтобы полив
// обрывался ровно на доле XP, а не прыгал от узла к узлу.
function cutCubic(p0: P, c1: P, c2: P, p3: P, t: number) {
  const mid = (a: P, b: P): P => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t })
  const a = mid(p0, c1), b = mid(c1, c2), c = mid(c2, p3)
  const d = mid(a, b), e = mid(b, c)
  return { c1: a, c2: d, end: mid(d, e) }
}

// pos — позиция вдоль тропинки в единицах «узлов» (2.4 = 40% пути от 3-го к 4-му)
function buildTrail(count: number, pos: number) {
  const at = (i: number): P => ({ x: xForIndex(i), y: yForIndex(i) })
  // управляющие точки уводим по вертикали — на входе и выходе из печенья
  // кривая идёт строго вверх-вниз, поэтому змейка выходит плавной
  const ctrl = (i: number): [P, P] => [
    { x: xForIndex(i), y: yForIndex(i) + STRIDE * 0.5 },
    { x: xForIndex(i + 1), y: yForIndex(i + 1) - STRIDE * 0.5 },
  ]
  const seg = (i: number) => {
    const [c1, c2] = ctrl(i), p = at(i + 1)
    return ` C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${p.x} ${p.y}`
  }

  const start = at(0)
  let full = `M ${start.x} ${start.y}`
  for (let i = 0; i < count - 1; i++) full += seg(i)

  const clamped = Math.max(0, Math.min(count - 1, pos))
  const whole = Math.floor(clamped)
  const t = clamped - whole
  let poured = `M ${start.x} ${start.y}`
  for (let i = 0; i < whole; i++) poured += seg(i)
  let tip = at(whole)
  if (t > 0.001 && whole < count - 1) {
    const [c1, c2] = ctrl(whole)
    const cut = cutCubic(at(whole), c1, c2, at(whole + 1), t)
    poured += ` C ${cut.c1.x} ${cut.c1.y} ${cut.c2.x} ${cut.c2.y} ${cut.end.x} ${cut.end.y}`
    tip = cut.end
  }
  return { full, poured, tip }
}
