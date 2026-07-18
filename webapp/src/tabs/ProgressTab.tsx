// Объединённая вкладка «Прогресс»: Путь уровней / Задания / Батл-пасс / Лидерборд
import { useState } from 'react'
import { useGame } from '../App'
import { useT } from '../i18n'
import Segments from '../Segments'
import LevelsTab from './LevelsTab'
import QuestsTab from './QuestsTab'
import BattlePassTab from './BattlePassTab'
import LeaderboardTab from './LeaderboardTab'

export default function ProgressTab() {
  const t = useT()
  const { state } = useGame()
  const [seg, setSeg] = useState<'path' | 'quests' | 'bp' | 'top'>('path')

  return (
    <div>
      <Segments
        items={[
          // бейджи: незабранный уровень / выполненные задания
          { key: 'path', label: t('seg_path'), badge: !!state.claimable_level },
          { key: 'quests', label: t('seg_quests'), badge: state.quests_claimable > 0 },
          { key: 'bp', label: t('seg_bp') },
          { key: 'top', label: t('seg_top') },
        ]}
        value={seg}
        onChange={setSeg}
      />
      {seg === 'path' && <LevelsTab />}
      {seg === 'quests' && <QuestsTab />}
      {seg === 'bp' && <BattlePassTab />}
      {seg === 'top' && <LeaderboardTab />}
    </div>
  )
}
