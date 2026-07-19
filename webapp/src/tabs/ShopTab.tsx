import { useEffect, useState } from 'react'
import { api, openInvoice } from '../api'
import { fmt, useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import type { ShopItem } from '../types'

const ICONS: Record<string, string> = {
  energy_full: '⚡',
  boost_x2_1h: '🔥',
  boost_x2_24h: '🚀',
  cookies_pack: '🍪',
  cookies_crate: '🧺',
  bp_premium: '🎖️',
}

export default function ShopTab() {
  const { refresh, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const [items, setItems] = useState<ShopItem[]>([])

  useEffect(() => {
    api.get('/api/shop').then((r) => setItems(r.items))
  }, [])

  const buy = async (item: ShopItem) => {
    try {
      const r = await api.post('/api/shop/invoice', { item_key: item.key })
      openInvoice(r.invoice_link, () => {
        sfxBuy()
        toast(t('purchase_ok', { n: item.title }))
        setTimeout(refresh, 1500)
      })
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    }
  }

  return (
    <div>
      <div className="hint" style={{ textAlign: 'center', marginBottom: 10 }}>
        {t('stars_hint')}
      </div>
      {items.map((it) => (
        <div className="card row" key={it.key}>
          <div style={{ fontSize: 30 }}>{ICONS[it.key] || '🎁'}</div>
          <div style={{ flex: 1 }}>
            <b>{it.title}</b>
            <div className="hint">{it.desc}</div>
            {it.amount != null && (
              <div style={{ fontWeight: 800, color: 'var(--good)', fontSize: 13 }}>
                ≈ +{fmt(it.amount)} 🍪
              </div>
            )}
          </div>
          <button
            className="claim-chip"
            style={{ background: 'var(--accent)', color: '#2a1c05', whiteSpace: 'nowrap' }}
            onClick={() => buy(it)}
          >
            ⭐ {it.stars}
          </button>
        </div>
      ))}
    </div>
  )
}
