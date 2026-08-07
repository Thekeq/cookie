// Объединённая вкладка «Прогресс»: Путь уровней / Задания / Батл-пасс / Лидерборд
import { useState } from 'react'
import { useGame } from '../App'
import { useT } from '../i18n'
import Segments from '../Segments'
import { startTarget, useBackButton } from '../telegram'
import LevelsTab from './LevelsTab'
import QuestsTab from './QuestsTab'
import BattlePassTab from './BattlePassTab'
import LeaderboardTab from './LeaderboardTab'
import DuelTab from './DuelTab'

type Seg = 'path' | 'quests' | 'bp' | 'top'

// сегмент из диплинка: пуш про задание должен открывать задания, а не путь
function startSeg(): Seg {
  const link = startTarget()
  return link?.tab === 'progress' && link.seg ? (link.seg as Seg) : 'path'
}

export default function ProgressTab() {
  const t = useT()
  const { state } = useGame()
  const [seg, setSeg] = useState<Seg>(startSeg)

  // «назад» с сегмента возвращает на путь уровней, и только потом (слоем ниже)
  // на вкладку кликера
  useBackButton(seg === 'path' ? null : () => setSeg('path'))

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
      {seg === 'top' && <><DuelTab /><LeaderboardTab /></>}
    </div>
  )
}
