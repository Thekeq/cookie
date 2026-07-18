import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import { api, ApiError } from './api'
import type { GameState } from './types'
import { Lang, LangCtx, loadLang, saveLang, useT } from './i18n'
import { unlockAudio } from './sound'
import Onboarding from './Onboarding'
import DailyModal from './DailyModal'
import ClickerTab from './tabs/ClickerTab'
import MergeTab from './tabs/MergeTab'
import FarmTab from './tabs/FarmTab'
import ProgressTab from './tabs/ProgressTab'
import ProfileHubTab from './tabs/ProfileHubTab'

interface Ctx {
  state: GameState
  setState: (s: GameState) => void
  refresh: () => Promise<void>
  toast: (msg: string, isError?: boolean) => void
  isAdmin: boolean
}

const GameCtx = createContext<Ctx>(null!)
export const useGame = () => useContext(GameCtx)

export function fmt(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'K'
  return Math.floor(n).toLocaleString('en')
}

export default function App() {
  const [lang, setLangState] = useState<Lang>(loadLang())
  const setLang = (l: Lang) => {
    setLangState(l)
    saveLang(l)
  }
  return (
    <LangCtx.Provider value={{ lang, setLang }}>
      <Game />
    </LangCtx.Provider>
  )
}

function Game() {
  const t = useT()
  const [state, setState] = useState<GameState | null>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState('clicker')
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [showOnboarding, setShowOnboarding] = useState(!localStorage.getItem('onboarded'))
  // попап ежедневной награды — один раз за сессию, если есть что забрать
  const [dailyShown, setDailyShown] = useState(false)
  // живой баланс: тикает каждую секунду со скоростью фермы + пассивки мерджа
  const [liveCookies, setLiveCookies] = useState(0)

  const toast = useCallback((text: string, err = false) => {
    setToastMsg({ text, err })
    setTimeout(() => setToastMsg(null), 2500)
  }, [])

  const refresh = useCallback(async () => {
    const s = await api.get('/api/state')
    setState(s)
    setLiveCookies(0)
  }, [])

  // тик пассивного дохода: ферма (cps) + мердж-доска (в час) капают на глазах
  useEffect(() => {
    if (!state) return
    const perSec = (state.farm?.cps || 0) + (state.passive_per_hour || 0) / 3600
    if (perSec <= 0) return
    const timer = setInterval(() => setLiveCookies((v) => v + perSec), 1000)
    return () => clearInterval(timer)
  }, [state?.farm?.cps, state?.passive_per_hour])

  // сервер знает правду: раз в 30 сек синкаем накопленное (collect в /api/state)
  useEffect(() => {
    if (!state || showOnboarding) return
    const timer = setInterval(() => refresh().catch(() => {}), 30_000)
    return () => clearInterval(timer)
  }, [state !== null, showOnboarding, refresh])

  // при любом обновлении стейта с сервера локальная прибавка обнуляется
  useEffect(() => {
    setLiveCookies(0)
  }, [state?.user.cookies])

  useEffect(() => {
    api
      .post('/api/auth')
      .then((s: GameState) => {
        setState(s)
        if (s.just_registered) toast(t('welcome'))
        if (s.passive_collected && s.passive_collected > 1)
          toast(`${t('offline_income')}: +${fmt(s.passive_collected)} 🍪`)
        api.get('/api/admin/stats').then(() => setIsAdmin(true)).catch(() => {})
      })
      .catch((e) => setError(e instanceof ApiError ? e.detail : t('open_in_tg')))
  }, [])

  // браузер разрешает звук только после первого жеста — ловим его один раз
  useEffect(() => {
    const unlock = () => {
      unlockAudio()
      window.removeEventListener('pointerdown', unlock)
    }
    window.addEventListener('pointerdown', unlock)
    return () => window.removeEventListener('pointerdown', unlock)
  }, [])

  if (error)
    return (
      <div className="loading-screen">
        <span>😕</span>
        <div style={{ fontSize: 15, padding: '0 30px', textAlign: 'center' }}>{error}</div>
      </div>
    )
  if (!state)
    return (
      <div className="loading-screen">
        <span className="spin">🍪</span>
      </div>
    )

  if (showOnboarding)
    return (
      <GameCtx.Provider value={{ state, setState, refresh, toast, isAdmin }}>
        <Onboarding onDone={() => setShowOnboarding(false)} />
      </GameCtx.Provider>
    )

  // 5 вкладок; «Прогресс» и «Профиль» содержат сегменты (Путь/Пасс/Топ и Профиль/Stars/Админ)
  const tabs = [
    { key: 'clicker', ico: state.user.skin_emoji || '🍪', label: t('tab_clicker') },
    { key: 'merge', ico: '🧩', label: t('tab_merge') },
    { key: 'farm', ico: '🏭', label: t('tab_farm') },
    { key: 'progress', ico: '🗺️', label: t('tab_progress'), badge: !!state.claimable_level || state.quests_claimable > 0 },
    { key: 'profile', ico: '👤', label: t('tab_profile') },
  ]

  return (
    <GameCtx.Provider value={{ state, setState, refresh, toast, isAdmin }}>
      <div className="app">
        <div className="header">
          <div className="balance">🍪 {fmt(state.user.cookies + liveCookies)}</div>
          <div className="lvl">
            ⚡ {Math.floor(state.user.energy)}/{state.user.max_energy} · {t('level')}{' '}
            {state.user.level}
          </div>
        </div>
        <div className="content">
          {tab === 'clicker' && <ClickerTab />}
          {tab === 'merge' && <MergeTab />}
          {tab === 'farm' && <FarmTab />}
          {tab === 'progress' && <ProgressTab />}
          {tab === 'profile' && <ProfileHubTab />}
        </div>
        <div className="tabbar">
          {tabs.map((tb) => (
            <button key={tb.key} className={tab === tb.key ? 'active' : ''} onClick={() => setTab(tb.key)}>
              <span className="ico" style={{ position: 'relative' }}>
                {tb.ico}
                {tb.badge && <span className="tab-badge" />}
              </span>
              {tb.label}
            </button>
          ))}
        </div>
        {toastMsg && <div className={'toast' + (toastMsg.err ? ' error' : '')}>{toastMsg.text}</div>}
        {state.daily?.can_claim && !dailyShown && (
          <DailyModal daily={state.daily} onClose={() => setDailyShown(true)} />
        )}
      </div>
    </GameCtx.Provider>
  )
}
