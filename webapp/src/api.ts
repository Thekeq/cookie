// Тонкий API-клиент: подписывает каждый запрос initData из Telegram WebApp

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

export class ApiError extends Error {
  /** state приходит только с 409: сервер сразу отдаёт актуальный экран */
  constructor(public detail: string, public state?: unknown) {
    super(detail)
  }
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
                       withRevision = false) {
  const initData: string = getInitData()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Authorization: 'tma ' + initData,
    // язык интерфейса — сервер локализует тексты (магазин, ачивки) и пуши
    'X-Lang': localStorage.getItem('lang') || 'en',
  }
  if (withRevision && boardRevision >= 0) headers['X-Board-Revision'] = String(boardRevision)
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    // 409 несёт свежее состояние — версию из него принимаем сразу, иначе
    // повторный ход ушёл бы с той же устаревшей и отбивался бы вечно
    noteRevision(data?.state)
    throw new ApiError(data.detail || `HTTP ${res.status}`, data?.state)
  }
  noteRevision(data)
  return data
}

export const api = {
  get: (path: string) => request('GET', path),
  post: (path: string, body?: unknown) => request('POST', path, body),
  /** Ход по доске: отправляется вместе с версией раскладки. */
  postBoard: (path: string, body?: unknown) => request('POST', path, body, true),
}

export function initTelegram() {
  tg?.ready?.()
  tg?.expand?.()
  tg?.disableVerticalSwipes?.()
}

export function tgUserId(): number | null {
  return tg?.initDataUnsafe?.user?.id ?? null
}
