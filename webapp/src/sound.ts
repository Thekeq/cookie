// Звуковой движок на Web Audio API: эффекты и фоновая музыка синтезируются
// кодом — никаких аудиофайлов. Настройки звука/музыки живут в localStorage.

let ctx: AudioContext | null = null
let musicGain: GainNode | null = null
let sfxGain: GainNode | null = null
let musicTimer: number | null = null

let sfxOn = localStorage.getItem('sfx') !== '0'
let musicOn = localStorage.getItem('music') !== '0'

function ensureCtx(): AudioContext | null {
  if (!ctx) {
    const AC = window.AudioContext || (window as any).webkitAudioContext
    if (!AC) return null
    ctx = new AC()
    sfxGain = ctx.createGain()
    sfxGain.gain.value = 0.5
    sfxGain.connect(ctx.destination)
    musicGain = ctx.createGain()
    musicGain.gain.value = 0.16
    musicGain.connect(ctx.destination)
  }
  if (ctx.state === 'suspended') ctx.resume()
  return ctx
}

export const isSfxOn = () => sfxOn
export const isMusicOn = () => musicOn

export function toggleSfx(): boolean {
  sfxOn = !sfxOn
  localStorage.setItem('sfx', sfxOn ? '1' : '0')
  return sfxOn
}

export function toggleMusic(): boolean {
  musicOn = !musicOn
  localStorage.setItem('music', musicOn ? '1' : '0')
  if (musicOn) startMusic()
  else stopMusic()
  return musicOn
}

// ---------- эффекты ----------

function blip(freq: number, dur: number, type: OscillatorType, vol = 1, slide = 0) {
  if (!sfxOn) return
  const c = ensureCtx()
  if (!c || !sfxGain) return
  const t = c.currentTime
  const osc = c.createOscillator()
  const g = c.createGain()
  osc.type = type
  osc.frequency.setValueAtTime(freq, t)
  if (slide) osc.frequency.exponentialRampToValueAtTime(Math.max(40, freq + slide), t + dur)
  g.gain.setValueAtTime(vol * 0.6, t)
  g.gain.exponentialRampToValueAtTime(0.001, t + dur)
  osc.connect(g)
  g.connect(sfxGain)
  osc.start(t)
  osc.stop(t + dur)
}

// ---------- сэмплы (webapp/public/sfx, звуки с mixkit.co — free license) ----------

const SAMPLE_FILES = {
  click: '/sfx/click.mp3',     // хрум печенья (Mixkit "Chewing something crunchy")
  merge: '/sfx/merge.mp3',     // сочный поп
  buy: '/sfx/buy.mp3',         // монетка
  error: '/sfx/error.mp3',     // "нельзя" из видеоигр
  fanfare: '/sfx/fanfare.mp3', // победная фанфара
} as const

type SampleKey = keyof typeof SAMPLE_FILES

const buffers: Partial<Record<SampleKey, AudioBuffer>> = {}
let samplesRequested = false

function loadSamples() {
  if (samplesRequested) return
  samplesRequested = true
  const c = ensureCtx()
  if (!c) return
  for (const [key, url] of Object.entries(SAMPLE_FILES) as [SampleKey, string][]) {
    fetch(url)
      .then((r) => r.arrayBuffer())
      .then((ab) => c.decodeAudioData(ab))
      .then((buf) => {
        buffers[key] = buf
      })
      .catch(() => {}) // нет файла/сети — упадём на синт-фолбэк
  }
}

/** Проиграть сэмпл: offset/dur — вырезка из файла, rate — питч, vol — громкость */
function playSample(key: SampleKey, opts: { rate?: number; vol?: number; offset?: number; dur?: number } = {}): boolean {
  if (!sfxOn) return true
  const c = ensureCtx()
  if (!c || !sfxGain) return true
  const buf = buffers[key]
  if (!buf) return false // ещё не загрузился — синт-фолбэк
  const src = c.createBufferSource()
  src.buffer = buf
  src.playbackRate.value = opts.rate ?? 1
  const g = c.createGain()
  g.gain.value = opts.vol ?? 1
  src.connect(g)
  g.connect(sfxGain)
  src.start(c.currentTime, opts.offset ?? 0, opts.dur)
  return true
}

