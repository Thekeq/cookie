// Нативные контролы Telegram WebApp: BackButton, MainButton, тема, showConfirm,
// диплинки.
//
// Единственная точка доступа к window.Telegram.WebApp: api.ts берёт объект
// отсюда же (`import { tg } from './telegram'`), второго пути к SDK в проекте
// быть не должно — иначе половина кода живёт с одним снимком объекта, а
// половина с другим.
import { useEffect, useRef } from 'react'

type Fn = () => void

/** Общее у BackButton и MainButton (у нижней кнопки параметров больше). */
interface BottomButton {
  show?(): void
  hide?(): void
  onClick?(cb: Fn): void
  offClick?(cb: Fn): void
}

interface MainBottomButton extends BottomButton {
  setParams?(p: { text?: string; is_active?: boolean; is_visible?: boolean }): void
  showProgress?(leaveActive?: boolean): void
  hideProgress?(): void
}

/** Ровно то из SDK, чем мы пользуемся; всё опционально — старые клиенты. */
export interface TelegramWebApp {
  initData?: string
  initDataUnsafe?: { user?: { id?: number }; start_param?: string }
  colorScheme?: 'light' | 'dark'
  themeParams?: Record<string, string | undefined>
  BackButton?: BottomButton
  MainButton?: MainBottomButton
  HapticFeedback?: {
    impactOccurred?(style: string): void
    notificationOccurred?(type: string): void
  }
  ready?(): void
  expand?(): void
  disableVerticalSwipes?(): void
  showConfirm?(message: string, cb: (ok: boolean) => void): void
  openInvoice?(url: string, cb: (status: string) => void): void
  openTelegramLink?(url: string): void
  setBackgroundColor?(color: string): void
  setHeaderColor?(color: string): void
  onEvent?(event: string, cb: Fn): void
  offEvent?(event: string, cb: Fn): void
}

export const tg: TelegramWebApp | undefined = (window as any).Telegram?.WebApp

// ---------------------------------------------------------------------------
// Стек экранов для нативных кнопок
//
// Кнопка в Telegram одна на всё приложение, а показать её хотят и вкладка, и
// сегмент внутри неё, и модалка поверх. Если каждый вызывать show()/hide()
// сам, то закрытие модалки прячет кнопку у экрана под ней («утечка» между
// экранами). Поэтому владелец кнопки — вершина стека: слой регистрируется на
// монтировании и снимается на размонтировании, а состояние кнопки каждый раз
// пересчитывается по вершине.
//
// priority — страховка от порядка эффектов: React выполняет эффекты снизу
// вверх, поэтому родительский экран может зарегистрироваться ПОЗЖЕ уже
// открытого потомка. Модалки берут priority=1 и всегда перебивают экраны.
// ---------------------------------------------------------------------------

interface Layer<T> {
  id: number
  priority: number
  value: T
}

let seq = 0

function topOf<T>(layers: Layer<T>[]): Layer<T> | null {
  let best: Layer<T> | null = null
  // `>=` — среди равных приоритетов побеждает зарегистрированный последним
  for (const l of layers) if (!best || l.priority >= best.priority) best = l
  return best
}

function drop<T>(layers: Layer<T>[], id: number) {
  const i = layers.findIndex((l) => l.id === id)
  if (i >= 0) layers.splice(i, 1)
}

// ---- BackButton ----

const backLayers: Layer<{ run: Fn }>[] = []
let backBound = false

function onBackPressed() {
  topOf(backLayers)?.value.run()
}

function syncBack() {
  const bb = tg?.BackButton
  if (!bb) return
  if (backLayers.length) {
    // подписываемся один раз: offClick при каждом закрытии модалки — лишний
    // повод потерять обработчик на клиентах, где offClick сравнивает функции
    if (!backBound) {
      bb.onClick?.(onBackPressed)
      backBound = true
    }
    bb.show?.()
  } else {
    bb.hide?.()
  }
}

/**
 * Нативная кнопка «назад» на время жизни экрана или модалки.
 *
 *   useBackButton(onClose, 1)                       // модалка
 *   useBackButton(seg !== 'path' ? reset : null)    // внутренний экран
 *
 * Передать null/undefined — значит «мне кнопка сейчас не нужна»: слой в стек
 * не попадает, и кнопкой владеет экран под нами.
 *
 * Escape намеренно НЕ трогаем: клавиатурное закрытие живёт в useFocusTrap
 * (a11y.ts), второй слушатель на том же событии драться с ним не должен.
 */
