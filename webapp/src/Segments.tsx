// Сегмент-переключатель внутри вкладки (как секции в Ферме)
export default function Segments<T extends string>({
  items,
  value,
  onChange,
}: {
  items: { key: T; label: string; badge?: boolean }[]
  value: T
  onChange: (v: T) => void
}) {
  return (
    <div className="row" style={{ marginBottom: 10, gap: 6 }}>
      {items.map((it) => (
        <button
          key={it.key}
          className="btn secondary"
          style={{
            padding: '9px 2px',
            fontSize: 13,
            position: 'relative',
            outline: value === it.key ? '2px solid var(--accent)' : 'none',
            // строго равные доли: без minWidth flex не даёт кнопке ужаться
            // меньше текста ("Завдання" распирала свой сегмент)
            flex: '1 1 0',
            minWidth: 0,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
          onClick={() => onChange(it.key)}
        >
          {it.label}
          {it.badge && <span className="seg-badge" />}
        </button>
      ))}
    </div>
  )
}
