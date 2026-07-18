import { useEffect, useState } from 'react'
import { api } from '../api'
import { fmt, useGame } from '../App'

interface Promo {
  code: string
  reward_cookies: number
  reward_energy: number
  max_uses: number
  uses: number
  active: number
}

interface Source {
  code: string
  title: string
  registrations: number
  link: string
}

interface Stats {
  users_total: number
  users_new_24h: number
  users_new_7d: number
  active_24h: number
  referrals_total: number
  purchases_count: number
  stars_earned: number
  by_source: { src: string; c: number }[]
}

export default function AdminTab() {
  const { toast } = useGame()
  const [stats, setStats] = useState<Stats | null>(null)
  const [promos, setPromos] = useState<Promo[]>([])
  const [sources, setSources] = useState<Source[]>([])

  const [pCode, setPCode] = useState('')
  const [pCookies, setPCookies] = useState('1000')
  const [pUses, setPUses] = useState('0')
  const [sCode, setSCode] = useState('')
  const [sTitle, setSTitle] = useState('')
  const [bcText, setBcText] = useState('')
  const [bcBusy, setBcBusy] = useState(false)

  const load = () => {
    api.get('/api/admin/stats').then(setStats)
    api.get('/api/admin/promo').then((r) => setPromos(r.promo_codes))
    api.get('/api/admin/sources').then((r) => setSources(r.sources))
  }
  useEffect(() => {
    load()
  }, [])

  const createPromo = async () => {
    try {
      await api.post('/api/admin/promo', {
        code: pCode,
        reward_cookies: Number(pCookies) || 0,
        max_uses: Number(pUses) || 0,
      })
      toast('Промокод создан ✅')
      setPCode('')
      load()
    } catch (e: any) {
      toast(e.detail || 'Ошибка', true)
    }
  }

  const togglePromo = async (code: string, active: boolean) => {
    await api.post('/api/admin/promo/toggle', { code, active })
    load()
  }

  const createSource = async () => {
    try {
      const r = await api.post('/api/admin/sources', { code: sCode, title: sTitle })
      toast('Ссылка создана ✅')
      navigator.clipboard?.writeText(r.link).catch(() => {})
      setSCode('')
      setSTitle('')
      load()
    } catch (e: any) {
      toast(e.detail || 'Ошибка', true)
    }
  }

  const sendBroadcast = async (test: boolean) => {
    if (!bcText.trim() || bcBusy) return
    setBcBusy(true)
    try {
      const r = await api.post('/api/admin/broadcast', { text: bcText, test })
      toast(test ? 'Превью отправлено тебе 📨' : `Разослано: ${r.sent} ✅ (блок: ${r.blocked}, ошибки: ${r.failed})`)
      if (!test) setBcText('')
    } catch (e: any) {
      toast(e.detail || 'Ошибка', true)
    } finally {
      setBcBusy(false)
    }
  }

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text)
    toast('Скопировано 📋')
  }

  return (
    <div>
      {stats && (
        <div className="card">
          <b>📊 Статистика</b>
          <div className="stat-grid" style={{ marginTop: 10 }}>
            <div className="stat-box">
              <div className="v">{stats.users_total}</div>
              <div className="k">всего юзеров</div>
            </div>
            <div className="stat-box">
              <div className="v">{stats.active_24h}</div>
              <div className="k">актив 24ч</div>
            </div>
            <div className="stat-box">
              <div className="v">+{stats.users_new_24h}</div>
              <div className="k">новых 24ч</div>
            </div>
            <div className="stat-box">
              <div className="v">+{stats.users_new_7d}</div>
              <div className="k">новых 7д</div>
            </div>
            <div className="stat-box">
              <div className="v">{stats.referrals_total}</div>
              <div className="k">рефералов</div>
            </div>
            <div className="stat-box">
              <div className="v">⭐ {stats.stars_earned}</div>
              <div className="k">{stats.purchases_count} покупок</div>
            </div>
          </div>
          <div style={{ marginTop: 10 }}>
            <div className="hint" style={{ marginBottom: 4 }}>По источникам:</div>
            {stats.by_source.map((s) => (
              <div className="row" key={s.src} style={{ fontSize: 13, padding: '2px 0' }}>
                <span>{s.src}</span>
                <b>{s.c}</b>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="card">
        <b>📨 Рассылка всем игрокам</b>
        <div className="hint" style={{ marginTop: 4 }}>
          Сообщение уйдёт через бота. HTML-теги поддерживаются (&lt;b&gt;, &lt;i&gt;).
        </div>
        <textarea
          className="field"
          style={{ marginTop: 10, minHeight: 70, resize: 'vertical', fontFamily: 'inherit' }}
          placeholder={'🍪 Новое обновление!\nПромокод COOKIE500 на 500 печенек…'}
          value={bcText}
          onChange={(e) => setBcText(e.target.value)}
        />
        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn secondary" style={{ flex: 1 }} disabled={!bcText.trim() || bcBusy}
                  onClick={() => sendBroadcast(true)}>
            Тест (себе)
          </button>
          <button className="btn" style={{ flex: 1 }} disabled={!bcText.trim() || bcBusy}
                  onClick={() => sendBroadcast(false)}>
            {bcBusy ? 'Отправка…' : 'Разослать всем'}
          </button>
        </div>
      </div>

      <div className="card">
        <b>🎟️ Новый промокод</b>
        <input className="field" style={{ marginTop: 10 }} placeholder="КОД" value={pCode}
               onChange={(e) => setPCode(e.target.value.toUpperCase())} />
        <div style={{ display: 'flex', gap: 8 }}>
          <input className="field" placeholder="Печеньки" value={pCookies}
                 onChange={(e) => setPCookies(e.target.value)} inputMode="numeric" />
          <input className="field" placeholder="Макс. использований (0=∞)" value={pUses}
                 onChange={(e) => setPUses(e.target.value)} inputMode="numeric" />
        </div>
        <button className="btn" onClick={createPromo} disabled={!pCode.trim()}>
          Создать
        </button>
        {promos.map((p) => (
          <div className="row" key={p.code} style={{ padding: '8px 0', fontSize: 13 }}>
            <div>
              <b>{p.code}</b> · 🍪{fmt(p.reward_cookies)}
              <div className="hint">
                {p.uses}/{p.max_uses || '∞'} использований
              </div>
            </div>
            <button className="claim-chip" onClick={() => togglePromo(p.code, !p.active)}>
              {p.active ? 'Выключить' : 'Включить'}
            </button>
          </div>
        ))}
      </div>

      <div className="card">
        <b>🔗 Source-ссылки</b>
        <input className="field" style={{ marginTop: 10 }} placeholder="код (напр. tiktok_1)"
               value={sCode} onChange={(e) => setSCode(e.target.value)} />
        <input className="field" placeholder="Название (TikTok реклама #1)" value={sTitle}
               onChange={(e) => setSTitle(e.target.value)} />
        <button className="btn" onClick={createSource} disabled={!sCode.trim()}>
          Создать ссылку
        </button>
        {sources.map((s) => (
          <div key={s.code} style={{ padding: '8px 0', fontSize: 13 }}>
            <div className="row">
              <b>{s.title || s.code}</b>
              <span>
                👥 {s.registrations}
                <button className="claim-chip" style={{ marginLeft: 8 }} onClick={() => copy(s.link)}>
                  📋
                </button>
              </span>
            </div>
            <div className="hint" style={{ wordBreak: 'break-all' }}>{s.link}</div>
          </div>
        ))}
      </div>
    </div>
  )
}
