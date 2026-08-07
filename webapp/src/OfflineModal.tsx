// Оффлайн-доход: раньше он приходил тостом, и цифра в шапке уже включала его —
// игроки читали это как «написали +N, а не начислили». Модалка показывает
// прибавку явно, отдельно от баланса.
import { fmt } from './App'
import { useFocusTrap } from './a11y'
import { useT } from './i18n'

export default function OfflineModal({ amount, onClose }: { amount: number; onClose: () => void }) {
  const t = useT()
  const trap = useFocusTrap(onClose)
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        ref={trap}
        className="modal-card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="offline-title"
        style={{ textAlign: 'center' }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="offline-emoji" aria-hidden="true">🌙</div>
        <b id="offline-title" style={{ fontSize: 18 }}>{t('offline_title')}</b>
        <div className="hint" style={{ marginTop: 6 }}>{t('offline_text')}</div>
        {/* награда — живой регион: цифра появляется уже после открытия модалки */}
        <div
          className="offline-amount"
          role="status"
          aria-live="polite"
          aria-label={'+' + t.plural('n_cookies', Math.floor(amount), { n: fmt(amount) })}
        >
          <span aria-hidden="true">+{fmt(amount)} 🍪</span>
        </div>
        <button className="btn" onClick={onClose}>{t('offline_ok')}</button>
      </div>
    </div>
  )
}
