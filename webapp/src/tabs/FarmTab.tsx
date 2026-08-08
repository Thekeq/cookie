import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmt, fmtRate, useGame } from '../App'
import { formatIncome } from '../format'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import { askConfirm } from '../telegram'
import type { FarmState } from '../types'
import { useBusy } from '../useBusy'
import RecipePanel from './RecipePanel'
import Spinner from './Spinner'

const B_ICONS: Record<string, string> = {
  cursor: '👆', granny: '👵', bakery: '🏠', conveyor: '🛗', factory: '🏭',
  mine: '⛏️', shipment: '🚚', reactor: '☢️', portal: '🌀', cloud: '☁️',
  timelab: '⏳', moonbase: '🌙', quantum: '🔮', galaxy: '🌌',
  singularity: '🕳️', multiverse: '♾️',
}
const U_ICONS: Record<string, string> = {
  click_mult: '💪', farm_mult: '🏭', energy_cap: '🔋', energy_regen: '⚡', passive_mult: '🧩',
}

// Покупка дороже половины кошелька — уже не «случайный тап»: переспрашиваем.
const EXPENSIVE_SHARE = 0.5

export default function FarmTab() {
  const { refresh, toast, liveBalance, flushClicks } = useGame()
  const t = useT()
  const te = useTErr()
  const [farm, setFarm] = useState<FarmState | null>(null)
  const [section, setSection] = useState<'buildings' | 'upgrades' | 'skins'>('buildings')
  // покупка списывает деньги: второй тап по той же кнопке не должен улететь
  const { busy, run } = useBusy()

  useEffect(() => {
    api.get('/api/farm').then((f: FarmState) => {
      setFarm(f)
      if (f.collected > 1) {
        toast(`${t('farm_income')}: +${fmt(f.collected)} 🍪`)
        refresh() // доход уже в БД: без синка шапка полминуты врала бы
      }
    })
  }, [])

  // once — покупка постройки: у неё на сервере есть токен идемпотентности,
  // поэтому оборванный ответ можно переспросить, не купив вторую.
  // bkey — под каким ключом крутится спиннер: у здания, апгрейда и скина
  // ключи из разных пространств имён и вполне могут совпасть.
  // ask — подпись покупки; передаётся только там, где списываются печеньки,
  // и диалог всплывает, лишь когда цена ощутима на фоне баланса
  const post = (bkey: string, path: string, key: string, once = false, ask?: {
    title: string; cost: number
  }) =>
    run(bkey, async () => {
      if (ask && ask.cost > liveBalance * EXPENSIVE_SHARE) {
        if (!(await askConfirm(`${t('buy')}: ${ask.title} · 🍪 ${fmt(ask.cost)}`))) return
      }
      try {
        await flushClicks() // сервер должен знать про все тапы до проверки цены
        const f = once ? await api.postOnce(path, { key }) : await api.post(path, { key })
        setFarm(f)
        sfxBuy()
        refresh()
      } catch (e: any) {
        sfxError()
        toast(te(e.detail), true)
      }
    })

  if (!farm)
    return (
      <div className="loading-screen" style={{ height: 200 }}>
        <span className="spin">🍪</span>
      </div>
    )

  const upgradeName = (u: { effect: string; value: number }) => {
    switch (u.effect) {
      case 'click_mult': return t('u_click_mult', { n: u.value })
      case 'farm_mult': return t('u_farm_mult', { n: u.value })
      case 'energy_cap': return t('u_energy_cap', { n: u.value })
      case 'energy_regen': return t('u_energy_regen', { n: u.value })
      case 'passive_mult': return t('u_passive_mult', { n: u.value })
      default: return u.effect
    }
  }

  return (
    <div>
      <div className="card">
        <div className="row">
          <div>
            <b>{t('farm_title')}</b>
            <div className="hint">{t('farm_hint', { n: farm.offline_cap_hours })}</div>
          </div>
          <div style={{ fontWeight: 800, color: 'var(--good)', whiteSpace: 'nowrap' }}>
            +{formatIncome(farm.cps)}
          </div>
        </div>
      </div>

      {/* закваска стоит рядом с оффлайн-доходом: это про одно и то же время */}
      <RecipePanel />

      <div className="row" style={{ marginBottom: 10, gap: 6 }}>
        {(['buildings', 'upgrades', 'skins'] as const).map((s) => (
          <button
            key={s}
            className="btn secondary"
            style={{
              padding: '9px 0', fontSize: 13,
              outline: section === s ? '2px solid var(--accent)' : 'none',
            }}
            onClick={() => setSection(s)}
          >
            {t(s)}
          </button>
        ))}
      </div>

      {section === 'buildings' &&
        farm.buildings.map((b) => (
          <div className="card ach" key={b.key}>
            <span className="ico">{B_ICONS[b.key] || '🏗️'}</span>
            <div className="grow">
              <b style={{ fontSize: 14 }}>
                {t(('b_' + b.key) as any)} {b.owned > 0 && <span className="hint">×{b.owned}</span>}
              </b>
              {/* за копию — в секундах: это цена вопроса «взять ещё одну».
                  Итог по зданию — в обеих единицах, чтобы его можно было
                  сложить глазами с доходом доски, который считается в часах */}
              <div className="hint">
                +{fmtRate(b.cps_each)}{t('per_sec')}
                {b.owned > 0 && ` (= ${formatIncome(b.cps_each * b.owned)})`}
              </div>
            </div>
            {b.maxed ? (
              <span className="hint" style={{ fontSize: 12 }}>{t('farm_maxed', { n: b.max_copies })}</span>
            ) : b.unlocked ? (
              <button className="claim-chip"
                      disabled={busy === 'b:' + b.key || liveBalance < b.cost}
                      onClick={() => post('b:' + b.key, '/api/farm/buy_building', b.key, true,
                        { title: t(('b_' + b.key) as any), cost: b.cost })}>
                {busy === 'b:' + b.key ? <Spinner /> : <>🍪 {fmt(b.cost)}</>}
              </button>
            ) : (
              <span className="hint" style={{ fontSize: 12 }}>
                🔒 {b.lock === 'record'
                  ? t('req_record', { n: b.req_record })
                  : t('req_level', { n: b.req_level })}
              </span>
            )}
          </div>
        ))}

      {section === 'upgrades' &&
        farm.upgrades.map((u) => (
          <div className="card ach" key={u.key}>
            <span className="ico">{U_ICONS[u.effect] || '⭐'}</span>
            <div className="grow">
              <b style={{ fontSize: 14 }}>{upgradeName(u)}</b>
              {!u.unlocked && <div className="hint">🔒 {t('req_level', { n: u.req_level })}</div>}
            </div>
            {u.owned ? (
              <span className="hint">{t('bought')}</span>
            ) : (
              <button
                className="claim-chip"
                disabled={!u.unlocked || busy === 'u:' + u.key || liveBalance < u.cost}
                onClick={() => post('u:' + u.key, '/api/farm/buy_upgrade', u.key, false,
                  { title: upgradeName(u), cost: u.cost })}
              >
                {busy === 'u:' + u.key ? <Spinner /> : <>🍪 {fmt(u.cost)}</>}
              </button>
            )}
          </div>
        ))}

      {section === 'skins' && (
        <div className="stat-grid">
          {farm.skins.map((s) => (
            <div className="stat-box" key={s.key} style={{ position: 'relative' }}>
              <div style={{ fontSize: 38 }}>{s.emoji}</div>
              {s.active ? (
                <div className="hint" style={{ color: 'var(--good)' }}>✓ {t('applied')}</div>
              ) : s.owned ? (
                <button className="claim-chip" style={{ marginTop: 4 }}
                        disabled={busy === 'apply:' + s.key}
                        onClick={() => post('apply:' + s.key, '/api/farm/set_skin', s.key)}>
                  {busy === 'apply:' + s.key ? <Spinner /> : t('apply')}
                </button>
              ) : s.unlocked ? (
                <button className="claim-chip" style={{ marginTop: 4 }}
                        disabled={busy === 'skin:' + s.key || liveBalance < s.cost}
                        onClick={() => post('skin:' + s.key, '/api/farm/buy_skin', s.key, false,
                          { title: s.emoji, cost: s.cost })}>
                  {busy === 'skin:' + s.key ? <Spinner /> : <>🍪 {fmt(s.cost)}</>}
                </button>
              ) : (
                <div className="hint">🔒 {t('req_level', { n: s.req_level })}</div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
