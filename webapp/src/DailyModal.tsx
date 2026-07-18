// Попап ежедневной награды: показывается при входе, если награда не забрана
import { useState } from 'react'
import { api, hapticSuccess } from './api'
import { fmt, useGame } from './App'
import { useT } from './i18n'
import { sfxError, sfxFanfare } from './sound'
import type { DailyState } from './types'

export default function DailyModal({ daily, onClose }: { daily: DailyState; onClose: () => void }) {
  const t = useT()
  const { refresh, toast } = useGame()
  const [busy, setBusy] = useState(false)

  const claim = async () => {
    if (busy) return
    setBusy(true)
    try {
      const r = await api.post('/api/daily/claim')
      hapticSuccess()
      sfxFanfare()
      toast(t('daily_got', { d: r.streak, n: fmt(r.reward) }))
      refresh()
      onClose()
    } catch (e: any) {
      sfxError()
      toast(e.detail || t('error'), true)
      onClose()
    }
  }

  // подсвечиваем день, который заберём сейчас (цикл по 7)
  const activeDay = ((daily.next_streak - 1) % 7) + 1

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <b style={{ fontSize: 18 }}>{t('daily_title')}</b>
        {daily.streak > 0 && (
          <div style={{ marginTop: 4, color: 'var(--accent)', fontWeight: 700 }}>
            🔥 {t('daily_streak', { n: daily.streak })}
          </div>
        )}
        <div className="hint" style={{ margin: '6px 0 12px' }}>{t('daily_hint')}</div>
        <div className="daily-grid">
          {daily.rewards.map((r) => (
            <div key={r.day} className={'daily-cell' + (r.day === activeDay ? ' active' : r.day < activeDay ? ' past' : '')}>
              <div className="hint" style={{ fontSize: 10 }}>{t('daily_day', { n: r.day })}</div>
              <div style={{ fontSize: 16 }}>🍪</div>
              <b style={{ fontSize: 11 }}>{fmt(r.cookies)}</b>
            </div>
          ))}
        </div>
        <button className="btn" style={{ marginTop: 14 }} onClick={claim} disabled={busy}>
          {t('daily_claim', { n: fmt(daily.next_reward) })}
        </button>
      </div>
    </div>
  )
}
