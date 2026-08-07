// Транспорт: таймауты, ретраи, единая классификация ошибок и статус сети.
//
// Зачем отдельным модулем. `api.ts` знает про игру и Telegram (initData, версия
// доски, токены идемпотентности), здесь же — только HTTP: сколько ждать, что
// можно повторить и как назвать поломку. Правило «GET повторяем, POST никогда»
// должно жить в одном месте, иначе каждый вызывающий решает его заново и
// однажды решит неправильно — деньгами игрока.

/** Разновидности ошибки, на которые интерфейс реагирует по-разному.
 *
 *  - `auth`     — 401: initData протух, помогает только переоткрыть приложение;
 *  - `timeout`  — свои часы сдались раньше сервера, ответа не было;
 *  - `offline`  — до сервера не доехали вовсе (сеть, DNS, спящий webview);
 *  - `conflict` — 409: действие ушло с устаревшего экрана, в `state` свежий;
 *  - `client`   — прочие 4xx: это игровой отказ, `detail` несёт код err_*;
 *  - `server`   — 5xx или нечитаемый ответ: наша вина, повтор осмыслен. */
export type ApiErrorKind =
  | 'auth'
  | 'timeout'
  | 'offline'
  | 'conflict'
  | 'client'
  | 'server'

export interface ApiErrorInit {
  kind?: ApiErrorKind
  status?: number
  requestId?: string
  retriable?: boolean
  i18nKey?: string
}

/** Ошибка запроса. Разбор — по полю `kind` (switch), текст — в `detail`. */
export class ApiError extends Error {
  readonly kind: ApiErrorKind
  /** HTTP-код ответа; 0 — ответа не было вовсе (сеть или таймаут). */
  readonly status: number
  /** X-Request-Id последней попытки: с ним строка находится в логах сервера. */
  readonly requestId: string
  /** Повтор имеет смысл (сеть/таймаут/5xx). Повторяем всё равно только GET. */
  readonly retriable: boolean
  /** Ключ словаря i18n для транспортных ошибок; у серверных пусто —
   *  там `detail` уже код вида err_*, его переводит translateError. */
  readonly i18nKey: string

  /** state приходит только с 409: сервер сразу отдаёт актуальный экран */
  constructor(public detail: string, public state?: unknown, init: ApiErrorInit = {}) {
    super(detail)
    this.name = 'ApiError'
    this.kind = init.kind ?? 'server'
    this.status = init.status ?? 0
    this.requestId = init.requestId ?? ''
    this.retriable = init.retriable ?? false
    this.i18nKey = init.i18nKey ?? ''
  }
}

export function isApiError(e: unknown): e is ApiError {
  return e instanceof ApiError
}

/** Вид ошибки для чего угодно, включая не-ApiError (те считаем `server`). */
export function errorKind(e: unknown): ApiErrorKind {
  return e instanceof ApiError ? e.kind : 'server'
}

/** Запрос отменён вызывающим (устаревший refresh). Не ошибка сети и не повод
 *  что-то показывать игроку — поэтому отдельный класс, а не ApiError. */
export class CanceledError extends Error {
  constructor(message = 'canceled') {
    super(message)
    this.name = 'CanceledError'
  }
}

export function isCanceled(e: unknown): e is CanceledError {
  return e instanceof CanceledError
}

// ---------- таймауты и бэкофф ----------

/** Чтение: пользователь смотрит на спиннер, ждать долго незачем. */
export const READ_TIMEOUT_MS = 8_000
/** Запись: на той стороне транзакция в базе, обрывать её дороже, чем ждать. */
export const WRITE_TIMEOUT_MS = 15_000

const BACKOFF_MS = [300, 900, 2700]

/** Джиттер: без него все вкладки, потерявшие сеть одновременно, вернутся тоже
 *  одновременно и добьют сервер, который только встал. */
function jitter(ms: number): number {
  return Math.round(ms * (0.7 + Math.random() * 0.6))
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    if (signal?.aborted) {
      reject(new CanceledError())
      return
    }
    const onAbort = () => {
      clearTimeout(timer)
      reject(new CanceledError())
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal?.addEventListener('abort', onAbort)
  })
}

// ---------- идентификатор запроса ----------

