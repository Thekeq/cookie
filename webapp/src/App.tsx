import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, ApiError, startParam } from './api'
import type { GameState } from './types'
import { Lang, LangCtx, loadLang, saveLang, setActiveLang, useT, useTErr } from './i18n'
import { formatNumber, formatRate } from './format'
import { unlockAudio } from './sound'
import Onboarding from './Onboarding'
import DailyModal from './DailyModal'
import OfflineModal from './OfflineModal'
import WhatsNew, { markWhatsNewSeen, shouldShowWhatsNew } from './WhatsNew'
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
  /** текущий множитель комбо (живёт здесь — переживает смену вкладок) */
  combo: number
  /** регистрирует тап: очередь кликов живёт в App и не теряется при смене вкладки */
  tapClick: (predicted: number) => void
  /** дожидается отправки всех накопленных кликов; звать перед любой покупкой */
  flushClicks: () => Promise<void>
}

const GameCtx = createContext<Ctx>(null!)
export const useGame = () => useContext(GameCtx)

// Форматирование переехало в format.ts (локаль-зависимое). Имена fmt/fmtRate
// оставлены как есть: их импортируют из './App' все вкладки.
export const fmt = formatNumber
export const fmtRate = formatRate

export default function App() {
  const [lang, setLangState] = useState<Lang>(loadLang())
  const setLang = (l: Lang) => {
    setLangState(l)
    saveLang(l) // внутри saveLang → setActiveLang: <html lang> и Intl-форматтеры
  }
  // синхронно, до рендера детей: fmt() зовут прямо в разметке вкладок
  setActiveLang(lang)
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
  // оффлайн-доход показываем модалкой: тост про «+N» рядом с уже выросшим
  // балансом читался как «написали, но не начислили»
  const [offlineIncome, setOfflineIncome] = useState(0)
  // экран «что нового»: игрок узнавал об изменениях баланса, натыкаясь
  // на другие числа — теперь один раз на версию ему это проговаривают
  const [showWhatsNew, setShowWhatsNew] = useState(shouldShowWhatsNew())
  // живой баланс: тикает каждую секунду со скоростью фермы + пассивки мерджа
  const [liveCookies, setLiveCookies] = useState(0)
  // энергия тикает так же — иначе шапка стоит мёртвой до ответа сервера
  const [liveEnergy, setLiveEnergy] = useState(0)
  // предикт кликов: тапы падают сюда мгновенно, сервер подтверждает батчем
  const [clickDelta, setClickDelta] = useState(0)

  // таймер тоста живёт в ref: без этого второй тост гасился таймером первого
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const toast = useCallback((text: string, err = false) => {
    setToastMsg({ text, err })
    if (toastTimer.current) clearTimeout(toastTimer.current)
    toastTimer.current = setTimeout(() => setToastMsg(null), 2500)
  }, [])
  useEffect(() => () => { if (toastTimer.current) clearTimeout(toastTimer.current) }, [])

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

  // энергия тоже восстанавливается на глазах: раньше число в шапке стояло
  // мёртвым до ответа сервера, и «подожди, пока накопится» выглядело как баг
  useEffect(() => {
    if (!state) return
    const regen = state.user.energy_regen || 0
    if (regen <= 0) return
    const timer = setInterval(() => setLiveEnergy((v) => v + regen), 1000)
    return () => clearInterval(timer)
  }, [state?.user.energy_regen])
  useEffect(() => setLiveEnergy(0), [state?.user.energy])

  // сервер знает правду: раз в 30 сек синкаем накопленное (collect в /api/state)
  useEffect(() => {
    if (!state || showOnboarding) return
    const timer = setInterval(() => refresh().catch(() => {}), 30_000)
    return () => clearInterval(timer)
  }, [state !== null, showOnboarding, refresh])

  // Возврат в приложение. Свёрнутый вебвью душит таймеры, поэтому 30-секундный
  // синк не срабатывает, и игрок возвращался к устаревшему экрану: баланс,
  // энергия и заказы показывали состояние на момент сворачивания.
  useEffect(() => {
    if (!state || showOnboarding) return
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        flushClicks().catch(() => {})
        refresh().catch(() => {})
      }
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => document.removeEventListener('visibilitychange', onVisible)
  }, [state !== null, showOnboarding, refresh, flushClicks])

  // при любом обновлении стейта с сервера локальная прибавка обнуляется:
  // серверный баланс уже включает и пассивку, и подтверждённые клики
  useEffect(() => {
    setLiveCookies(0)
    setClickDelta(0)
  }, [state?.user.cookies])

  const bootstrap = useCallback(() => {
    setError('')
    return api
      .post('/api/auth')
      .then((s: GameState) => {
        setState(s)
        if (s.just_registered) toast(t('welcome'))
        if (s.passive_collected && s.passive_collected > 1)
          setOfflineIncome(s.passive_collected)
        api.get('/api/admin/stats').then(() => setIsAdmin(true)).catch(() => {})
      })
      .catch((e) => setError(e instanceof ApiError ? te(e.detail) : t('open_in_tg')))
  }, [toast, t, te])

  useEffect(() => {
    bootstrap()
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

  // Экран ошибки с выходом: раньше обрыв сети на старте оставлял игрока
  // наедине с грустным смайликом, и единственным способом продолжить было
  // закрыть и открыть приложение заново
  if (error)
    return (
      <div className="loading-screen">
        <span className="error-emoji">🥠</span>
        <div className="error-text">{error}</div>
        <button className="btn" style={{ maxWidth: 220 }} onClick={() => bootstrap()}>
          {t('retry')}
        </button>
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
                 combo, tapClick, flushClicks }}
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
  // энергия не может перелиться через потолок — сервер её всё равно срежет
  const liveEnergyShown = Math.min(state.user.max_energy, state.user.energy + liveEnergy)

  return (
    <GameCtx.Provider
      value={{ state, setState, refresh, toast, isAdmin, liveBalance,
               combo, tapClick, flushClicks }}
    >
      <div className="app">
        <div className="header">
          {/* aria-label с числительным: скринридер читает «1 234 печеньки»,
              а не «печенье 1 234». Живой регион тут не нужен — баланс тикает
              каждую секунду, и aria-live превратил бы это в непрерывный поток */}
          <div className="balance" aria-label={`${t('balance_label')}: ${t.plural('n_cookies', Math.floor(liveBalance), { n: fmt(liveBalance) })}`}>
            <span aria-hidden="true">🍪 {fmt(liveBalance)}</span>
          </div>
          <div className="lvl">
            <span aria-hidden="true">⚡</span> {Math.floor(liveEnergyShown)}/{state.user.max_energy} ·{' '}
            {t('level')} {state.user.level}
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
        <nav className="tabbar" aria-label={t('nav_sections')}>
          {tabs.map((tb) => (
            <button
              key={tb.key}
              aria-current={tab === tb.key ? 'page' : undefined}
              className={tab === tb.key ? 'active' : ''}
              onClick={() => setTab(tb.key)}
            >
              <span className="ico" aria-hidden="true" style={{ position: 'relative' }}>
                {tb.ico}
                {tb.badge && <span className="tab-badge" />}
              </span>
              {tb.label}
              {/* бейдж — красная точка; цвет один состояние кодировать не может */}
              {tb.badge && <span className="sr-only"> — {t('badge_new')}</span>}
            </button>
          ))}
        </nav>
        {/* Живой регион смонтирован всегда: если создавать его вместе с тостом,
            часть скринридеров не успевает подхватить изменение и молчит. */}
        <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
          {toastMsg ? (toastMsg.err ? `${t('error')}: ${toastMsg.text}` : toastMsg.text) : ''}
        </div>
        {toastMsg && (
          <div className={'toast' + (toastMsg.err ? ' error' : '')} aria-hidden="true">
            {toastMsg.err && <span className="toast-ico">⚠️</span>}
            {toastMsg.text}
          </div>
        )}
        {offlineIncome > 1 && (
          <OfflineModal amount={offlineIncome} onClose={() => setOfflineIncome(0)} />
        )}
        {offlineIncome <= 1 && !showWhatsNew && state.daily?.can_claim && !dailyShown && (
          <DailyModal daily={state.daily} onClose={() => setDailyShown(true)} />
        )}
        {showWhatsNew && offlineIncome <= 1 && (
          <WhatsNew onClose={() => { markWhatsNewSeen(); setShowWhatsNew(false) }} />
        )}
      </div>
    </GameCtx.Provider>
  )
}
