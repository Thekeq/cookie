// Защита от двойного тапа по кнопке, которая шлёт запрос.
//
// Кнопки клейма блокировались только по «хватает ли денег», но не по «запрос
// уже летит»: на медленной сети двойной тап отправлял две награды подряд.
// Сервер теперь отбивает второй клейм условным UPDATE, но игрок при этом
// видел ошибку на ровном месте — правильнее не давать отправить второй раз.
import { useCallback, useRef, useState } from 'react'

export function useBusy() {
  const [busy, setBusy] = useState<string | null>(null)
  const inflight = useRef(false)

  /** Оборачивает обработчик: пока запрос летит, повторный вызов игнорируется. */
  const run = useCallback(async (key: string, fn: () => Promise<unknown>) => {
    if (inflight.current) return
    inflight.current = true
    setBusy(key)
    try {
      await fn()
    } finally {
      inflight.current = false
      setBusy(null)
    }
  }, [])

  return { busy, run, isBusy: busy !== null }
}
