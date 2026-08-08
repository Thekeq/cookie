import { createContext, lazy, Suspense, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api, ApiError } from './api'
import { startTarget, useBackButton } from './telegram'
import type { GameState } from './types'
import { Lang, LangCtx, loadLang, saveLang, setActiveLang, useT, useTErr } from './i18n'
import { formatNumber, formatRate } from './format'
import { unlockAudio } from './sound'
import Onboarding from './Onboarding'
import DailyModal from './DailyModal'
import OfflineModal from './OfflineModal'
import WhatsNew, { markWhatsNewSeen, shouldShowWhatsNew } from './WhatsNew'
// Кликер — вкладка первого кадра, он в основном бандле. Остальные пять едут
// отдельными чанками: до первого тапа игрок их не видит, а тянут они за собой
// мердж-доску, ферму, лидерборды, батлпасс и админку.
import ClickerTab from './tabs/ClickerTab'

const MergeTab = lazy(() => import('./tabs/MergeTab'))
const BakeryTab = lazy(() => import('./tabs/BakeryTab'))
const FarmTab = lazy(() => import('./tabs/FarmTab'))
const ProgressTab = lazy(() => import('./tabs/ProgressTab'))
const ProfileHubTab = lazy(() => import('./tabs/ProfileHubTab'))

// Чанк начинают качать на pointerdown, а не на click: между нажатием и
// отпусканием пальца есть ~100 мс, и на них обычно укладывается загрузка —
// вкладка открывается без промежуточного спиннера. Промах по кнопке стоит
// одного лишнего запроса за сессию, это дешевле мигающего экрана.
const TAB_CHUNK: Record<string, () => Promise<unknown>> = {
  merge: () => import('./tabs/MergeTab'),
  bakery: () => import('./tabs/BakeryTab'),
  farm: () => import('./tabs/FarmTab'),
  progress: () => import('./tabs/ProgressTab'),
  profile: () => import('./tabs/ProfileHubTab'),
}
const prefetchTab = (key: string) => {
  TAB_CHUNK[key]?.().catch(() => {}) // не приехало — Suspense покажет спиннер
}

// Шаг единственного таймера приложения (см. «один таймер на всё» ниже).
// 500 мс — общий делитель всех прежних периодов: 1с, 1.5с, 30с.
const TICK_MS = 500

interface Ctx {
  state: GameState
  setState: (s: GameState) => void
  refresh: () => Promise<void>
  toast: (msg: string, isError?: boolean) => void
  isAdmin: boolean
  /** имя бота для реф-ссылок; приходит с сервера один раз в /api/auth и живёт
   *  здесь, потому что /api/state его не возвращает и перетёр бы поле в state */
  botUsername: string
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
  // диплинк из бота (пуш, ссылка, /admin) открывает приложение сразу на нужной
  // вкладке; составные вкладки сами дочитают из него свой сегмент
  const [tab, setTab] = useState(startTarget()?.tab ?? 'clicker')
  const [toastMsg, setToastMsg] = useState<{ text: string; err: boolean } | null>(null)
  const [isAdmin, setIsAdmin] = useState(false)
  const [botUsername, setBotUsername] = useState('')
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

  // Нативная «назад» на любой вкладке кроме стартовой: раньше единственным
  // выходом с внутреннего экрана была панель вкладок, а системная кнопка
  // закрывала приложение целиком. Слой самый нижний (priority 0) — сегменты
  // и модалки перекрывают его своими.
  useBackButton(tab === 'clicker' ? null : () => setTab('clicker'))

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

  useEffect(() => setLiveEnergy(0), [state?.user.energy])

