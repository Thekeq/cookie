// Ежедневные задания (3 в день, реролл 1/день); заказы пекарни — своя вкладка
import { useEffect, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxFanfare } from '../sound'
import type { Quest } from '../types'

const METRIC_ICO: Record<string, string> = {
  clicks: '👆', merges: '🧩', spawns: '🍪', buildings: '🏭', earned: '💰', make_item: '⭐',
}

export default function QuestsTab() {
  const t = useT()
  const te = useTErr()
  const { refresh, toast } = useGame()
  const [quests, setQuests] = useState<Quest[]>([])
  const [rerollLeft, setRerollLeft] = useState(false)

  const load = () => {
    api.get('/api/quests').then((r) => {
      setQuests(r.quests)
      setRerollLeft(!!r.reroll_available)
    })
  }
  useEffect(() => {
    load()
  }, [])

  const claim = async (key: string) => {
    try {
      const r = await api.post('/api/quests/claim', { key })
      hapticSuccess()
      sfxFanfare()
      toast(t('quest_reward_got', { n: fmt(r.reward_cookies), x: fmt(r.reward_bp_xp) }))
      setQuests(r.quests)
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const reroll = async (key: string) => {
    try {
      const r = await api.post('/api/quests/reroll', { key })
      sfxBuy()
      setQuests(r.quests)
      setRerollLeft(false)
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const questText = (q: Quest) => t(`q_${q.metric}` as any, { n: fmt(q.goal) })

  return (
    <div>
      <div className="card">
        <b>{t('quests_title')}</b>
        <div className="hint" style={{ marginTop: 4 }}>{t('quests_hint')}</div>
      </div>
      {quests.map((q) => (
        <div className="card ach" key={q.key}>
          <span className="ico">{q.claimed ? '✅' : q.done ? '🎁' : METRIC_ICO[q.metric] || '📋'}</span>
          <div className="grow">
            <b style={{ fontSize: 14 }}>{questText(q)}</b>
            <div className="hint">
              🍪 {fmt(q.reward_cookies)} · 🎖️ {fmt(q.reward_bp_xp)} XP
              {rerollLeft && !q.done && !q.claimed && (
                <>
                  {' · '}
                  <span
                    style={{ color: 'var(--accent)', cursor: 'pointer' }}
                    onClick={() => reroll(q.key)}
                  >
                    🎲 {t('quest_reroll')}
                  </span>
                </>
              )}
            </div>
            <div className="progress-bar" style={{ marginTop: 5 }}>
              <div style={{ width: `${(q.progress / q.goal) * 100}%` }} />
            </div>
          </div>
          <button className="claim-chip" disabled={!q.done || q.claimed} onClick={() => claim(q.key)}>
            {q.claimed ? '✓' : `${fmt(q.progress)}/${fmt(q.goal)}`}
          </button>
        </div>
      ))}
    </div>
  )
}
