import { useContext, useState } from 'react'
import { useFocusTrap } from './a11y'
import { LANGS, LangCtx, useT } from './i18n'
import { sfxBuy } from './sound'

// Онбординг для новых игроков: шаг 0 — выбор языка, шаги 1..4 — туториал (скипается)
const STEPS = [
  { emoji: '🍪', title: 'tut_1_title', text: 'tut_1_text' },
  { emoji: '🧩', title: 'tut_2_title', text: 'tut_2_text' },
  { emoji: '🏭', title: 'tut_3_title', text: 'tut_3_text' },
  { emoji: '🗺️', title: 'tut_4_title', text: 'tut_4_text' },
] as const

export default function Onboarding({ onDone }: { onDone: () => void }) {
  const t = useT()
  const { lang, setLang } = useContext(LangCtx)
  const [step, setStep] = useState(0) // 0 = язык, 1..4 = туториал
  // экран перекрывает всё приложение — фокус держим внутри. Escape не даём:
  // онбординг нельзя «закрыть», из него можно только выйти кнопкой
  const trap = useFocusTrap()

  const finish = () => {
    localStorage.setItem('onboarded', '1')
    sfxBuy()
    onDone()
  }

  const s = step > 0 ? STEPS[step - 1] : null
  const last = step === STEPS.length

  // Один общий контейнер на оба шага: ловушка фокуса цепляется к узлу при
  // монтировании, и если бы шаги возвращали разные корневые div-ы, после
  // перехода Tab упирался бы в отсоединённый от документа старый узел.
  return (
    <div className="onboarding" ref={trap} role="dialog" aria-modal="true" aria-labelledby="onb-title">
      {s === null ? (
        <>
          <div className="onb-emoji" aria-hidden="true">🌐</div>
          <h2 id="onb-title">{t('tut_lang_title')}</h2>
          <div className="onb-langs">
            {LANGS.map((l) => (
              <button
                key={l.code}
                className={'onb-lang' + (lang === l.code ? ' active' : '')}
                aria-pressed={lang === l.code}
                onClick={() => setLang(l.code)}
              >
                {/* выбранный язык подсвечен рамкой; для скринридера — aria-pressed,
                    для глаза — ещё и галочка, а не только цвет */}
                <span style={{ fontSize: 34 }}>{l.flag}</span>
                <span>
                  {l.label}
                  {lang === l.code && <span aria-hidden="true"> ✓</span>}
                </span>
              </button>
            ))}
          </div>
          <button className="btn" style={{ maxWidth: 320 }} onClick={() => setStep(1)}>
            {t('tut_next')}
          </button>
        </>
      ) : (
        <>
          <div className="onb-dots" role="group" aria-label={`${step} / ${STEPS.length}`}>
            {STEPS.map((_, i) => (
              <span key={i} className={'onb-dot' + (i === step - 1 ? ' active' : '')} aria-hidden="true" />
            ))}
          </div>
          <div className="onb-emoji" aria-hidden="true">{s.emoji}</div>
          <h2 id="onb-title">{t(s.title)}</h2>
          <p className="onb-text">{t(s.text)}</p>
          <button className="btn" style={{ maxWidth: 320 }}
                  onClick={() => (last ? finish() : setStep(step + 1))}>
            {last ? t('tut_start') : t('tut_next')}
          </button>
          {!last && (
            <button className="onb-skip" onClick={finish}>
              {t('tut_skip')}
            </button>
          )}
        </>
      )}
    </div>
  )
}
