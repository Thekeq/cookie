import { useRef, useState } from 'react'
import { api, haptic, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxMerge } from '../sound'
import { COOKIE_SKINS } from '../cookieSkins'

interface Drag {
  from: number
  level: number
  x: number // координаты пальца внутри доски (для призрака)
  y: number
  over: number | null // клетка под пальцем
  moved: boolean // палец реально сдвинулся (отличаем от случайного тапа)
}

export default function MergeTab() {
  const { state, setState, toast, liveBalance } = useGame()
  const t = useT()
  const te = useTErr()
  const [drag, setDrag] = useState<Drag | null>(null)
  const [popCell, setPopCell] = useState<number | null>(null)
  const [buyLevel, setBuyLevel] = useState(1) // уровень покупаемой печеньки
  const busy = useRef(false)
  const boardRef = useRef<HTMLDivElement>(null)

  const boardMap = new Map(state.board.map((b) => [b.cell, b.item_level]))

  const doMove = async (from: number, to: number) => {
    if (busy.current) return
    busy.current = true
    try {
      const s = await api.post('/api/merge/move', { from_cell: from, to_cell: to })
      setState(s)
      if (s.merged_level) {
        hapticSuccess()
        sfxMerge(s.merged_level)
        setPopCell(to)
        setTimeout(() => setPopCell(null), 350)
        if (s.merged_level >= 5)
          toast(`${t('merged_lvl', { n: s.merged_level })} ${COOKIE_SKINS[s.merged_level]}`)
      } else {
        haptic('light')
      }
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    } finally {
      busy.current = false
    }
  }

  // клетка по координатам пальца (сетка 5x5 c gap 6px — считаем по границам доски)
  const cellAt = (clientX: number, clientY: number): number | null => {
    const rect = boardRef.current?.getBoundingClientRect()
    if (!rect) return null
    const x = clientX - rect.left
    const y = clientY - rect.top
    if (x < 0 || y < 0 || x >= rect.width || y >= rect.height) return null
    const col = Math.min(4, Math.floor((x / rect.width) * 5))
    const row = Math.min(4, Math.floor((y / rect.height) * 5))
    return row * 5 + col
  }

  const onDragStart = (e: React.PointerEvent, cell: number) => {
    const lvl = boardMap.get(cell)
    if (!lvl || busy.current) return
    e.preventDefault()
    // захватываем поинтер на доску: события идут к нам, скролл не мешает
    boardRef.current?.setPointerCapture(e.pointerId)
    haptic('light')
    const rect = boardRef.current!.getBoundingClientRect()
    setDrag({
      from: cell, level: lvl,
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      over: null, moved: false,
    })
  }

  const onDragMove = (e: React.PointerEvent) => {
    if (!drag) return
    const rect = boardRef.current!.getBoundingClientRect()
    const over = cellAt(e.clientX, e.clientY)
    setDrag({
      ...drag,
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      over: over === drag.from ? null : over,
      moved: true,
    })
  }

  const onDragEnd = (e: React.PointerEvent) => {
    if (!drag) return
    boardRef.current?.releasePointerCapture(e.pointerId)
    const target = cellAt(e.clientX, e.clientY)
    const { from, moved } = drag
    setDrag(null)
    // дроп на другую клетку после реального движения — ход
    if (moved && target !== null && target !== from) doMove(from, target)
  }

  const spawn = async () => {
    try {
      const s = await api.post('/api/merge/spawn', { level: buyLevel })
      setState(s)
      haptic('medium')
      sfxBuy()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const maxDirect = state.spawn_direct?.max_level || 1
  const safeBuyLevel = Math.min(buyLevel, maxDirect)
  const buyCost = state.spawn_direct?.costs?.[String(safeBuyLevel)] ?? state.spawn_cost

  return (
    <div>
      <div className="card">
        <div className="row">
          <div>
            <b>{t('passive_income')}</b>
            <div className="hint">{t('passive_hint')}</div>
          </div>
          <div style={{ fontWeight: 800, color: 'var(--good)' }}>
            +{fmt(state.passive_per_hour)}/ч
          </div>
        </div>
      </div>

      <div
        className="board"
        ref={boardRef}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={() => setDrag(null)}
      >
        {Array.from({ length: 25 }, (_, i) => {
          const lvl = boardMap.get(i)
          const isSource = drag?.from === i
          const isOver = drag?.over === i
          // подсказка во время перетаскивания: одинаковый уровень = сольются
          const mergeOk = isOver && lvl !== undefined && lvl === drag!.level
          return (
            <div
              key={i}
              className={
                'cell' +
                (lvl ? ' has-item' : '') +
                (isSource ? ' drag-source' : '') +
                (isOver ? (mergeOk ? ' drop-ok' : ' drop-over') : '') +
                (popCell === i ? ' merge-pop' : '')
              }
              onPointerDown={(e) => onDragStart(e, i)}
            >
              {lvl && (
                <>
                  <span className="cookie-item" style={isSource ? { opacity: 0.25 } : undefined}>
                    {COOKIE_SKINS[lvl]}
                  </span>
                  <span className="item-lvl">{lvl}</span>
                </>
              )}
            </div>
          )
        })}

        {/* призрак: печенька летит за пальцем */}
        {drag && drag.moved && (
          <span className="drag-ghost" style={{ left: drag.x, top: drag.y }}>
            {COOKIE_SKINS[drag.level]}
          </span>
        )}
      </div>

      {/* выбор уровня покупаемой печеньки: топ-тиры только слиянием */}
      {maxDirect > 1 && (
        <div className="spawn-levels">
          {Array.from({ length: maxDirect }, (_, i) => i + 1).map((l) => (
            <button
              key={l}
              className={'spawn-lvl' + (safeBuyLevel === l ? ' active' : '')}
              onClick={() => setBuyLevel(l)}
            >
              <span>{COOKIE_SKINS[l]}</span>
              <span className="spawn-lvl-n">{l}</span>
            </button>
          ))}
        </div>
      )}

      <button
        className="btn"
        onClick={spawn}
        disabled={state.board.length >= 25 || liveBalance < buyCost}
      >
        {state.board.length >= 25
          ? t('board_full')
          : `${t('buy_cookie')} ${COOKIE_SKINS[safeBuyLevel]} ${safeBuyLevel} · 🍪 ${fmt(buyCost)}`}
      </button>
      <div className="hint" style={{ textAlign: 'center', marginTop: 8 }}>
        {t('merge_hint')} {COOKIE_SKINS[state.max_item_unlocked]} {state.max_item_unlocked}
      </div>
    </div>
  )
}
