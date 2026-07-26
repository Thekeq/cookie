// Престиж — самое необратимое действие в игре, и подтверждался он системным
// window.confirm: чужой шрифт, чужие кнопки, и ни слова о том, что именно
// сгорит, а что останется. Здесь это сцена: старая печь остывает, новая
// разгорается, и обе колонки последствий видно до нажатия.
import { useEffect, useState } from 'react'
import { fmt } from './App'
import { useT } from './i18n'

interface Props {
  points: number          // сколько очков даст перерождение
  multiplier: number      // множитель ПОСЛЕ перерождения
  keptLevel: number       // какой уровень останется
  level: number           // текущий уровень
  onConfirm: () => void
  onClose: () => void
}

export default function PrestigeModal(
  { points, multiplier, keptLevel, level, onConfirm, onClose }: Props,
) {
  const t = useT()
  const [burning, setBurning] = useState(false)

  // на подтверждении проигрываем короткую сцену, потом отдаём управление
  useEffect(() => {
    if (!burning) return
    const timer = setTimeout(onConfirm, 900)
    return () => clearTimeout(timer)
  }, [burning, onConfirm])

  return (
    <div className="modal-backdrop" onClick={burning ? undefined : onClose}>
      <div className={"modal-card prestige-modal" + (burning ? " burning" : "")}
           onClick={(e) => e.stopPropagation()}>
        <div className="prestige-scene">
          <span className="prestige-old">🏚️</span>
          <span className="prestige-arrow">→</span>
          <span className="prestige-new">✨</span>
        </div>

        <h3 style={{ marginBottom: 6 }}>{t('prestige_title')}</h3>
        <div className="prestige-gain">x{multiplier.toFixed(2)}</div>
        <div className="hint" style={{ marginBottom: 12 }}>
          {t('prestige_modal_hint', { n: fmt(points) })}
        </div>

        <div className="prestige-cols">
          <div className="prestige-col lose">
            <div className="prestige-col-t">{t('prestige_lose')}</div>
            <div>🏭 {t('buildings')}</div>
            <div>🧩 {t('tab_merge')}</div>
            <div>⬆️ {t('upgrades')}</div>
            <div>🍪 {t('prestige_lose_cookies')}</div>
          </div>
          <div className="prestige-col keep">
            <div className="prestige-col-t">{t('prestige_keep')}</div>
            <div>🎖️ {t('tab_bp')}</div>
            <div>👥 {t('friends')}</div>
            <div>⭐ {t('tab_shop')}</div>
            <div>🗺️ {t('prestige_keep_level', { a: keptLevel, b: level })}</div>
          </div>
        </div>

        <button className="btn" disabled={burning} onClick={() => setBurning(true)}>
          {burning ? t('prestige_burning') : t('prestige_confirm_btn')}
        </button>
        {!burning && (
          <button className="btn secondary" style={{ marginTop: 8 }} onClick={onClose}>
            {t('cancel')}
          </button>
        )}
      </div>
    </div>
  )
}
