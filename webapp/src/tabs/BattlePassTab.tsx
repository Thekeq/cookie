import { useEffect, useState } from 'react'
import { api, hapticSuccess, openInvoice } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import type { BPLevel } from '../types'

interface BPData {
  season: number
  season_ends_at: number
  bp_xp: number
  bp_level: number
  xp_in_level: number
  xp_per_level: number
  premium: boolean
  premium_price_stars: number
  levels: BPLevel[]
}

export default function BattlePassTab() {
  const { refresh, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const [bp, setBp] = useState<BPData | null>(null)

  const load = () => api.get('/api/battlepass').then(setBp)
  useEffect(() => {
    load()
  }, [])

  const claim = async (level: number, track: 'free' | 'premium') => {
    try {
      const r = await api.post('/api/battlepass/claim', { level, track })
      hapticSuccess()
      sfxBuy()
      toast(`+${fmt(r.reward.cookies)} 🍪`)
      load()
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  const buyPremium = async () => {
    try {
      const r = await api.post('/api/shop/invoice', { item_key: 'bp_premium' })
      openInvoice(r.invoice_link, () => {
        toast(t('bp_premium_on'))
        setTimeout(() => {
          load()
          refresh()
        }, 1500)
      })
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  if (!bp)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  const progressInLevel = bp.xp_in_level ?? bp.bp_xp - bp.bp_level * bp.xp_per_level

  return (
    <div>
      <div className="card">
        <div className="row" style={{ marginBottom: 6 }}>
          <b>{t('bp_title', { n: bp.season })}</b>
          <span className="hint">{t('level')} {bp.bp_level}/30</span>
        </div>
        <div className="progress-bar">
          <div style={{ width: `${Math.min(100, (progressInLevel / bp.xp_per_level) * 100)}%` }} />
        </div>
        <div className="hint" style={{ marginTop: 4 }}>
          {fmt(Math.max(0, progressInLevel))} / {fmt(bp.xp_per_level)} {t('bp_to_next')}
        </div>
        {bp.season_ends_at > 0 && (() => {
          const leftSec = Math.max(0, bp.season_ends_at - Date.now() / 1000)
          const d = Math.floor(leftSec / 86400)
          const h = Math.floor((leftSec % 86400) / 3600)
          const s = (d > 0 ? t('days_short', { n: d }) + ' ' : '') + t('hours_short', { n: h })
          return (
            <div style={{ marginTop: 6, fontWeight: 700, color: 'var(--accent)', fontSize: 13 }}>
              ⏳ {t('season_ends', { n: s })}
            </div>
          )
        })()}
        {!bp.premium && (
          <button className="btn" style={{ marginTop: 10 }} onClick={buyPremium}>
            {t('bp_buy')} ⭐ {bp.premium_price_stars}
          </button>
        )}
      </div>

      <div className="row hint" style={{ padding: '0 4px 6px', fontWeight: 700 }}>
        <span style={{ width: 44 }}></span>
        <span style={{ flex: 1, textAlign: 'center' }}>{t('bp_free')}</span>
        <span style={{ flex: 1, textAlign: 'center' }}>Premium {bp.premium ? '✅' : '🔒'}</span>
      </div>

      <div className="bp-track">
        {bp.levels.map((l) => (
          <div className="bp-level" key={l.level}>
            <div className={'bp-num' + (l.reached ? ' reached' : '')}>{l.level}</div>
            <div
              className={
                'bp-reward' +
                (l.free_claimed ? ' claimed' : l.reached ? ' claimable' : ' locked')
              }
              onClick={() => l.reached && !l.free_claimed && claim(l.level, 'free')}
            >
              🍪 {fmt(l.free.cookies)}
              {l.free_claimed ? ' ✓' : ''}
            </div>
            <div
              className={
                'bp-reward' +
                (l.premium_claimed
                  ? ' claimed'
                  : l.reached && bp.premium
                    ? ' claimable'
                    : ' locked')
              }
              onClick={() => l.reached && bp.premium && !l.premium_claimed && claim(l.level, 'premium')}
            >
              🍪 {fmt(l.premium.cookies)}
              {l.premium.energy ? ` +⚡${l.premium.energy}` : ''}
              {l.premium_claimed ? ' ✓' : ''}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
