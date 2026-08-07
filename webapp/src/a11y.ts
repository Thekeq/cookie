// Доступность: переиспользуемые хуки.
//
// Модалки в приложении рисовались обычными div-ами: скринридер не понимал,
// что открылся диалог, Tab уезжал на кнопки под backdrop-ом, Escape не
// закрывал, а после закрытия фокус терялся в начале документа. Один хук
// закрывает всё это разом.
import { useEffect, useRef } from 'react'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

/**
 * Ловушка фокуса для модалки.
 *
 * Вешай возвращённый ref на карточку диалога (не на backdrop):
 *   const trap = useFocusTrap(onClose)
 *   <div className="modal-backdrop"><div ref={trap} role="dialog" aria-modal="true">
 *
 * Что делает: переводит фокус внутрь при открытии, зацикливает Tab/Shift+Tab
 * по содержимому, закрывает по Escape (если передан onEscape) и возвращает
 * фокус на элемент, с которого модалку открыли.
 *
 * onEscape можно не передавать (или передать undefined) — тогда диалог по
 * Escape не закрывается, но фокус всё равно заперт.
 */
export function useFocusTrap<T extends HTMLElement = HTMLDivElement>(onEscape?: () => void) {
  const ref = useRef<T>(null)
  // onEscape меняется между рендерами (замыкание на свежие пропсы), но
  // пересоздавать ловушку из-за этого нельзя — иначе фокус прыгает в начало
  const escape = useRef(onEscape)
  useEffect(() => {
    escape.current = onEscape
  }, [onEscape])

  useEffect(() => {
    const node = ref.current
    if (!node) return
    const restoreTo = document.activeElement as HTMLElement | null

    // видимые фокусируемые элементы считаем каждый раз заново: кнопки внутри
    // модалки появляются и блокируются по ходу (disabled во время запроса)
    const items = () =>
      Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      )

    const first = items()[0]
    if (first) first.focus()
    else {
      node.tabIndex = -1
      node.focus()
    }

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (escape.current) {
          e.preventDefault()
          escape.current()
        }
        return
      }
      if (e.key !== 'Tab') return
      const list = items()
      if (!list.length) {
        e.preventDefault()
        return
      }
      const head = list[0]
      const tail = list[list.length - 1]
      const active = document.activeElement as HTMLElement | null
      const outside = !active || !node.contains(active)
      if (e.shiftKey && (outside || active === head)) {
        e.preventDefault()
        tail.focus()
      } else if (!e.shiftKey && (outside || active === tail)) {
        e.preventDefault()
        head.focus()
      }
    }

    // capture: перехватываем раньше обработчиков вкладок под модалкой
    document.addEventListener('keydown', onKey, true)
    return () => {
      document.removeEventListener('keydown', onKey, true)
      if (restoreTo && typeof restoreTo.focus === 'function') restoreTo.focus()
    }
  }, [])

  return ref
}