/** Тап по большой печеньке — реальный хрум со случайным питчем (не звучит роботом) */
export function sfxClick() {
  const ok = playSample('click', {
    rate: 0.92 + Math.random() * 0.25,
    vol: 0.9,
    offset: 0.02,
    dur: 0.35, // короткий надкус, а не всё жевание
  })
  if (!ok) blip(500 + Math.random() * 200, 0.07, 'triangle', 0.8, -200)
}

/** Слияние печенек — поп; выше уровень печеньки = чуть выше питч */
export function sfxMerge(level = 2) {
  const ok = playSample('merge', { rate: 1 + Math.min(level, 10) * 0.04, vol: 1 })
  if (!ok) {
    const base = 380 + Math.min(level, 10) * 60
    blip(base, 0.1, 'sine', 0.9)
    setTimeout(() => blip(base * 1.5, 0.16, 'sine', 0.9), 120)
  }
}

/** Покупка / клейм награды — монетка */
export function sfxBuy() {
  const ok = playSample('buy', { vol: 0.9 })
  if (!ok) {
    blip(660, 0.09, 'square', 0.5)
    setTimeout(() => blip(880, 0.14, 'square', 0.5), 80)
  }
}

/** Ошибка / нельзя */
export function sfxError() {
  const ok = playSample('error', { vol: 0.55, dur: 0.6 })
  if (!ok) blip(220, 0.15, 'sawtooth', 0.4, -60)
}

/** Крупное событие: level-up, достижение */
export function sfxFanfare() {
  const ok = playSample('fanfare', { vol: 0.9 })
  if (!ok) {
    const notes = [523, 659, 784, 1047]
    notes.forEach((f, i) => setTimeout(() => blip(f, 0.22, 'triangle', 0.9), i * 110))
  }
}

// ---------- фоновая музыка ----------
// Уютный луп в духе казуалок: пентатоника C-мажор, мягкий "маримбовый" тембр,
// шагающий бас. Генерируется такт за тактом, поэтому весит 0 байт.

const SCALE = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25] // C D E G A C5 D5 E5
const BASS = [130.81, 98.0, 110.0, 146.83] // C3 G2 A2 D3
const STEP = 0.28 // сек на шаг (примерно 107 bpm)
let barIndex = 0

function playNote(freq: number, at: number, dur: number, vol: number) {
  if (!ctx || !musicGain) return
  const osc = ctx.createOscillator()
  const g = ctx.createGain()
  osc.type = 'sine'
  osc.frequency.value = freq
  // "маримба": быстрая атака, плавный спад
  g.gain.setValueAtTime(0.0001, at)
  g.gain.exponentialRampToValueAtTime(vol, at + 0.02)
  g.gain.exponentialRampToValueAtTime(0.0001, at + dur)
  osc.connect(g)
  g.connect(musicGain)
  osc.start(at)
  osc.stop(at + dur + 0.05)
}

function scheduleBar() {
  if (!ctx || !musicOn) return
  const t0 = ctx.currentTime + 0.05
  const bass = BASS[barIndex % BASS.length]
  playNote(bass, t0, STEP * 4, 0.5)
  // мелодия: 8 шагов, детерминированный «случайный» узор от номера такта
  for (let i = 0; i < 8; i++) {
    const seed = (barIndex * 8 + i) * 2654435761 % 100
    if (seed < 62) {
      const note = SCALE[(seed + barIndex) % SCALE.length]
      playNote(note, t0 + i * STEP, STEP * (seed % 3 === 0 ? 1.8 : 0.9), 0.35)
    }
  }
  barIndex++
}

export function startMusic() {
  if (!musicOn) return
  const c = ensureCtx()
  if (!c) return
  if (musicTimer !== null) return
  scheduleBar()
  musicTimer = window.setInterval(scheduleBar, STEP * 8 * 1000)
}

export function stopMusic() {
  if (musicTimer !== null) {
    clearInterval(musicTimer)
    musicTimer = null
  }
}

/** Первый жест пользователя: браузер требует запуска аудио из клика */
export function unlockAudio() {
  ensureCtx()
  loadSamples()
  startMusic()
}
