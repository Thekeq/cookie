import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, ApiError, startParam } from './api'
import type { GameState } from './types'
import { Lang, LangCtx, loadLang, saveLang, useT, useTErr } from './i18n'
import { unlockAudio } from './sound'
import Onboarding from './Onboarding'
import DailyModal from './DailyModal'
import ClickerTab from './tabs/ClickerTab'
import MergeTab from './tabs/MergeTab'
import BakeryTab from './tabs/BakeryTab'
import FarmTab from './tabs/FarmTab'
import ProgressTab from './tabs/ProgressTab'
import ProfileHubTab from './tabs/ProfileHubTab'

interface Ctx {
  state: GameState
  setState: (s: GameState) => void
  refresh: () => Promise<void>
  toast: (msg: string, isError?: boolean) => void
  isAdmin: boolean
  /** единый живой баланс для всех вкладок: сервер + пассивный тик + предикт кликов */
  liveBalance: number
  /** предикт клика: мгновенно прибавляет к балансу, сервер подтвердит батчем */
  bumpBalance: (n: number) => void
  /** текущий множитель комбо (живёт здесь — переживает смену вкладок) */
  combo: number
  /** регистрирует тап: очередь кликов живёт в App и не теряется при смене вкладки */
  tapClick: (predicted: number) => void
  /** дожидается отправки всех накопленных кликов; звать перед любой покупкой */
  flushClicks: () => Promise<void>
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
  const te = useTErr()
  const [state, setState] = useState<GameState | null>(null)
  const [error, setError] = useState('')
  // диплинк /admin из бота открывает приложение сразу на админ-панели
  const [tab, setTab] = useState(startParam() === 'admin' ? 'profile' : 'clicker')
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [showOnboarding, setShowOnboarding] = useState(!localStorage.getItem('onboarded'))
  // попап ежедневной награды — один раз за сессию, если есть что забрать
  const [dailyShown, setDailyShown] = useState(false)
  // живой баланс: тикает каждую секунду со скоростью фермы + пассивки мерджа
  const [liveCookies, setLiveCookies] = useState(0)
  // предикт кликов: тапы падают сюда мгновенно, сервер подтверждает батчем
  const [clickDelta, setClickDelta] = useState(0)

  const bumpBalance = useCallback((n: number) => setClickDelta((v) => v + n), [])

  const toast = useCallback((text: string, err = false) => {
    setToastMsg({ text, err })
    setTimeout(() => setToastMsg(null), 2500)
  }, [])

  const refresh = useCallback(async () => {
    const s = await api.get('/api/state')
    setState(s)
    setLiveCookies(0)
  }, [])

  // ---- очередь кликов живёт здесь, а не во вкладке кликера: не теряется
  // при смене вкладки, и любая покупка может дождаться её отправки ----
  const pendingClicks = useRef(0)
  const clickRetry = useRef<{ id: string; n: number } | null>(null)
  const clickInflight = useRef<Promise<void> | null>(null)
  const lastTapAt = useRef(0)
  const [combo, setCombo] = useState(1)

  const tapClick = useCallback((predicted: number) => {
    lastTapAt.current = Date.now()
    pendingClicks.current += 1
    setClickDelta((v) => v + predicted)
  }, [])

  const sendClickBatch = useCallback(async () => {
    // ретрай потерянного ответа идёт тем же batch_id — сервер дедуплицирует
    let batch = clickRetry.current
    if (!batch) {
      const n = pendingClicks.current
      if (!n) return
      pendingClicks.current = 0
      batch = { id: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`, n }
    }
    try {
      const r = await api.post('/api/click', { clicks: batch.n, batch_id: batch.id })
      clickRetry.current = null
      // серверное комбо принимаем, только если игрок ещё тапает —
      // иначе устаревший ответ «воскресит» уже погасшее комбо
      if (Date.now() - lastTapAt.current < 4000) setCombo(r.combo || 1)
      setState((prev: GameState | null) =>
        prev
          ? {
              ...prev,
              golden: r.golden ?? prev.golden,
              user: { ...prev.user, cookies: r.cookies, energy: r.energy, xp: r.xp ?? prev.user.xp },
            }
          : prev)
    } catch {
      clickRetry.current = batch // сеть моргнула — повторим тот же батч
    }
  }, [])

  const flushClicks = useCallback(async () => {
    // запросы не пересекаются: ждём текущий, потом дожимаем очередь
    while (clickInflight.current) await clickInflight.current
    while (pendingClicks.current > 0 || clickRetry.current) {
      const p = sendClickBatch()
      clickInflight.current = p
      await p
      clickInflight.current = null
      if (clickRetry.current) break // сеть лежит — не крутимся вечно
    }
  }, [sendClickBatch])

  // батч-отправка раз в 1.5 сек — работает с любой открытой вкладкой
  useEffect(() => {
    if (!state || showOnboarding) return
    const timer = setInterval(() => {
      if (clickInflight.current) return
      const p = sendClickBatch()
      clickInflight.current = p
      p.finally(() => (clickInflight.current = null))
    }, 1500)
    return () => clearInterval(timer)
  }, [state !== null, showOnboarding, sendClickBatch])

  // локальное затухание комбо: пауза в тапах > 4с — гаснет сразу на клиенте
  useEffect(() => {
    const timer = setInterval(() => {
      if (combo > 1 && Date.now() - lastTapAt.current > 4000) setCombo(1)
    }, 400)
    return () => clearInterval(timer)
  }, [combo])

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

  // при любом обновлении стейта с сервера локальная прибавка обнуляется:
  // серверный баланс уже включает и пассивку, и подтверждённые клики
  useEffect(() => {
    setLiveCookies(0)
    setClickDelta(0)
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
      .catch((e) => setError(e instanceof ApiError ? te(e.detail) : t('open_in_tg')))
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
      <GameCtx.Provider
        value={{ state, setState, refresh, toast, isAdmin, liveBalance: state.user.cookies,
                 bumpBalance, combo, tapClick, flushClicks }}
      >
        <Onboarding onDone={() => setShowOnboarding(false)} />
      </GameCtx.Provider>
    )

  // 6 вкладок; «Прогресс» и «Профиль» содержат сегменты (Путь/Пасс/Топ и Профиль/Stars/Админ)
  const tabs = [
    { key: 'clicker', ico: state.user.skin_emoji || '🍪', label: t('tab_clicker') },
    { key: 'merge', ico: '🧩', label: t('tab_merge') },
    { key: 'bakery', ico: '🧑‍🍳', label: t('tab_bakery'), badge: !!state.orders_claimable },
    { key: 'farm', ico: '🏭', label: t('tab_farm') },
    { key: 'progress', ico: '🗺️', label: t('tab_progress'), badge: !!state.claimable_level || state.quests_claimable > 0 },
    { key: 'profile', ico: '👤', label: t('tab_profile') },
  ]

  // единая правда для всех вкладок: шапка и кликер показывают одно число
  const liveBalance = state.user.cookies + liveCookies + clickDelta

  return (
    <GameCtx.Provider
      value={{ state, setState, refresh, toast, isAdmin, liveBalance,
               bumpBalance, combo, tapClick, flushClicks }}
    >
      <div className="app">
        <div className="header">
          <div className="balance">🍪 {fmt(liveBalance)}</div>
          <div className="lvl">
            ⚡ {Math.floor(state.user.energy)}/{state.user.max_energy} · {t('level')}{' '}
            {state.user.level}
          </div>
        </div>
        <div className="content">
          {tab === 'clicker' && <ClickerTab />}
          {tab === 'merge' && <MergeTab />}
          {tab === 'bakery' && <BakeryTab />}
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
