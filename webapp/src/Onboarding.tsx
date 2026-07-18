import { useContext, useState } from 'react'
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

  const finish = () => {
    localStorage.setItem('onboarded', '1')
    sfxBuy()
    onDone()
  }

  if (step === 0)
    return (
      <div className="onboarding">
        <div className="onb-emoji">🌐</div>
        <h2>{t('tut_lang_title')}</h2>
        <div className="onb-langs">
          {LANGS.map((l) => (
            <button
              key={l.code}
              className={'onb-lang' + (lang === l.code ? ' active' : '')}
              onClick={() => setLang(l.code)}
            >
              <span style={{ fontSize: 34 }}>{l.flag}</span>
              <span>{l.label}</span>
            </button>
          ))}
        </div>
        <button className="btn" style={{ maxWidth: 320 }} onClick={() => setStep(1)}>
          {t('tut_next')}
        </button>
      </div>
    )

  const s = STEPS[step - 1]
  const last = step === STEPS.length

  return (
    <div className="onboarding">
      <div className="onb-dots">
        {STEPS.map((_, i) => (
          <span key={i} className={'onb-dot' + (i === step - 1 ? ' active' : '')} />
        ))}
      </div>
      <div className="onb-emoji">{s.emoji}</div>
      <h2>{t(s.title)}</h2>
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
    </div>
  )
}
