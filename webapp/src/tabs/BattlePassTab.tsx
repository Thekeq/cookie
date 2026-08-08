import { useEffect, useState } from 'react'
import { api, hapticSuccess, openInvoice } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import type { TFunc } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import type { BPLevel, BPLockedPremium, BPReward } from '../types'
import { useBusy } from '../useBusy'
import Spinner from './Spinner'

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
  locked_premium: BPLockedPremium
}

/**
 * Ненулевые части награды строками: «🍪 12K», «+⚡100», «+🔲1», «+⏳3ч», «+🎨».
 *
 * Только иконки и числа: в треке две колонки по половине экрана, и слово
 * «клетка» не влезает в них ни на одном из трёх языков. Тот же список идёт в
 * тост после клейма — там подписи не нужны, потому что игрок только что видел
 * ту же ячейку и узнаёт её по иконкам.
 */
function rewardParts(r: BPReward, t: TFunc): string[] {
  const parts: string[] = []
  if (r.cookies) parts.push(`🍪 ${fmt(r.cookies)}`)
  if (r.energy) parts.push(`+⚡${r.energy}`)
  if (r.cells) parts.push(`+🔲${r.cells}`)
  if (r.offline_hours) parts.push(`+⏳${t('hours_short', { n: r.offline_hours })}`)
  if (r.skin) parts.push('+🎨')
  return parts
}

export default function BattlePassTab() {
  const { refresh, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const [bp, setBp] = useState<BPData | null>(null)
  // награда уровня и покупка премиума — по одному запросу за раз
  const { busy, run } = useBusy()

  const load = () => api.get('/api/battlepass').then(setBp)
  useEffect(() => {
    load()
  }, [])

  const bpKey = (level: number, track: 'free' | 'premium') => `${track}:${level}`

  const claim = (level: number, track: 'free' | 'premium') =>
    run(bpKey(level, track), async () => {
      try {
        const r = await api.postOnce('/api/battlepass/claim', { level, track })
        hapticSuccess()
        sfxBuy()
        // Подмену клетки на печенье обязательно проговариваем: без подписи игрок
        // видит печеньки там, где в треке нарисована клетка, и считает это багом.
        toast(rewardParts(r.reward, t).join(' ')
          + (r.reward.cookies_substituted ? ` · ${t('bp_cells_maxed')}` : ''))
        load()
        refresh()
      } catch (e: any) {
        sfxError()
        toast(te(e.detail), true)
      }
    })

  const buyPremium = () => run('premium', async () => {
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
  })

  if (!bp)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  const progressInLevel = bp.xp_in_level ?? bp.bp_xp - bp.bp_level * bp.xp_per_level

  // Накопленное за уже взятые уровни — момент конверсии: пас продаёт не «награды
  // когда-нибудь», а вот эту готовую пачку, которая выдаётся в секунду покупки.
  // Здесь карточка широкая, поэтому в отличие от трека пишем словами.
  // Дефолт, а не bp.locked_premium напрямую: у клиента, открытого до выкладки
  // сервера с этим полем, вкладка иначе падала бы целиком на lp.levels
  const lp: BPLockedPremium = bp.locked_premium ?? {
    levels: 0, cookies: 0, energy: 0, cells: 0, offline_hours: 0, skins: 0,
  }
  const lockedItems: string[] = []
  if (lp.cookies) lockedItems.push(`🍪 ${fmt(lp.cookies)}`)
  if (lp.energy) lockedItems.push(`⚡ +${lp.energy}`)
  if (lp.cells) lockedItems.push(`🔲 ${t.plural('bp_n_cells', lp.cells)}`)
  if (lp.offline_hours) lockedItems.push(`⏳ ${t('bp_offline_cap', { n: lp.offline_hours })}`)
  if (lp.skins) lockedItems.push(`🎨 ${t.plural('bp_n_skins', lp.skins)}`)

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
        {/* пока копить нечего — обычная кнопка; иначе продаёт карточка ниже */}
        {!bp.premium && lp.levels === 0 && (
          <button className="btn" style={{ marginTop: 10 }}
                  disabled={busy === 'premium'} onClick={buyPremium}>
            {busy === 'premium'
              ? <Spinner />
              : <>{t('bp_buy')} ⭐ {bp.premium_price_stars}</>}
          </button>
        )}
      </div>

      {!bp.premium && lp.levels > 0 && (
        <div className="card bp-offer">
          <b>{t('bp_locked_title')}</b>
          <div className="levels">{t.plural('bp_n_levels', lp.levels)}</div>
          <div className="hint" style={{ marginTop: 6 }}>{t('bp_locked_hint')}</div>
          <ul>
            {lockedItems.map((it) => <li key={it}>{it}</li>)}
          </ul>
          <button className="btn" style={{ marginTop: 12 }}
                  disabled={busy === 'premium'} onClick={buyPremium}>
            {busy === 'premium'
              ? <Spinner />
              : <>{t('bp_buy')} ⭐ {bp.premium_price_stars}</>}
          </button>
        </div>
      )}

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
              {busy === bpKey(l.level, 'free') ? <Spinner /> : (
                <>
                  {rewardParts(l.free, t).map((p) => <span key={p}>{p}</span>)}
                  {l.free_claimed ? <span>✓</span> : null}
                </>
              )}
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
              {busy === bpKey(l.level, 'premium') ? <Spinner /> : (
                <>
                  {rewardParts(l.premium, t).map((p) => <span key={p}>{p}</span>)}
                  {l.premium_claimed ? <span>✓</span> : null}
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
