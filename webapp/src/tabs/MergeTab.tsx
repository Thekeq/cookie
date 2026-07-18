import { useRef, useState } from 'react'
import { api, haptic, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT } from '../i18n'
import { sfxBuy, sfxError, sfxMerge } from '../sound'

// эмодзи-скины печенек по уровням (1..12)
const COOKIE_SKINS = ['', '🍪', '🥠', '🧁', '🍩', '🎂', '🍰', '🥮', '🍮', '🍫', '🍯', '👑', '💎']

export default function MergeTab() {
  const { state, setState, toast } = useGame()
  const t = useT()
  const [selected, setSelected] = useState<number | null>(null)
  const [popCell, setPopCell] = useState<number | null>(null)
  const busy = useRef(false)

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
      toast(e.detail || t('error'), true)
    } finally {
      busy.current = false
    }
  }

  const onCellTap = (cell: number) => {
    const has = boardMap.has(cell)
    if (selected === null) {
      if (has) {
        setSelected(cell)
        haptic('light')
      }
      return
    }
    if (selected === cell) {
      setSelected(null)
      return
    }
    const from = selected
    setSelected(null)
    doMove(from, cell)
  }

  const spawn = async () => {
    try {
      const s = await api.post('/api/merge/spawn')
      setState(s)
      haptic('medium')
      sfxBuy()
    } catch (e: any) {
      sfxError()
      toast(e.detail || t('error'), true)
    }
  }

  const selLvl = selected !== null ? boardMap.get(selected) : null

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

      <div className="board">
        {Array.from({ length: 25 }, (_, i) => {
          const lvl = boardMap.get(i)
          const dropOk =
            selected !== null && selected !== i && lvl !== undefined && lvl === selLvl
          return (
            <div
              key={i}
              className={
                'cell' +
                (selected === i ? ' selected' : '') +
                (dropOk ? ' drop-ok' : '') +
                (popCell === i ? ' merge-pop' : '')
              }
              onPointerDown={() => onCellTap(i)}
            >
              {lvl && (
                <>
                  <span className="cookie-item">{COOKIE_SKINS[lvl]}</span>
                  <span className="item-lvl">{lvl}</span>
                </>
              )}
            </div>
          )
        })}
      </div>

      <button
        className="btn"
        onClick={spawn}
        disabled={state.board.length >= 25 || state.user.cookies < state.spawn_cost}
      >
        {state.board.length >= 25
          ? t('board_full')
          : `${t('buy_cookie')} 🍪 ${fmt(state.spawn_cost)}`}
      </button>
      <div className="hint" style={{ textAlign: 'center', marginTop: 8 }}>
        {t('merge_hint')} {COOKIE_SKINS[state.max_item_unlocked]} {state.max_item_unlocked}
      </div>
    </div>
  )
}
