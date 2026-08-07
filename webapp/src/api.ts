// Тонкий API-клиент: подписывает каждый запрос initData из Telegram WebApp.
// Транспорт (таймауты, ретраи, классификация ошибок, статус сети) — в net.ts.

import { ApiError, CanceledError, netRequest, newRequestId } from './net'

// Реэкспорт, чтобы вкладкам не пришлось знать про второй модуль:
// `import { api, ApiError } from './api'` продолжает работать как раньше.
export {
  ApiError,
  CanceledError,
  isApiError,
  isCanceled,
  errorKind,
  getNetStatus,
  onNetStatus,
  READ_TIMEOUT_MS,
  WRITE_TIMEOUT_MS,
} from './net'
export type { ApiErrorKind, NetStatus } from './net'

const tg = (window as any).Telegram?.WebApp

// Дев-режим: бот присылает ссылку 127.0.0.1/#tgWebAppData=<initData> —
// обычно telegram-web-app.js сам её парсит, но подстрахуемся и запомним
// initData в sessionStorage, чтобы пережить перезагрузку страницы
function devInitData(): string {
  const m = window.location.hash.match(/tgWebAppData=([^&]+)/)
  if (m) {
    const data = decodeURIComponent(m[1])
    sessionStorage.setItem('dev_init_data', data)
    return data
  }
  return sessionStorage.getItem('dev_init_data') || ''
}

function getInitData(): string {
  return tg?.initData || devInitData()
}

/** startParam диплинка: t.me/bot?startapp=X или ?tgWebAppStartParam=X (dev) */
export function startParam(): string {
  return (
    tg?.initDataUnsafe?.start_param ||
    new URLSearchParams(window.location.search).get('tgWebAppStartParam') ||
    ''
  )
}

export function haptic(style: 'light' | 'medium' | 'heavy' = 'light') {
  tg?.HapticFeedback?.impactOccurred?.(style)
}

export function hapticSuccess() {
  tg?.HapticFeedback?.notificationOccurred?.('success')
}

export function openInvoice(link: string, onPaid: () => void) {
  if (tg?.openInvoice) {
    tg.openInvoice(link, (status: string) => {
      if (status === 'paid') onPaid()
    })
  } else {
    window.open(link, '_blank')
  }
}

export function shareRefLink(botUsername: string, userId: number, text: string) {
  const link = `https://t.me/${botUsername}?startapp=ref_${userId}`
  const url = `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent(text)}`
  if (tg?.openTelegramLink) tg.openTelegramLink(url)
  else window.open(url, '_blank')
}

// Версия раскладки доски, которую игрок сейчас видит. Ход адресуется номерами
// клеток, поэтому «слей 3 и 4», отправленное со старого экрана (вторая сессия
// на другом устройстве успела всё переставить), слило бы не то, что задумано.
// Возвращаем версию серверу и получаем 409 вместо чужого хода.
let boardRevision = -1

function noteRevision(data: any) {
  if (typeof data?.revision?.board === 'number') boardRevision = data.revision.board
}

async function request(method: string, path: string, body?: unknown,
                       withRevision = false, opId = '', signal?: AbortSignal) {
  const initData: string = getInitData()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: 'tma ' + initData,
    // язык интерфейса — сервер локализует тексты (магазин, ачивки) и пуши
    'X-Lang': localStorage.getItem('lang') || 'en',
  }
  if (withRevision && boardRevision >= 0) headers['X-Board-Revision'] = String(boardRevision)
  if (opId) headers['X-Op-Id'] = opId
  try {
    const data = await netRequest<any>(method, path, { body, headers, signal })
    noteRevision(data)
    return data
  } catch (e) {
    // 409 несёт свежее состояние — версию из него принимаем сразу, иначе
    // повторный ход ушёл бы с той же устаревшей и отбивался бы вечно
    if (e instanceof ApiError) noteRevision(e.state)
    throw e
  }
}

const opId = newRequestId