/** uuid запроса для заголовка `X-Request-Id`.
 *
 *  Сервер (`server/obs.py: new_request_id`) принимает чужой идентификатор,
 *  если тот состоит из [A-Za-z0-9-_.] и короче 64 символов, иначе выдаёт свой.
 *  Все три ветки ниже в этот алфавит укладываются, поэтому строка из логов
 *  клиента находится в логах сервера дословно. */
export function newRequestId(): string {
  const c: Crypto | undefined = typeof crypto !== 'undefined' ? crypto : undefined
  if (c && typeof c.randomUUID === 'function') {
    try {
      return c.randomUUID()
    } catch {
      // некоторые webview отдают randomUUID только на https — падаем ниже
    }
  }
  if (c && typeof c.getRandomValues === 'function') {
    const b = c.getRandomValues(new Uint8Array(16))
    let s = ''
    for (let i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, '0')
    return s
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

// ---------- статус сети ----------

export interface NetStatus {
  /** navigator.onLine: браузер уверен, что сеть есть. Врёт в обе стороны, но
   *  это единственный сигнал, доступный до первого запроса. */
  online: boolean
  /** Сколько запросов подряд не доехало (сеть, таймаут, 5xx). Ответ сервера
   *  любого другого кода счётчик обнуляет: связь есть, спорит логика. */
  failures: number
  /** Сеть вроде есть, но запросы валятся — время показать «переподключаемся». */
  reconnecting: boolean
  /** Чем упало в последний раз; null — пока всё хорошо. */
  lastErrorKind: ApiErrorKind | null
}

function initialOnline(): boolean {
  return typeof navigator === 'undefined' || navigator.onLine !== false
}

// Снимок неизменяемый и переиздаётся только при реальном изменении: это
// позволяет подписчику (в том числе useSyncExternalStore) сравнивать ссылки.
let status: NetStatus = {
  online: initialOnline(),
  failures: 0,
  reconnecting: false,
  lastErrorKind: null,
}

const listeners = new Set<(s: NetStatus) => void>()

function publish(next: Omit<NetStatus, 'reconnecting'>) {
  const value: NetStatus = { ...next, reconnecting: next.online && next.failures > 0 }
  if (
    value.online === status.online &&
    value.failures === status.failures &&
    value.reconnecting === status.reconnecting &&
    value.lastErrorKind === status.lastErrorKind
  ) return
  status = value
  // подписчик не должен ронять транспорт: его исключение — его беда
  listeners.forEach((cb) => {
    try {
      cb(status)
    } catch {
      /* игнорируем */
    }
  })
}

/** Текущий снимок. Ссылка стабильна, пока статус не менялся. */
export function getNetStatus(): NetStatus {
  return status
}

/** Подписка на статус сети. Возвращает функцию отписки.
 *  Колбэк сразу НЕ вызывается — стартовое значение берётся из getNetStatus(). */
export function onNetStatus(cb: (s: NetStatus) => void): () => void {
  listeners.add(cb)
  return () => {
    listeners.delete(cb)
  }
}

function noteFailure(kind: ApiErrorKind) {
  publish({ online: status.online, failures: status.failures + 1, lastErrorKind: kind })
}

function noteSuccess() {
  publish({ online: status.online, failures: 0, lastErrorKind: null })
}

if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('online', () => {
    publish({ online: true, failures: status.failures, lastErrorKind: status.lastErrorKind })
  })
  window.addEventListener('offline', () => {
    publish({ online: false, failures: status.failures, lastErrorKind: 'offline' })
  })
}

// ---------- собственно запрос ----------

export interface NetRequestOptions {
  /** undefined — тела нет вовсе (важно для GET) */
  body?: unknown
  headers?: Record<string, string>
  /** отмена снаружи: устаревший refresh, размонтированный экран */
  signal?: AbortSignal
  timeoutMs?: number
  /** только для GET; POST не повторяется никогда, что бы здесь ни стояло */
  retry?: boolean
}

function transportError(canceled: boolean, timedOut: boolean, requestId: string): unknown {
  if (canceled) return new CanceledError()
  if (timedOut) {
    noteFailure('timeout')
    return new ApiError('Request timed out', undefined,
      { kind: 'timeout', status: 0, requestId, retriable: true, i18nKey: 'err_timeout' })
  }
  noteFailure('offline')
  return new ApiError('No connection', undefined,
    { kind: 'offline', status: 0, requestId, retriable: true, i18nKey: 'err_offline' })
}

function httpError(httpStatus: number, data: any, requestId: string): ApiError {
  const detail: string = typeof data?.detail === 'string' ? data.detail : ''
  const state = data?.state
  if (httpStatus === 401)
    return new ApiError(detail || 'initData expired, reopen the app', state,
      { kind: 'auth', status: httpStatus, requestId, i18nKey: 'err_auth_expired' })
  if (httpStatus === 409)
    return new ApiError(detail || 'err_state_conflict', state,
      { kind: 'conflict', status: httpStatus, requestId })
  if (httpStatus >= 500)
    return new ApiError(detail || `HTTP ${httpStatus}`, state,
      { kind: 'server', status: httpStatus, requestId, retriable: true, i18nKey: 'err_server' })
  return new ApiError(detail || `HTTP ${httpStatus}`, state,
    { kind: 'client', status: httpStatus, requestId })
}

async function attempt(method: string, path: string, opts: NetRequestOptions,
                       requestId: string, timeoutMs: number): Promise<any> {
  const ext = opts.signal
  if (ext?.aborted) throw new CanceledError()

  const ctrl = new AbortController()
  let timedOut = false
  let canceled = false
  const onExtAbort = () => {
    canceled = true
    ctrl.abort()
  }
  ext?.addEventListener('abort', onExtAbort)
  // таймер живёт до конца чтения тела: оборванный на середине ответ висит
  // ровно так же, как неотвеченный запрос
  const timer = setTimeout(() => {
    timedOut = true
    ctrl.abort()
  }, timeoutMs)

  try {
    let res: Response
    try {
      res = await fetch(path, {
        method,
        headers: { ...(opts.headers || {}), 'X-Request-Id': requestId },
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
        signal: ctrl.signal,
      })
    } catch {
      throw transportError(canceled, timedOut, requestId)
    }

    let text: string
    try {
      text = await res.text()
    } catch {
      throw transportError(canceled, timedOut, requestId)
    }

    // Битый JSON больше не превращается в пустой объект: раньше сломанный
    // ответ выглядел как успешный и стирал состояние на экране.
    let data: any = {}
    let broken = false
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        broken = true
      }
    }

    if (res.status >= 500) noteFailure('server')
    else noteSuccess()

    if (!res.ok) throw httpError(res.status, broken ? undefined : data, requestId)
    if (broken)
      throw new ApiError('Bad server response', undefined,
        { kind: 'server', status: res.status, requestId, retriable: true, i18nKey: 'err_bad_response' })
    return data
  } finally {
    clearTimeout(timer)
    ext?.removeEventListener('abort', onExtAbort)
  }
}

/** Запрос с таймаутом, ретраями (только GET) и разбором ошибок.
 *
 *  POST не повторяется на этом уровне сознательно: идемпотентность на сервере
 *  есть, но токен операции проброшен не везде, и слепой повтор покупки списал
 *  бы дважды. Где токен есть — повтор делает вызывающий (см. postOnce). */
export async function netRequest<T = any>(method: string, path: string,
                                          opts: NetRequestOptions = {}): Promise<T> {
  const m = method.toUpperCase()
  const isRead = m === 'GET'
  const timeoutMs = opts.timeoutMs ?? (isRead ? READ_TIMEOUT_MS : WRITE_TIMEOUT_MS)
  const canRetry = isRead && opts.retry !== false
  const base = newRequestId()

  for (let i = 0; ; i++) {
    try {
      // ретраи различимы в логах: тот же запрос, видно какая попытка
      const rid = i === 0 ? base : `${base}.r${i}`
      return (await attempt(m, path, opts, rid, timeoutMs)) as T
    } catch (e) {
      const retriable = e instanceof ApiError && e.retriable
      if (!canRetry || !retriable || i >= BACKOFF_MS.length) throw e
      await sleep(jitter(BACKOFF_MS[i]), opts.signal)
    }
  }
}