export function useBackButton(onBack?: Fn | null, priority = 0) {
  // хендлер держим в ref: он замыкается на свежие пропсы каждый рендер, но
  // перерегистрировать из-за этого слой (и мигать кнопкой) незачем
  const handler = useRef(onBack)
  useEffect(() => {
    handler.current = onBack
  })

  const active = !!onBack
  useEffect(() => {
    if (!active) return
    const id = ++seq
    backLayers.push({ id, priority, value: { run: () => handler.current?.() } })
    syncBack()
    return () => {
      drop(backLayers, id)
      syncBack()
    }
  }, [active, priority])
}

// ---- MainButton ----

export interface MainButtonSpec {
  text: string
  onClick: Fn
  /** false — кнопка видна, но не нажимается (например, идёт запрос) */
  enabled?: boolean
  /** крутилка внутри кнопки */
  progress?: boolean
}

type MainLayer = { text: string; enabled: boolean; progress: boolean; run: Fn }

const mainLayers: Layer<MainLayer>[] = []
let mainBound = false

function onMainPressed() {
  const top = topOf(mainLayers)
  if (top && top.value.enabled) top.value.run()
}

function syncMain() {
  const mb = tg?.MainButton
  if (!mb) return
  const top = topOf(mainLayers)
  if (!top) {
    mb.hide?.()
    return
  }
  if (!mainBound) {
    mb.onClick?.(onMainPressed)
    mainBound = true
  }
  const v = top.value
  mb.setParams?.({ text: v.text, is_active: v.enabled, is_visible: true })
  if (v.progress) mb.showProgress?.(true)
  else mb.hideProgress?.()
  mb.show?.()
}

/**
 * Нативная главная кнопка на ключевом подтверждении.
 *
 *   useMainButton({ text: t('daily_claim'), onClick: claim, enabled: !busy }, 1)
 *
 * Кнопка в карточке остаётся на месте: в браузере (и в клиентах без SDK)
 * MainButton просто нет, и подтверждать было бы нечем.
 */
export function useMainButton(spec?: MainButtonSpec | null, priority = 0) {
  const click = useRef(spec?.onClick)
  useEffect(() => {
    click.current = spec?.onClick
  })

  const active = !!spec
  const text = spec?.text ?? ''
  const enabled = spec?.enabled !== false
  const progress = !!spec?.progress

  const layer = useRef<Layer<MainLayer> | null>(null)
  useEffect(() => {
    if (!active) return
    const l: Layer<MainLayer> = {
      id: ++seq,
      priority,
      value: { text, enabled, progress, run: () => click.current?.() },
    }
    layer.current = l
    mainLayers.push(l)
    syncMain()
    return () => {
      drop(mainLayers, l.id)
      layer.current = null
      syncMain()
    }
    // текст и доступность подхватывает второй эффект — здесь только жизнь слоя
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, priority])

  useEffect(() => {
    const l = layer.current
    if (!l) return
    l.value.text = text
    l.value.enabled = enabled
    l.value.progress = progress
    syncMain()
  }, [text, enabled, progress])
}

// ---------------------------------------------------------------------------
// Подтверждение опасных действий
// ---------------------------------------------------------------------------

function fallbackConfirm(message: string): boolean {
  // window.confirm нет разве что в экзотическом вебвью; там лучше пропустить
  // действие, чем оставить игроку мёртвую кнопку
  return typeof window.confirm === 'function' ? window.confirm(message) : true
}

/**
 * Спросить «точно?» нативным диалогом Telegram; без SDK — системным confirm.
 *
 *   if (!(await askConfirm(t('order_abandon')))) return
 */
export function askConfirm(message: string): Promise<boolean> {
  const show = tg?.showConfirm
  if (typeof show !== 'function') return Promise.resolve(fallbackConfirm(message))
  return new Promise((resolve) => {
    let done = false
    const finish = (ok: boolean) => {
      if (done) return
      done = true
      resolve(ok)
    }
    try {
      // старый клиент бросает WebAppMethodUnsupported прямо из вызова
      show.call(tg, message, (ok: boolean) => finish(!!ok))
    } catch {
      finish(fallbackConfirm(message))
    }
  })
}

