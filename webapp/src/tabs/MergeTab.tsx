import { useEffect, useRef, useState } from 'react'
import { api, haptic, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxMerge } from '../sound'
import { COOKIE_SKINS } from '../cookieSkins'

// Альбом блестящих печенек: 24 слота, наборы дают постоянный бонус к доходу
function AlbumModal({ onClose }: { onClose: () => void }) {
  const t = useT()
  const [data, setData] = useState<any>(null)

  useEffect(() => {
    api.get('/api/collection').then(setData).catch(() => {})
  }, [])

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <b style={{ fontSize: 17 }}>{t('album_title')}</b>
        {data && (
          <>
            <div className="hint" style={{ marginTop: 6 }}>
              {t('album_hint', { n: Math.round(data.set_bonus * 100) })}
            </div>
            <div style={{ marginTop: 4, fontWeight: 700, color: 'var(--good)' }}>
              {t('album_bonus_now', { n: Math.round((data.multiplier - 1) * 100) })}
            </div>
            {data.sets.map((s: any) => (
              <div key={s.from} style={{ marginTop: 10 }}>
                <div className="row">
                  <span className="hint">
                    {t('set_label', { a: s.from, b: s.to })} · {s.have}/{s.need}
                    {s.done && ' ✅'}
                  </span>
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4, marginTop: 4 }}>
                  {Array.from({ length: s.to - s.from + 1 }, (_, i) => s.from + i).map((lvl: number) => {
                    const owned = data.owned.includes(lvl)
                    return (
                      <span
                        key={lvl}
                        style={{
                          fontSize: 24, width: 34, height: 34, borderRadius: 8,
                          display: 'flex', alignItems: 'center', justifyContent: 'center',
                          background: owned ? 'rgba(240,166,59,0.18)' : 'var(--card)',
                          filter: owned ? 'none' : 'grayscale(1) opacity(0.35)',
                          outline: owned ? '1px solid var(--accent)' : 'none',
                        }}
                      >
                        {COOKIE_SKINS[lvl]}
                      </span>
                    )
                  })}
                </div>
              </div>
            ))}
            <div className="hint" style={{ marginTop: 10 }}>
              {t('album_pity', { n: Math.max(1, data.pity_at - data.pity) })}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

interface Drag {
  from: number
  level: number
  x: number // координаты пальца внутри доски (для призрака)
  y: number
  over: number | null // клетка под пальцем
  overTrash: boolean // палец над мусоркой
  moved: boolean // палец реально сдвинулся (отличаем от случайного тапа)
}

export default function MergeTab() {
  const { state, setState, toast, liveBalance, flushClicks } = useGame()
  const t = useT()
  const te = useTErr()
  const [drag, setDrag] = useState<Drag | null>(null)
  const [popCell, setPopCell] = useState<number | null>(null)
  const [shinyCell, setShinyCell] = useState<number | null>(null)
  const [buyLevel, setBuyLevel] = useState(1) // уровень покупаемой печеньки
  const [showAlbum, setShowAlbum] = useState(false)
  const busy = useRef(false)
  const boardRef = useRef<HTMLDivElement>(null)
  const trashRef = useRef<HTMLDivElement>(null)

  const boardMap = new Map(state.board.map((b) => [b.cell, b.item_level]))
  const cellsOpen = state.board_cells?.unlocked ?? 25
  // занятые ОТКРЫТЫЕ клетки: печеньки в закрытых (legacy) не блокируют спавн
  const openBusy = state.board.filter((b) => b.cell < cellsOpen).length
  const boardFull = openBusy >= cellsOpen

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
        if (s.shiny) {
          // золотая подсветка: сразу видно, что выпала блестяшка в альбом
          setShinyCell(to)
          setTimeout(() => setShinyCell(null), 1800)
          toast(t('shiny_drop'))
        } else if (s.merged_level >= 5)
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

  // печенька в мусорку/переплавку: клетка свободна, кэшбек частью цены
  const doTrash = async (cell: number) => {
    if (busy.current) return
    busy.current = true
    try {
      const s = await api.post('/api/merge/trash', { cell })
      setState(s)
      hapticSuccess()
      sfxBuy()
      toast(t('trash_done', { n: fmt(s.trash_refund || 0) }))
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

  const inTrash = (clientX: number, clientY: number): boolean => {
    const rect = trashRef.current?.getBoundingClientRect()
    if (!rect) return false
    return clientX >= rect.left && clientX <= rect.right &&
      clientY >= rect.top && clientY <= rect.bottom
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
      over: null, overTrash: false, moved: false,
    })
  }

  const onDragMove = (e: React.PointerEvent) => {
    if (!drag) return
    const rect = boardRef.current!.getBoundingClientRect()
    let over = cellAt(e.clientX, e.clientY)
    // пустая закрытая клетка — не цель: сервер всё равно откажет
    if (over !== null && over >= cellsOpen && !boardMap.has(over)) over = null
    setDrag({
      ...drag,
      x: e.clientX - rect.left, y: e.clientY - rect.top,
      over: over === drag.from ? null : over,
      overTrash: inTrash(e.clientX, e.clientY),
      moved: true,
    })
  }

  const onDragEnd = (e: React.PointerEvent) => {
    if (!drag) return
    boardRef.current?.releasePointerCapture(e.pointerId)
    const target = cellAt(e.clientX, e.clientY)
    const { from, moved } = drag
    const dropTrash = inTrash(e.clientX, e.clientY)
    setDrag(null)
    if (!moved) return
    if (dropTrash) {
      doTrash(from)
      return
    }
    // дроп на другую открытую/занятую клетку — ход
    if (target !== null && target !== from &&
        !(target >= cellsOpen && !boardMap.has(target))) doMove(from, target)
  }

  const spawn = async () => {
    try {
      await flushClicks() // сервер должен знать про все тапы до проверки цены
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

  // как открыть следующие клетки: уровень и/или друзья
  const cells = state.board_cells
  const nextRef = cells?.ref_cells?.find((r) => !r.done)

  return (
    <div>
      <div className="card">
        <div className="row">
          <div>
            <b>{t('passive_income')}</b>
            <div className="hint">{t('passive_hint')}</div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontWeight: 800, color: 'var(--good)' }}>
              +{fmt(state.passive_per_hour)}{t('per_hour')}
            </div>
            <button
              className="claim-chip"
              style={{ marginTop: 4, padding: '5px 10px' }}
              onClick={() => setShowAlbum(true)}
            >
              {t('album')}
            </button>
          </div>
        </div>
      </div>

      {showAlbum && <AlbumModal onClose={() => setShowAlbum(false)} />}

      <div
        className="board"
        ref={boardRef}
        onPointerMove={onDragMove}
        onPointerUp={onDragEnd}
        onPointerCancel={() => setDrag(null)}
      >
        {Array.from({ length: 25 }, (_, i) => {
          const lvl = boardMap.get(i)
          const locked = i >= cellsOpen && !lvl
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
                (locked ? ' locked' : '') +
                (isSource ? ' drag-source' : '') +
                (isOver ? (mergeOk ? ' drop-ok' : ' drop-over') : '') +
                (popCell === i ? ' merge-pop' : '') +
                (shinyCell === i ? ' shiny-pop' : '')
              }
              onPointerDown={(e) => onDragStart(e, i)}
            >
              {lvl && (
                <>
                  <span className="cookie-item" style={isSource ? { opacity: 0.25 } : undefined}>
                    {COOKIE_SKINS[lvl]}
                  </span>
                  <span className="item-lvl">{lvl}</span>
                  {shinyCell === i && <span className="shiny-spark">✨</span>}
                </>
              )}
              {locked && <span className="cell-lock">🔒</span>}
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

      {/* мусорка-печь: появляется во время перетаскивания, дроп = переплавка */}
      <div
        ref={trashRef}
        className={
          'trash-zone' + (drag?.moved ? ' show' : '') + (drag?.overTrash ? ' over' : '')
        }
      >
        🔥 {t('trash_zone', { n: Math.round((cells?.trash_refund ?? 0.1) * 100) })}
      </div>

      {/* прогресс открытия клеток: уровнями и друзьями */}
      {cells && cells.unlocked < cells.total && (
        <div className="hint" style={{ textAlign: 'center', marginBottom: 8 }}>
          🔒 {t('cells_count', { a: cells.unlocked, b: cells.total })}
          {cells.next_unlock_level && <> · {t('cell_next_lvl', { n: cells.next_unlock_level })}</>}
          {nextRef && <> · {t('cell_next_ref', { n: nextRef.friends })}</>}
        </div>
      )}

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
        disabled={boardFull || liveBalance < buyCost}
      >
        {boardFull
          ? t('board_full')
          : `${t('buy_cookie')} ${COOKIE_SKINS[safeBuyLevel]} ${safeBuyLevel} · 🍪 ${fmt(buyCost)}`}
      </button>
      <div className="hint" style={{ textAlign: 'center', marginTop: 8 }}>
        {t('merge_hint')} {COOKIE_SKINS[state.max_item_unlocked]} {state.max_item_unlocked}
      </div>
    </div>
  )
}
