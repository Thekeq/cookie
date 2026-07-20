// Объединённая вкладка «Профиль»: профиль / магазин Stars / (админка)
import { useState } from 'react'
import { startParam } from '../api'
import { useGame } from '../App'
import { useT } from '../i18n'
import Segments from '../Segments'
import ProfileTab from './ProfileTab'
import ShopTab from './ShopTab'
import AdminTab from './AdminTab'

export default function ProfileHubTab() {
  const t = useT()
  const { isAdmin } = useGame()
  // диплинк /admin из бота открывает сразу сегмент админки
  const [seg, setSeg] = useState<'profile' | 'shop' | 'admin'>(
    startParam() === 'admin' ? 'admin' : 'profile')

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
