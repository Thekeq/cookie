// Закваска: что печь, пока игрока нет.
//
// Оффлайн-кап сам по себе — штраф «не играешь, значит теряешь». Закваска
// разворачивает это: перед выходом игрок выбирает рецепт, и чем он длиннее,
// тем выше множитель оффлайн-дохода, но тем уже окно возврата. Вернулся рано —
// тесто ещё не поднялось, слишком поздно — подгорело.
import { useEffect, useState } from 'react'
import { api } from '../api'
import { useGame } from '../App'
import { useT, useTErr } from '../i18n'
import { sfxBuy, sfxError } from '../sound'
import { useBusy } from '../useBusy'

interface Recipe {
  key: string
  hours: number
  mult: number
  window_h: number
  req_level: number
  unlocked: boolean
}
interface Active {
  key: string | null
  state: 'none' | 'rising' | 'ready' | 'burnt'
  mult: number
  ready_at?: number
  spoils_at?: number
}

const ICON: Record<string, string> = {
  quick: '🥐', classic: '🍞', sourdough: '🥖', festive: '🎂',
}

/** Остаток времени в часах и минутах; текст собирает i18n, а не этот файл */
function untilParts(ts: number): { h: number; m: number } {
  const s = Math.max(0, ts - Date.now() / 1000)
  return { h: Math.floor(s / 3600), m: Math.floor((s % 3600) / 60) }
}

export default function RecipePanel() {
  const t = useT()
  const te = useTErr()
  const { toast, refresh } = useGame()
  const [recipes, setRecipes] = useState<Recipe[]>([])
  const [active, setActive] = useState<Active | null>(null)
  const { busy, run } = useBusy()
  // тик, чтобы обратный отсчёт шёл на глазах, а не замирал до перерисовки
  const [, tick] = useState(0)

  const load = () =>
    api.get('/api/recipes').then((r) => {
      setRecipes(r.recipes)
      setActive(r.active)
    }).catch(() => {})

  useEffect(() => {
    load()
    const timer = setInterval(() => tick((v) => v + 1), 30_000)
    return () => clearInterval(timer)
  }, [])

  const choose = (key: string) =>
    run(key, async () => {
      try {
        const r = await api.post('/api/recipes/set', { key })
        setActive(r.active)
        setRecipes(r.recipes)
        sfxBuy()
        refresh()
      } catch (e: any) {
        sfxError()
        toast(te(e.detail), true)
      }
    })

  if (!active) return null
  const st = active.state
  const fmtLeft = (ts: number) => {
    const { h, m } = untilParts(ts)
    return h > 0 ? t('time_hm', { h, m }) : t('time_m', { m })
  }

  return (
    <div className="card recipe-card">
      <div className="row" style={{ marginBottom: 8 }}>
        <b>{t('recipe_title')}</b>
        {st === 'ready' && <span className="recipe-badge ready">{t('recipe_ready')}</span>}
        {st === 'rising' && <span className="recipe-badge">{t('recipe_rising')}</span>}
        {st === 'burnt' && <span className="recipe-badge burnt">{t('recipe_burnt')}</span>}
      </div>

      {st === 'rising' && active.ready_at && (
        <div className="hint" style={{ marginBottom: 8 }}>
          {t('recipe_wait', { n: fmtLeft(active.ready_at) })}
        </div>
      )}
      {st === 'ready' && active.spoils_at && (
        <div className="hint" style={{ marginBottom: 8 }}>
          {t('recipe_collect', { m: active.mult.toFixed(2), n: fmtLeft(active.spoils_at) })}
        </div>
      )}
      {(st === 'none' || st === 'burnt') && (
        <div className="hint" style={{ marginBottom: 8 }}>{t('recipe_hint')}</div>
      )}

      <div className="recipe-list">
        {recipes.map((r) => (
          <button
            key={r.key}
            className={'recipe-opt' + (active.key === r.key ? ' active' : '')}
            disabled={!r.unlocked || busy === r.key}
            onClick={() => choose(r.key)}
          >
            <span className="recipe-ico">{ICON[r.key] || '🍪'}</span>
            <span className="recipe-name">{t(`recipe_${r.key}` as any)}</span>
            <span className="recipe-mult">x{r.mult.toFixed(2)}</span>
            <span className="recipe-time">
              {r.unlocked ? t('recipe_window', { a: r.hours, b: r.window_h })
                          : t('req_level', { n: r.req_level })}
            </span>
          </button>
        ))}
      </div>
    </div>
  )
}
