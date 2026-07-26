// Дуэль пекарей: асинхронный забег 1x1 на сутки.
//
// Лидерборд — витрина: чужие числа видно, сделать с ними нельзя ничего.
// Дуэль даёт конкретного соперника и понятный вопрос «кто больше напечёт
// за сутки». Обоим не нужно быть онлайн одновременно.
import { useEffect, useState } from 'react'
import { api, hapticSuccess } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError, sfxFanfare } from '../sound'
import { useBusy } from '../useBusy'

interface Duel {
  id: number
  status: 'waiting' | 'active' | 'done'
  ends_at: number
  my_score: number
  foe_score: number
  foe_name: string | null
  foe_level: number | null
  won: boolean | null
  reward: number
  claimed: boolean
}

export default function DuelTab() {
  const t = useT()
  const te = useTErr()
  const { toast, refresh } = useGame()
  const [duel, setDuel] = useState<Duel | null>(null)
  const [hours, setHours] = useState(24)
  const [loaded, setLoaded] = useState(false)
  const { busy, run } = useBusy()

  const load = () =>
    api.get('/api/duel').then((r) => {
      setDuel(r.duel)
      setHours(r.hours)
      setLoaded(true)
    }).catch(() => setLoaded(true))

  useEffect(() => {
    load()
    // счёт соперника меняется независимо от нас — держим сцену свежей
    const timer = setInterval(load, 20_000)
    return () => clearInterval(timer)
  }, [])

  const act = (key: string, path: string, ok?: () => void) =>
    run(key, async () => {
      try {
        const r = await api.post(path)
        setDuel(r.duel)
        ok?.()
      } catch (e: any) {
        sfxError()
        toast(te(e.detail), true)
      }
    })

  const claim = () =>
    run('claim', async () => {
      try {
        const r = await api.post('/api/duel/claim')
        setDuel(r.duel)
        if (r.won) {
          hapticSuccess()
          sfxFanfare()
          toast(t('duel_won_toast', { n: fmt(r.reward) }))
        } else {
          toast(t('duel_lost_toast'))
        }
        refresh()
      } catch (e: any) {
        sfxError()
        toast(te(e.detail), true)
      }
    })

  if (!loaded) return null

  // нет дуэли — приглашение начать
  if (!duel)
    return (
      <div className="card duel-card">
        <div className="duel-hero">🥊</div>
        <b>{t('duel_title')}</b>
        <div className="hint" style={{ margin: '6px 0 12px' }}>
          {t('duel_hint', { n: hours })}
        </div>
        <button className="btn" disabled={busy !== null}
                onClick={() => act('find', '/api/duel/find', sfxBuy)}>
          {t('duel_find')}
        </button>
      </div>
    )

  if (duel.status === 'waiting')
    return (
      <div className="card duel-card">
        <div className="duel-hero searching">🔍</div>
        <b>{t('duel_searching')}</b>
        <div className="hint" style={{ margin: '6px 0 12px' }}>{t('duel_searching_hint')}</div>
        <button className="btn secondary" disabled={busy !== null}
                onClick={() => act('cancel', '/api/duel/cancel')}>
          {t('duel_cancel')}
        </button>
      </div>
    )

  const total = Math.max(1, duel.my_score + duel.foe_score)
  const myShare = (duel.my_score / total) * 100
  const leading = duel.my_score >= duel.foe_score

  return (
    <div className="card duel-card">
      <div className="duel-vs">
        <div className={'duel-side' + (leading ? ' lead' : '')}>
          <div className="duel-who">{t('duel_you')}</div>
          <div className="duel-score">{fmt(duel.my_score)}</div>
        </div>
        <div className="duel-x">VS</div>
        <div className={'duel-side' + (!leading ? ' lead' : '')}>
          <div className="duel-who">{duel.foe_name}</div>
          <div className="duel-score">{fmt(duel.foe_score)}</div>
        </div>
      </div>

      {/* полоса перетягивания: видно, кто впереди, без чтения чисел */}
      <div className="duel-bar">
        <div className="duel-bar-me" style={{ width: `${myShare}%` }} />
      </div>

      {duel.status === 'active' && (
        <div className="hint" style={{ textAlign: 'center', marginTop: 10 }}>
          {t('duel_ends', {
            n: Math.max(0, Math.round((duel.ends_at - Date.now() / 1000) / 3600)),
          })}
        </div>
      )}

      {duel.status === 'done' && !duel.claimed && (
        <button className="btn" style={{ marginTop: 12 }} disabled={busy !== null}
                onClick={claim}>
          {duel.won ? t('duel_claim', { n: fmt(duel.reward) }) : t('duel_close')}
        </button>
      )}
    </div>
  )
}
