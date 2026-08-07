// Форматирование чисел в активной локали.
//
// Раньше это жило в App.tsx и было прибито к 'en': `toLocaleString('en')`
// давал «1,234» русскому игроку, у которого разделитель — пробел, а дробная
// часть — запятая. Локаль берём из i18n (getActiveLang), а не из React-контекста:
// fmt() зовут обычные функции и модули без доступа к контексту.
//
// Сигнатуры совместимы со старыми fmt/fmtRate из App.tsx — App их реэкспортит
// под прежними именами, так что вкладки менять не нужно.
import { getActiveLang, LOCALE_TAG } from './i18n'

// суффиксы до дециллиона: поздние цены доски реально уходят за 1e15,
// и «605062015.2B» вместо «605.1Qa» читалось как поломка
const SUFFIX = ['', 'K', 'M', 'B', 'T', 'Qa', 'Qi', 'Sx', 'Sp', 'Oc', 'No', 'Dc']

const cache = new Map<string, Intl.NumberFormat>()
function nf(digits: number): Intl.NumberFormat {
  const tag = LOCALE_TAG[getActiveLang()]
  const id = `${tag}:${digits}`
  let f = cache.get(id)
  if (!f) {
    f = new Intl.NumberFormat(tag, { minimumFractionDigits: digits, maximumFractionDigits: digits })
    cache.set(id, f)
  }
  return f
}

/** Баланс и цены: «1 234», «12.3K», «605.1Qa». Разделители — из локали. */
export function formatNumber(n: number): string {
  if (!isFinite(n)) return '∞'
  if (n < 1e4) return nf(0).format(Math.floor(n))
  const tier = Math.min(SUFFIX.length - 1, Math.floor(Math.log10(n) / 3))
  return nf(1).format(n / 1000 ** tier) + SUFFIX[tier]
}

// Скорость (cookies/с), а не запас. Отдельно от formatNumber потому, что тот
// округляет вниз до целого — для баланса это правильно (дробных печенек игрок
// не видит), а для скорости это ложь: у бабушки 0.9/с, и в шапке она писалась
// «+0/s», то есть постройка выглядела сломанной сразу после покупки.
export function formatRate(n: number): string {
  if (!isFinite(n)) return '∞'
  if (n > 0 && n < 100) {
    const tag = LOCALE_TAG[getActiveLang()]
    return (Math.round(n * 10) / 10).toLocaleString(tag, { maximumFractionDigits: 1 })
  }
  return formatNumber(n)
}
