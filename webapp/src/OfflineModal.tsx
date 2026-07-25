// Оффлайн-доход: раньше он приходил тостом, и цифра в шапке уже включала его —
// игроки читали это как «написали +N, а не начислили». Модалка показывает
// прибавку явно, отдельно от баланса.
import { fmt } from './App'
import { useT } from './i18n'

export default function OfflineModal({ amount, onClose }: { amount: number; onClose: () => void }) {
  const t = useT()
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" style={{ textAlign: 'center' }} onClick={(e) => e.stopPropagation()}>
        <div className="offline-emoji">🌙</div>
        <b style={{ fontSize: 18 }}>{t('offline_title')}</b>
        <div className="hint" style={{ marginTop: 6 }}>{t('offline_text')}</div>
        <div className="offline-amount">+{fmt(amount)} 🍪</div>
        <button className="btn" onClick={onClose}>{t('offline_ok')}</button>
      </div>
    </div>
  )
}
