// Объединённая вкладка «Профиль»: профиль / магазин Stars / (админка)
import { useState } from 'react'
import { useGame } from '../App'
import { useT } from '../i18n'
import Segments from '../Segments'
import { startTarget, useBackButton } from '../telegram'
import ProfileTab from './ProfileTab'
import ShopTab from './ShopTab'
import AdminTab from './AdminTab'

type Seg = 'profile' | 'shop' | 'admin'

// диплинк из бота (/admin, ссылка на магазин) открывает сразу нужный сегмент
function startSeg(): Seg {
  const link = startTarget()
  return link?.tab === 'profile' && link.seg ? (link.seg as Seg) : 'profile'
}

export default function ProfileHubTab() {
  const t = useT()
  const { isAdmin } = useGame()
  const [seg, setSeg] = useState<Seg>(startSeg)

  // «назад» с магазина/админки возвращает в профиль, а не закрывает приложение
  useBackButton(seg === 'profile' ? null : () => setSeg('profile'))

  const items: { key: 'profile' | 'shop' | 'admin'; label: string }[] = [
    { key: 'profile', label: t('seg_profile') },
    { key: 'shop', label: t('seg_shop') },
    ...(isAdmin ? [{ key: 'admin' as const, label: t('seg_admin') }] : []),
  ]

  return (
    <div>
      <Segments items={items} value={seg} onChange={setSeg} />
      {seg === 'profile' && <ProfileTab />}
      {seg === 'shop' && <ShopTab />}
      {seg === 'admin' && isAdmin && <AdminTab />}
      {seg === 'admin' && !isAdmin && <ProfileTab />}
    </div>
  )
}
