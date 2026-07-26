import { useContext, useEffect, useRef, useState } from 'react'
import { api, openInvoice } from '../api'
import { fmt, useGame } from '../App'
import { LangCtx, useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import type { ShopItem } from '../types'

const ICONS: Record<string, string> = {
  energy_full: '⚡',
  boost_x2_1h: '🔥',
  boost_x2_24h: '🚀',
  cookies_pack: '🍪',
  cookies_crate: '🧺',
  bp_premium: '🎖️',
  offline_cap_6h: '🌙',
  offline_cap_12h: '🌌',
}

export default function ShopTab() {
  const { refresh, toast } = useGame()
  const t = useT()
  const te = useTErr()
  const { lang } = useContext(LangCtx)
  const [items, setItems] = useState<ShopItem[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const timers = useRef<ReturnType<typeof setTimeout>[]>([])

  const loadItems = () => api.get('/api/shop').then((r) => setItems(r.items)).catch(() => {})

  // товары локализует сервер — при смене языка перезапрашиваем
  useEffect(() => {
    loadItems()
  }, [lang])
  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  const buy = async (item: ShopItem) => {
    if (busy) return          // второй тап не должен плодить инвойсы
    setBusy(item.key)
    try {
      const r = await api.post('/api/shop/invoice', { item_key: item.key })
      openInvoice(r.invoice_link, () => {
        sfxBuy()
        toast(t('purchase_ok', { n: item.title }))
        // Выдачу делает бот по successful_payment, и она вполне может прийти
        // позже одного refresh: раньше стоял единственный setTimeout(1500),
        // после которого игрок видел тост «куплено», но не видел покупку.
        // Опрашиваем несколько раз с нарастающей паузой.
        let tries = 0
        const poll = () => {
          tries += 1
          refresh().catch(() => {})
          loadItems()
          if (tries < 5) timers.current.push(setTimeout(poll, 1200 * tries))
        }
        timers.current.push(setTimeout(poll, 900))
      })
    } catch (e: any) {
      sfxError()
      toast(te(e.detail), true)
    } finally {
      setBusy(null)
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
          {it.owned ? (
            <span className="hint" style={{ whiteSpace: 'nowrap' }}>{t('bought')}</span>
          ) : (
            <button
              className="claim-chip"
              style={{ background: 'var(--accent)', color: '#2a1c05', whiteSpace: 'nowrap' }}
              disabled={busy !== null}
              onClick={() => buy(it)}
            >
              ⭐ {it.stars}
            </button>
          )}
        </div>
      ))}
    </div>
  )
}