// ---- «побеждает последний» для чтения ----
//
// refresh() зовут таймер, возврат из фона и любая вкладка после действия.
// Два таких чтения одного пути легко пересекаются, и медленный первый ответ
// приходит ПОСЛЕ быстрого второго, откатывая экран к прошлому состоянию.
// Поэтому предыдущий запрос обрывается, а его вызывающий получает результат
// того, кто его вытеснил: отменённый refresh не должен ни падать (его никто
// не ждал руками), ни возвращать устаревшее.

type Waiter = { resolve: (v: any) => void; reject: (e: unknown) => void }
type Settled = { ok: true; value: any } | { ok: false; err: unknown }

interface Flight {
  ctrl: AbortController
  superseded: boolean
  done: Settled | null
  waiters: Waiter[]
}

// ключ — путь запроса; записей столько, сколько разных GET-путей у приложения
const flights = new Map<string, Flight>()

function settle(flight: Flight, done: Settled) {
  flight.done = done
  const waiters = flight.waiters
  flight.waiters = []
  for (const w of waiters) {
    if (done.ok) w.resolve(done.value)
    else w.reject(done.err)
  }
}

/** Дождаться того, кто нас вытеснил, и вернуть его результат. */
function adopt(key: string, fallback: unknown): Promise<any> {
  const cur = flights.get(key)
  if (!cur) return Promise.reject(fallback)
  const done = cur.done
  if (done) return done.ok ? Promise.resolve(done.value) : Promise.reject(done.err)
  return new Promise((resolve, reject) => cur.waiters.push({ resolve, reject }))
}

async function getLatest(path: string): Promise<any> {
  const prev = flights.get(path)
  const flight: Flight = {
    ctrl: new AbortController(),
    superseded: false,
    done: null,
    waiters: [],
  }
  flights.set(path, flight)
  if (prev && !prev.done) {
    prev.superseded = true
    // ожидающих предшественника переводим на себя — цепочка всегда смотрит
    // на самый свежий запрос, каким бы длинным ни был хвост вытеснений
    flight.waiters.push(...prev.waiters)
    prev.waiters = []
    prev.ctrl.abort()
  }
  try {
    const value = await request('GET', path, undefined, false, '', flight.ctrl.signal)
    settle(flight, { ok: true, value })
    return value
  } catch (e) {
    if (flight.superseded && e instanceof CanceledError) return adopt(path, e)
    settle(flight, { ok: false, err: e })
    throw e
  }
}

// Денежное действие: один токен на одно нажатие.
//
// Мобильная сеть теряет ОТВЕТ чаще, чем запрос: награда выдана, до телефона
// не доехала. Без токена повтор упирается в err_claimed, и для игрока это
// выглядит как «награду съели». С токеном сервер отдаёт ровно тот ответ,
// который потерялся, поэтому повторяем сами и молча — но только когда fetch
// умер на сети. Ответ сервера (любой код) — это результат, его не повторяем.
async function postOnce(path: string, body?: unknown) {
  const op = opId()
  try {
    return await request('POST', path, body, false, op)
  } catch (e) {
    // status 0 — ответа не было вообще (сеть или таймаут), только это и
    // повторяем. Любой ответ сервера, включая 5xx, — уже результат:
    // транзакция могла пройти, а токен операции защищает лишь от двойного
    // начисления, не от двойной попытки с новым смыслом.
    if (!(e instanceof ApiError) || e.status !== 0) throw e
    return await request('POST', path, body, false, op)
  }
}

export const api = {
  /** Чтение: таймаут 8с, до трёх повторов, устаревший запрос вытесняется
   *  новым к тому же пути (вызывающий получает результат свежего). */
  get: (path: string) => getLatest(path),
  post: (path: string, body?: unknown) => request('POST', path, body),
  /** Ход по доске: отправляется вместе с версией раскладки. */
  postBoard: (path: string, body?: unknown) => request('POST', path, body, true),
  /** Награда или покупка: повтор не начисляет второй раз (см. postOnce). */
  postOnce,
}

export function initTelegram() {
  tg?.ready?.()
  tg?.expand?.()
  tg?.disableVerticalSwipes?.()
}

export function tgUserId(): number | null {
  return tg?.initDataUnsafe?.user?.id ?? null
}
