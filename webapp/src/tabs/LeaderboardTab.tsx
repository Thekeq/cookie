import { useEffect, useState } from 'react'
import { api, shareRefLink } from '../api'
import { fmt, useGame } from '../App'
import { useT } from '../i18n'

const BOT_USERNAME = (import.meta as any).env?.VITE_BOT_USERNAME || 'YourCookieBot'

interface LBRow {
  rank: number
  user_id: number
  name: string
  level: number
  season_earned: number
  is_me: boolean
  prize: number
}

interface LBData {
  top: LBRow[]
  me: { rank: number | null; season_earned: number }
  players_total: number
  season: number
  season_ends_at: number
  top_rewards: Record<string, number>
  last_result: { season_id: number; rank: number; reward_cookies: number } | null
}

const MEDALS = ['🥇', '🥈', '🥉']

export default function LeaderboardTab() {
  const t = useT()
  const { state, toast } = useGame()
  const [data, setData] = useState<LBData | null>(null)

  useEffect(() => {
    api.get('/api/leaderboard').then(setData).catch((e) => toast(e.detail || t('error'), true))
  }, [])

  if (!data)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  // человекочитаемый остаток сезона: "3д 14ч" / "5ч"
  const leftSec = Math.max(0, data.season_ends_at - Date.now() / 1000)
  const leftD = Math.floor(leftSec / 86400)
  const leftH = Math.floor((leftSec % 86400) / 3600)
  const leftStr =
    (leftD > 0 ? t('days_short', { n: leftD }) + ' ' : '') + t('hours_short', { n: leftH })

  return (
    <div>
      <div className="card">
        <div className="row">
          <b>{t('lb_title')}</b>
          <span className="hint">{t('season_num', { n: data.season })}</span>
        </div>
        <div style={{ marginTop: 4, fontWeight: 700, color: 'var(--accent)' }}>
          ⏳ {t('season_ends', { n: leftStr })}
        </div>
        <div className="hint" style={{ marginTop: 4 }}>{t('lb_season_hint')}</div>
        {data.me.rank && (
          <div className="row" style={{ marginTop: 8 }}>
            <span style={{ fontWeight: 700 }}>
              {t('lb_your_rank', { n: data.me.rank, m: data.players_total })}
            </span>
            <button
              className="claim-chip"
              onClick={() =>
                shareRefLink(BOT_USERNAME, state.user.user_id, t('share_rank_text', { n: data.me.rank! }))
              }
            >
              {t('share_ach')}
            </button>
          </div>
        )}
        {data.last_result && data.last_result.reward_cookies > 0 && (
          <div className="hint" style={{ marginTop: 4 }}>
            🏆 {t('lb_last_season', { r: data.last_result.rank, n: fmt(data.last_result.reward_cookies) })}
          </div>
        )}
      </div>

      {data.top.map((r) => (
        <div
          className="card ach"
          key={r.user_id}
          style={{
            padding: '10px 14px',
            marginBottom: 6,
            outline: r.is_me ? '2px solid var(--accent)' : 'none',
          }}
        >
          <span style={{ width: 34, textAlign: 'center', fontSize: r.rank <= 3 ? 22 : 14, fontWeight: 800 }}>
            {MEDALS[r.rank - 1] || `#${r.rank}`}
          </span>
          <div className="grow">
            <b style={{ fontSize: 14 }}>
              {r.name} {r.is_me && <span style={{ color: 'var(--accent)' }}>· {t('lb_you')}</span>}
            </b>
            <div className="hint">
              {t('level')} {r.level}
              {r.prize > 0 && <span> · 🎁 {fmt(r.prize)}</span>}
            </div>
          </div>
          <b style={{ whiteSpace: 'nowrap' }}>🍪 {fmt(r.season_earned)}</b>
        </div>
      ))}
    </div>
  )
}
