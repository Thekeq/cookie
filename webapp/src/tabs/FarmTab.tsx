import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import type { FarmState } from '../types'
import RecipePanel from './RecipePanel'

const B_ICONS: Record<string, string> = {
  cursor: '👆', granny: '👵', bakery: '🏠', conveyor: '🛗', factory: '🏭',
  mine: '⛏️', shipment: '🚚', reactor: '☢️', portal: '🌀', cloud: '☁️',
  timelab: '⏳', moonbase: '🌙', quantum: '🔮', galaxy: '🌌',
  singularity: '🕳️', multiverse: '♾️',
}
const U_ICONS: Record<string, string> = {
  click_mult: '💪', farm_mult: '🏭', energy_cap: '🔋', energy_regen: '⚡', passive_mult: '🧩',
}

export default function FarmTab() {
  const { refresh, toast, liveBalance, flushClicks } = useGame()
  const t = useT()
  const te = useTErr()
  const [farm, setFarm] = useState<FarmState | null>(null)
  const [section, setSection] = useState<'buildings' | 'upgrades' | 'skins'>('buildings')

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
  // поэтому оборванный ответ можно переспросить, не купив вторую
  const post = async (path: string, key: string, once = false) => {
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
  }

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
            +{fmt(farm.cps)}/s
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
              <div className="hint">
                +{fmt(b.cps_each)}/s {b.owned > 0 && `(= ${fmt(b.cps_each * b.owned)}/s)`}
              </div>
            </div>
            {b.maxed ? (
              <span className="hint" style={{ fontSize: 12 }}>{t('farm_maxed', { n: b.max_copies })}</span>
            ) : b.unlocked ? (
              <button className="claim-chip" disabled={liveBalance < b.cost}
                      onClick={() => post('/api/farm/buy_building', b.key, true)}>
                🍪 {fmt(b.cost)}
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
                disabled={!u.unlocked || liveBalance < u.cost}
                onClick={() => post('/api/farm/buy_upgrade', u.key)}
              >
                🍪 {fmt(u.cost)}
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
                        onClick={() => post('/api/farm/set_skin', s.key)}>
                  {t('apply')}
                </button>
              ) : s.unlocked ? (
                <button className="claim-chip" style={{ marginTop: 4 }}
                        disabled={liveBalance < s.cost}
                        onClick={() => post('/api/farm/buy_skin', s.key)}>
                  🍪 {fmt(s.cost)}
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
