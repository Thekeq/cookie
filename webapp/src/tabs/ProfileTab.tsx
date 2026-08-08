import { useContext, useEffect, useState } from 'react'
import { api, hapticSuccess, shareRefLink } from '../api'
import { fmt, useGame } from '../App'
import { LANGS, LangCtx, useT, useTErr } from '../i18n'
import {
  isMusicOn, isSfxOn, sfxBuy, sfxError, sfxFanfare, toggleMusic, toggleSfx,
} from '../sound'
import { tg } from '../telegram'
import type { Achievement, RefMilestone } from '../types'
import { useBusy } from '../useBusy'
import PrestigeModal from '../PrestigeModal'
import Spinner from './Spinner'

export default function ProfileTab() {
  const { state, refresh, toast, refLink } = useGame()
  const t = useT()
  const te = useTErr()
  const { lang, setLang } = useContext(LangCtx)
  const [achs, setAchs] = useState<Achievement[]>([])
  const [refs, setRefs] = useState<{
    count: number; reward_referrer: number; reward_referred: number; milestones: RefMilestone[]
  } | null>(null)
  const [channel, setChannel] = useState<{ channel: string; reward: number; claimed: boolean } | null>(null)
  const [promo, setPromo] = useState('')
  const [sfx, setSfx] = useState(isSfxOn())
  const [music, setMusic] = useState(isMusicOn())

  const load = () => {
    api.get('/api/achievements').then((r) => setAchs(r.achievements))
    api.get('/api/referrals').then(setRefs)
    api.get('/api/channel').then(setChannel).catch(() => {})
  }
  // перезагружаем при смене языка: ачивки приходят с сервера уже переведёнными
  useEffect(() => {
    load()
  }, [lang])

  const { busy, run } = useBusy()

  const claimAch = (key: string) => run(key, async () => {
    try {
      const r = await api.post('/api/achievements/claim', { key })
      hapticSuccess()
      sfxFanfare()
      toast(t('ach_reward', { n: fmt(r.reward) }))
      load()
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  const claimMilestone = (key: string) => run('ms:' + key, async () => {
    try {
      await api.post('/api/referrals/milestone', { key })
      hapticSuccess()
      sfxFanfare()
      toast(t('ref_ms_got'))
      load()
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  const claimChannel = () => run('channel', async () => {
    try {
      const r = await api.post('/api/channel/claim')
      hapticSuccess()
      sfxFanfare()
      toast(t('channel_got', { n: fmt(r.reward) }))
      load()
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  // window.confirm для необратимого действия — чужой шрифт, чужие кнопки и
  // ни слова о том, что сгорит, а что останется. Заменено сценой-модалкой.
  const [prestigeAsk, setPrestigeAsk] = useState(false)

  // Модалка закрывается сразу, а запрос ещё летит — спиннер поэтому уезжает
  // на кнопку престижа под ней, иначе сброс выглядел бы как «ничего не было».
  const doPrestige = () => run('prestige', async () => {
    const p = state.prestige
    if (!p?.can_prestige) return
    setPrestigeAsk(false)
    try {
      const s = await api.post('/api/prestige')
      hapticSuccess()
      sfxFanfare()
      toast(t('prestige_done', {
        c: s.prestige.count,
        m: s.prestige_result.multiplier.toFixed(2),
      }))
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  const shareAch = (title: string) => {
    shareRefLink(refLink, t('share_ach_text', { a: title }))
  }

  const openChannel = () => {
    if (!channel?.channel) return
    const url = `https://t.me/${channel.channel}`
    if (tg?.openTelegramLink) tg.openTelegramLink(url)
    else window.open(url, '_blank')
  }

  const redeemPromo = () => run('promo', async () => {
    if (!promo.trim()) return
    try {
      const r = await api.post('/api/promo/redeem', { code: promo })
      hapticSuccess()
      sfxBuy()
      toast(t('promo_ok', { n: fmt(r.reward_cookies) }))
      setPromo('')
      refresh()
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  })

  return (
    <div>
      <div className="card">
        <b style={{ fontSize: 17 }}>
          {state.user.first_name || state.user.username || t('player')}
        </b>
        <div className="stat-grid" style={{ marginTop: 10 }}>
          <div className="stat-box">
            <div className="v">{fmt(state.user.total_clicks)}</div>
            <div className="k">{t('clicks')}</div>
          </div>
          <div className="stat-box">
            <div className="v">{fmt(state.user.total_merges)}</div>
            <div className="k">{t('merges')}</div>
          </div>
          <div className="stat-box">
            <div className="v">{state.user.level}</div>
            <div className="k">{t('level')}</div>
          </div>
          <div className="stat-box">
            <div className="v">{refs?.count ?? 0}</div>
            <div className="k">{t('friends')}</div>
          </div>
        </div>
      </div>

      {/* настройки: язык и звук */}
      <div className="card">
        <div className="row" style={{ marginBottom: 10 }}>
          <b>{t('language')}</b>
          <div style={{ display: 'flex', gap: 6 }}>
            {LANGS.map((l) => (
              <button
                key={l.code}
                className="claim-chip"
                style={{
                  background: lang === l.code ? 'var(--accent)' : 'var(--card)',
                  color: lang === l.code ? '#2a1c05' : 'var(--text)',
                }}
                onClick={() => setLang(l.code)}
              >
                {l.flag}
              </button>
            ))}
          </div>
        </div>
        <div className="row" style={{ marginBottom: 10 }}>
          <b>{t('sound')}</b>
          <button className="claim-chip" onClick={() => setSfx(toggleSfx())}>
            {sfx ? `🔊 ${t('sound_on')}` : `🔇 ${t('sound_off')}`}
          </button>
        </div>
        <div className="row">
          <b>{t('music')}</b>
          <button className="claim-chip" onClick={() => setMusic(toggleMusic())}>
            {music ? `🎵 ${t('sound_on')}` : `🔇 ${t('sound_off')}`}
          </button>
        </div>
      </div>

      <div className="card">
        <b>{t('invite_title')}</b>
        <div className="hint" style={{ margin: '6px 0 10px' }}>
          {t('invite_hint', { a: fmt(refs?.reward_referrer ?? 1000), b: fmt(refs?.reward_referred ?? 500) })}
        </div>
        {/* сервер не отдал ссылку — бот не настроен; гасим кнопку, чтобы промах
            в настройке был виден, а не расходился битыми приглашениями */}
        <button className="btn" disabled={!refLink}
                onClick={() => shareRefLink(refLink, t('share_text'))}>
          {t('share_link')}
        </button>

        {/* milestone-награды: 3 / 10 / 25 друзей */}
        {refs && refs.milestones?.length > 0 && (
          <div style={{ marginTop: 12 }}>
            <b style={{ fontSize: 14 }}>{t('ref_milestones')}</b>
            {refs.milestones.map((m) => (
              <div className="row" key={m.key} style={{ padding: '7px 0', fontSize: 13 }}>
                <div className="grow">
                  <b>{t('ref_ms_friends', { n: m.count })}</b> · {t(`ref_ms_${m.type === 'bp_premium' ? 'premium' : m.type}` as any)}
                  <div className="progress-bar" style={{ marginTop: 4 }}>
                    <div style={{ width: `${(m.progress / m.count) * 100}%` }} />
                  </div>
                </div>
                <button
                  className="claim-chip"
                  style={{ marginLeft: 10 }}
                  disabled={!m.done || m.claimed || busy === 'ms:' + m.key}
                  onClick={() => claimMilestone(m.key)}
                >
                  {busy === 'ms:' + m.key
                    ? <Spinner />
                    : m.claimed ? '✓' : `${m.progress}/${m.count}`}
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* престиж: открывается после 10M заработанных */}
      <div className="card">
        <b>{t('prestige_title')}</b>
        <div className="hint" style={{ margin: '6px 0 10px' }}>
          {t('prestige_hint', { p: Math.round((state.prestige?.mult_per_point || 0.02) * 100) })}
        </div>
        {state.prestige?.points > 0 && (
          <div style={{ fontWeight: 700, color: 'var(--accent)', marginBottom: 8 }}>
            {t('prestige_now', { n: state.prestige.points, m: state.prestige.multiplier.toFixed(2) })}
          </div>
        )}
        {state.prestige?.can_prestige ? (
          <button className="btn" disabled={busy === 'prestige'}
                  onClick={() => setPrestigeAsk(true)}>
            {busy === 'prestige'
              ? <Spinner />
              : <>{t('prestige_gain', { n: state.prestige.gain_available })} ✨</>}
          </button>
        ) : (
          <div className="hint">
            {t('prestige_locked', { n: fmt(state.prestige?.min_earned || 10_000_000) })}
          </div>
        )}
      </div>

      {/* подписка на канал (если настроен CHANNEL_USERNAME) */}
      {channel?.channel && !channel.claimed && (
        <div className="card">
          <b>{t('channel_title')}</b>
          <div className="hint" style={{ margin: '6px 0 10px' }}>
            {t('channel_hint', { n: fmt(channel.reward) })}
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn secondary" style={{ flex: 1 }} onClick={openChannel}>
              {t('channel_open')}
            </button>
            <button className="btn" style={{ flex: 1 }}
                    disabled={busy === 'channel'} onClick={claimChannel}>
              {busy === 'channel' ? <Spinner /> : t('channel_check')}
            </button>
          </div>
        </div>
      )}

      <div className="card">
        <b>{t('promo_title')}</b>
        <div style={{ display: 'flex', gap: 8, marginTop: 10 }}>
          <input
            className="field"
            style={{ marginBottom: 0, flex: 1 }}
            placeholder={t('promo_placeholder')}
            value={promo}
            onChange={(e) => setPromo(e.target.value.toUpperCase())}
          />
          <button className="claim-chip" disabled={busy === 'promo'} onClick={redeemPromo}>
            {busy === 'promo' ? <Spinner /> : 'OK'}
          </button>
        </div>
      </div>

      <b style={{ display: 'block', margin: '14px 4px 8px' }}>{t('achievements')}</b>
      {achs.map((a) => (
        <div className="card ach" key={a.key}>
          <span className="ico">{a.claimed ? '✅' : a.done ? '🎁' : '🔒'}</span>
          <div className="grow">
            <b style={{ fontSize: 14 }}>{a.title}</b>
            <div className="hint">{a.desc}</div>
            <div className="progress-bar" style={{ marginTop: 5 }}>
              <div style={{ width: `${(a.progress / a.goal) * 100}%` }} />
            </div>
          </div>
          {a.claimed ? (
            <button className="claim-chip" onClick={() => shareAch(a.title)}>
              {t('share_ach')}
            </button>
          ) : (
            <button className="claim-chip" disabled={!a.done || busy === a.key}
                    onClick={() => claimAch(a.key)}>
              {busy === a.key ? <Spinner /> : `🍪 ${fmt(a.reward)}`}
            </button>
          )}
        </div>
      ))}

      {prestigeAsk && state.prestige && (
        <PrestigeModal
          points={state.prestige.gain_available}
          multiplier={state.prestige.next_multiplier ?? state.prestige.multiplier}
          keptLevel={state.prestige.kept_level ?? 1}
          level={state.user.level}
          onConfirm={doPrestige}
          onClose={() => setPrestigeAsk(false)}
        />
      )}
    </div>
  )
}