  // ---- один таймер на всё приложение ----
  //
  // Раньше здесь крутилось пять независимых setInterval: батч кликов (1.5с),
  // затухание комбо (0.4с), тик печенек (1с), тик энергии (1с) и синк с
  // сервером (30с). На дешёвом Android каждый таймер — это отдельный wakeup
  // и отдельный проход рендера; вместе они не давали главному потоку
  // простаивать вообще. Теперь тикает ОДИН интервал, а прежние периоды
  // выражены кратностью его тика.
  //
  // Эффект монтируется ровно один раз: всё, что меняется от рендера к
  // рендеру, лежит в ref-ах, иначе пересоздание интервала при каждом тапе
  // (combo менялся) сбрасывало бы счётчик и 30-секундный синк не наступал
  // бы никогда.
  const perSecRef = useRef(0)
  const energyRegenRef = useRef(0)
  const liveRef = useRef(false)
  useEffect(() => {
    perSecRef.current = state ? (state.farm?.cps || 0) + (state.passive_per_hour || 0) / 3600 : 0
    energyRegenRef.current = state?.user.energy_regen || 0
    liveRef.current = !!state && !showOnboarding
  })

  useEffect(() => {
    let n = 0
    let id: ReturnType<typeof setInterval> | null = null

    const tick = () => {
      n += 1
      // комбо гаснет и на паузе в игре — проверка дешёвая, без setState,
      // если значение не изменилось (React отбрасывает такой апдейт)
      if (Date.now() - lastTapAt.current > 4000) setCombo((c) => (c > 1 ? 1 : c))
      if (!liveRef.current) return

      if (n % 2 === 0) {
        // 1 с: пассивка (ферма + доска) и реген энергии капают на глазах
        if (perSecRef.current > 0) setLiveCookies((v) => v + perSecRef.current)
        if (energyRegenRef.current > 0) setLiveEnergy((v) => v + energyRegenRef.current)
      }
      if (n % 3 === 0 && !clickInflight.current) {
        // 1.5 с: батч кликов — работает с любой открытой вкладкой
        const p = sendClickBatch()
        clickInflight.current = p
        p.finally(() => (clickInflight.current = null))
      }
      if (n % 60 === 0) {
        // 30 с: сервер знает правду (collect внутри /api/state)
        refresh().catch(() => {})
      }
    }

    // Свёрнутый вебвью всё равно душит таймеры, но не гарантированно и не
    // сразу: пока он их душит не до конца, мы жжём батарею на тики, которые
    // никто не увидит. Останавливаем сами; накопленное досчитает сервер, а
    // на возврате уже стоит refresh() по visibilitychange (ниже).
    const start = () => { if (id === null) id = setInterval(tick, TICK_MS) }
    const stop = () => { if (id !== null) { clearInterval(id); id = null } }
    const onVisibility = () => (document.visibilityState === 'visible' ? start() : stop())

    start()
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [sendClickBatch, refresh])

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
        if (s.bot_username) setBotUsername(s.bot_username)
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
        value={{ state, setState, refresh, toast, isAdmin, botUsername,
                 liveBalance: state.user.cookies, combo, tapClick, flushClicks }}
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
      value={{ state, setState, refresh, toast, isAdmin, botUsername, liveBalance,
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
          {tab === 'clicker' ? (
            <ClickerTab />
          ) : (
            // Тот же 🍪, что и на загрузке приложения: игрок уже видел этот
            // экран и понимает, что идёт загрузка, а не что-то сломалось.
            <Suspense
              fallback={
                // aria-hidden: подписи «загрузка» нет в словаре, а придумывать
                // ключ мимо i18n.ts — оставить его непереведённым. Скринридер
                // и так объявит новую вкладку, когда та смонтируется.
                <div className="loading-screen" aria-hidden="true">
                  <span className="spin">🍪</span>
                </div>
              }
            >
              {tab === 'merge' && <MergeTab />}
              {tab === 'bakery' && <BakeryTab />}
              {tab === 'farm' && <FarmTab />}
              {tab === 'progress' && <ProgressTab />}
              {tab === 'profile' && <ProfileHubTab />}
            </Suspense>
          )}
        </div>
        <nav className="tabbar" aria-label={t('nav_sections')}>
          {tabs.map((tb) => (
            <button
              key={tb.key}
              aria-current={tab === tb.key ? 'page' : undefined}
              className={tab === tb.key ? 'active' : ''}
              onPointerDown={() => prefetchTab(tb.key)}
              onFocus={() => prefetchTab(tb.key)}
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
