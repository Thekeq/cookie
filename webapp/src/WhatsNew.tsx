// «Что нового» после обновления.
//
// Баланс и механики меняются часто, а игрок узнавал об этом, натыкаясь на
// изменившиеся числа: клик вдруг качается иначе, мердж даёт больше, доска
// стоит по-другому. Один экран при первом запуске новой версии снимает это.
//
// Список короткий и на языке игрока: что для него изменилось, а не что мы
// сделали. Версия хранится в localStorage — экран показывается один раз.
import { useT } from './i18n'

/** Поднимай при заметных для игрока изменениях. */
export const APP_VERSION = 3

interface Props {
  onClose: () => void
}

/** Пункты текущей версии: ключ i18n + иконка. */
const NOTES: { ico: string; key: string }[] = [
  { ico: '👆', key: 'wn_click' },
  { ico: '🧩', key: 'wn_merge' },
  { ico: '🥖', key: 'wn_recipe' },
  { ico: '✨', key: 'wn_event' },
  { ico: '🎁', key: 'wn_rewards' },
]

export default function WhatsNew({ onClose }: Props) {
  const t = useT()
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-card" onClick={(e) => e.stopPropagation()}>
        <h3 style={{ textAlign: 'center', marginBottom: 12 }}>{t('whats_new_title')}</h3>
        <div className="wn-list">
          {NOTES.map((n) => (
            <div className="wn-row" key={n.key}>
              <span className="wn-ico">{n.ico}</span>
              <span>{t(n.key as any)}</span>
            </div>
          ))}
        </div>
        <button className="btn" style={{ marginTop: 14 }} onClick={onClose}>
          {t('whats_new_ok')}
        </button>
      </div>
    </div>
  )
}

/** Показывать ли экран: версия выросла и это не первый запуск вообще. */
export function shouldShowWhatsNew(): boolean {
  const seen = localStorage.getItem('app_version')
  // новичку показывать нечего — ему всё новое, у него онбординг
  if (!localStorage.getItem('onboarded')) return false
  return seen !== String(APP_VERSION)
}

export function markWhatsNewSeen() {
  localStorage.setItem('app_version', String(APP_VERSION))
}
