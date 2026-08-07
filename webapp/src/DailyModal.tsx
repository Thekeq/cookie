// Попап ежедневной награды: показывается при входе, если награда не забрана
import { useState } from 'react'
import { api, hapticSuccess } from './api'
import { fmt, useGame } from './App'
import { useFocusTrap } from './a11y'
import { useT, useTErr } from './i18n'
import { sfxError, sfxFanfare } from './sound'
import { useBackButton, useMainButton } from './telegram'
import type { DailyState } from './types'

export default function DailyModal({ daily, onClose }: { daily: DailyState; onClose: () => void }) {
  const t = useT()
  const te = useTErr()
  const { refresh, toast } = useGame()
  const [busy, setBusy] = useState(false)
  // Escape закрывает через ловушку фокуса, нативная «назад» — через слой стека
  const trap = useFocusTrap(onClose)
  useBackButton(onClose, 1)

  const claim = async () => {
    if (busy) return
    setBusy(true)
    try {
      const r = await api.postOnce('/api/daily/claim')
      hapticSuccess()
      sfxFanfare()
      toast(r.freeze_used ? t('streak_frozen') : t('daily_got', { d: r.streak, n: fmt(r.reward) }))
      refresh()
      onClose()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
      onClose()
    }
  }

  // забрать награду можно и нативной кнопкой — она под большим пальцем
  useMainButton(
    {
      text: t('daily_claim', { n: fmt(daily.next_reward) }),
      onClick: claim,
      enabled: !busy,
      progress: busy,
    },
    1,
  )

  // подсвечиваем день, который заберём сейчас (цикл по 7)
  const activeDay = ((daily.next_streak - 1) % 7) + 1

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        ref={trap}
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="daily-title"
        onClick={(e) => e.stopPropagation()}
      >
        <b id="daily-title" style={{ fontSize: 18 }}>{t('daily_title')}</b>
        {daily.streak > 0 && (
          <div style={{ marginTop: 4, color: 'var(--accent)', fontWeight: 700 }}>
            <span aria-hidden="true">🔥</span> {t.plural('daily_streak_n', daily.streak)}
          </div>
        )}
        <div className="hint" style={{ margin: '6px 0 12px' }}>{t('daily_hint')}</div>
        <div className="daily-grid">
          {daily.rewards.map((r) => {
            const done = r.day < activeDay
            const now = r.day === activeDay
            return (
              // состояние дня не только рамкой-цветом: у прошедших ✓, у текущего ▸
              <div
                key={r.day}
                className={'daily-cell' + (now ? ' active' : done ? ' past' : '')}
                aria-label={`${t('daily_day', { n: r.day })}: ${fmt(r.cookies)} — ${
                  now ? t('current_step') : done ? t('step_done') : ''
                }`}
              >
                <div className="hint" style={{ fontSize: 10 }}>
                  <span className="daily-mark" aria-hidden="true">{done ? '✓' : now ? '▸' : ''}</span>
                  {t('daily_day', { n: r.day })}
                </div>
                <div style={{ fontSize: 16 }} aria-hidden="true">🍪</div>
                <b style={{ fontSize: 11 }}>{fmt(r.cookies)}</b>
              </div>
            )
          })}
        </div>
        <button className="btn" style={{ marginTop: 14 }} onClick={claim} disabled={busy}>
          {t('daily_claim', { n: fmt(daily.next_reward) })}
        </button>
      </div>
    </div>
  )
}
