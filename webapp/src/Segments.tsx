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
            padding: '9px 0',
            fontSize: 13,
            position: 'relative',
            outline: value === it.key ? '2px solid var(--accent)' : 'none',
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
