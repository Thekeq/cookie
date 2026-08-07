// Спиннер ВНУТРИ кнопки, пока её запрос летит.
//
// Дизейбл без индикации выглядит как «кнопка сломалась»: на медленной сети
// игрок жмёт, ничего не происходит, он жмёт ещё. Крутилка на месте подписи
// говорит «принято, жду сервер».
//
// Стили инлайновые намеренно: @keyframes spin уже глобальный (styles.css),
// а свой класс сюда не завести — файл стилей сейчас правит другая рука.
// Размер в em, цвет — currentColor: спиннер сам подстраивается под кнопку.
export default function Spinner({ size = '1em' }: { size?: string }) {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-block',
        width: size,
        height: size,
        border: '2px solid currentColor',
        borderTopColor: 'transparent',
        borderRadius: '50%',
        verticalAlign: '-0.15em',
        animation: 'spin 0.7s linear infinite',
      }}
    />
  )
}