// ---------------------------------------------------------------------------
// themeParams
//
// Цвета ходят В КЛИЕНТ, а не из него. Раньше было наоборот: themeParams
// подменяли --bg/--bg2/--dough/--text, и на светлой схеме пекарня становилась
// белым листом с коричневыми прямоугольниками — по картинке нельзя было
// сказать, во что играешь. Кондитерская — это место, и место не меняет цвет
// оттого, что сменилась тема телефона.
//
// Клиентские значения по-прежнему приезжают в CSS как --tg-*: они доступны
// всему, что захочет с ними считаться, просто больше ничего не подменяют.
// ---------------------------------------------------------------------------

/** Какао игры. Держать в согласии с --bg / --bg2 в styles.css. */
const OVEN_BG = '#17100a'
const OVEN_HEADER = '#221609'

export function applyTheme() {
  const root = document.documentElement
  const params = tg?.themeParams || {}
  for (const [key, value] of Object.entries(params)) {
    if (typeof value !== 'string' || !value) continue
    root.style.setProperty('--tg-' + key.replace(/_/g, '-'), value)
  }
  try {
    // Шапка клиента и поле за пределами вебвью красятся под игру: иначе между
    // светлой шапкой Telegram и тёмной сценой остаётся шов, который читается
    // как «страница не догрузилась».
    tg?.setBackgroundColor?.(OVEN_BG)
    tg?.setHeaderColor?.(OVEN_HEADER)
  } catch {
    // метода нет или версия клиента ниже — цвета останутся клиентскими
  }
}

/** Применить тему и следить за её сменой (пользователь переключил на ходу). */
export function initTheme() {
  applyTheme()
  tg?.onEvent?.('themeChanged', applyTheme)
}

// ---------------------------------------------------------------------------
// Диплинки
// ---------------------------------------------------------------------------

/** startParam диплинка: t.me/bot?startapp=X или ?tgWebAppStartParam=X (dev) */
export function startParam(): string {
  return (
    tg?.initDataUnsafe?.start_param ||
    new URLSearchParams(window.location.search).get('tgWebAppStartParam') ||
    ''
  )
}

/** Куда ведёт диплинк: вкладка и (если экран составной) сегмент внутри неё. */
export interface DeepLink {
  tab: string
  seg?: string
}

// Пуши и ссылки из бота присылают сюда имя экрана. Синонимы держим здесь же:
// ссылка «?startapp=orders» должна открывать пекарню, даже если внутри кода
// вкладка называется bakery.
const DEEP_LINKS: Record<string, DeepLink> = {
  clicker: { tab: 'clicker' },
  merge: { tab: 'merge' },
  board: { tab: 'merge' },
  bakery: { tab: 'bakery' },
  orders: { tab: 'bakery' },
  farm: { tab: 'farm' },
  progress: { tab: 'progress', seg: 'path' },
  path: { tab: 'progress', seg: 'path' },
  levels: { tab: 'progress', seg: 'path' },
  quests: { tab: 'progress', seg: 'quests' },
  bp: { tab: 'progress', seg: 'bp' },
  top: { tab: 'progress', seg: 'top' },
  leaderboard: { tab: 'progress', seg: 'top' },
  profile: { tab: 'profile', seg: 'profile' },
  shop: { tab: 'profile', seg: 'shop' },
  stars: { tab: 'profile', seg: 'shop' },
  admin: { tab: 'profile', seg: 'admin' },
}

/**
 * Экран из start-параметра или null, если параметра нет / он не про навигацию
 * (реферальные ссылки вида ref_123 сюда же приходят и должны игнорироваться).
 *
 * Понимает и «merge», и «tab_merge»/«open_merge» — бот волен присылать любой
 * из этих видов, фронт только читает.
 */
export function startTarget(): DeepLink | null {
  const raw = startParam().toLowerCase()
  if (!raw) return null
  const key = raw.replace(/^(tab|open|go)[-_]/, '')
  return DEEP_LINKS[key] ?? null
}
