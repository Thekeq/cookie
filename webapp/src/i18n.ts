// Простой i18n без библиотек: словари en/uk/ru, дефолт — английский.
import { createContext, useContext } from 'react'

export type Lang = 'en' | 'uk' | 'ru'

export const LANGS: { code: Lang; label: string; flag: string }[] = [
  { code: 'en', label: 'English', flag: '🇬🇧' },
  { code: 'uk', label: 'Українська', flag: '🇺🇦' },
  { code: 'ru', label: 'Русский', flag: '🇷🇺' },
]

const dict = {
  // общее
  tab_clicker: { en: 'Clicker', uk: 'Клікер', ru: 'Кликер' },
  tab_merge: { en: 'Merge', uk: 'Мердж', ru: 'Мердж' },
  tab_levels: { en: 'Path', uk: 'Шлях', ru: 'Путь' },
  tab_farm: { en: 'Farm', uk: 'Ферма', ru: 'Ферма' },
  tab_bp: { en: 'Pass', uk: 'Пас', ru: 'Пасс' },
  tab_shop: { en: 'Stars', uk: 'Stars', ru: 'Stars' },
  tab_profile: { en: 'Profile', uk: 'Профіль', ru: 'Профиль' },
  tab_admin: { en: 'Admin', uk: 'Адмін', ru: 'Админ' },
  error: { en: 'Error', uk: 'Помилка', ru: 'Ошибка' },
  welcome: { en: 'Welcome to Cookie Merge! 🍪', uk: 'Ласкаво просимо до Cookie Merge! 🍪', ru: 'Добро пожаловать в Cookie Merge! 🍪' },
  offline_income: { en: 'Offline income', uk: 'Дохід офлайн', ru: 'Пассивный доход' },
  open_in_tg: { en: 'Open the app from Telegram', uk: 'Відкрий застосунок з Telegram', ru: 'Открой приложение из Telegram' },
  level: { en: 'Level', uk: 'Рівень', ru: 'Уровень' },

  // кликер
  energy: { en: 'Energy', uk: 'Енергія', ru: 'Энергия' },
  per_click: { en: 'per click', uk: 'за клік', ru: 'за клик' },
  boost_active: { en: 'x2 boost active 🔥', uk: 'буст x2 активний 🔥', ru: 'буст x2 активен 🔥' },
  no_energy: { en: 'Out of energy! Wait or buy more ⚡', uk: 'Енергія скінчилась! Зачекай або купи ⚡', ru: 'Энергия кончилась! Подожди или купи ⚡' },
  click_power: { en: 'Click power', uk: 'Сила кліку', ru: 'Сила клика' },
  next_level_click: { en: 'Next level', uk: 'Наступний рівень', ru: 'Следующий уровень' },
  upgrade_for: { en: 'Upgrade for', uk: 'Прокачати за', ru: 'Прокачать за' },
  click_upgraded: { en: 'Click power upgraded! 💪', uk: 'Силу кліку прокачано! 💪', ru: 'Сила клика прокачана! 💪' },

  // merge
  passive_income: { en: 'Passive income', uk: 'Пасивний дохід', ru: 'Пассивный доход' },
  passive_hint: { en: 'Cookies lvl 3+ generate income', uk: 'Печиво 3+ рівня приносить дохід', ru: 'Печеньки 3+ уровня приносят доход' },
  board_full: { en: 'Board is full', uk: 'Дошка заповнена', ru: 'Доска заполнена' },
  buy_cookie: { en: 'Buy cookie', uk: 'Купити печиво', ru: 'Купить печеньку' },
  merge_hint: { en: 'Drag a cookie onto the same one to merge. Max unlocked:', uk: 'Перетягни печиво на таке саме — вони зіллються. Максимум відкрито:', ru: 'Перетащи печеньку на такую же — они сольются. Максимум открыт:' },
  merged_lvl: { en: 'Created a level {n} cookie!', uk: 'Створено печиво {n} рівня!', ru: 'Создана печенька {n} уровня!' },

  // уровни
  claim_level: { en: '🎉 Claim level {n}!', uk: '🎉 Забрати рівень {n}!', ru: '🎉 Забрать уровень {n}!' },
  level_up: { en: 'Level {n}!', uk: 'Рівень {n}!', ru: 'Уровень {n}!' },
  unlocks: { en: 'Lvl {n} unlocks:', uk: 'Рів. {n} відкриває:', ru: 'Ур. {n} открывает:' },
  xp_max: { en: 'XP (max)', uk: 'XP (макс)', ru: 'XP (макс)' },

  // ферма
  farm_title: { en: '🏭 Cookie Farm', uk: '🏭 Печивна ферма', ru: '🏭 Печенечная ферма' },
  farm_income: { en: 'Farm income', uk: 'Дохід ферми', ru: 'Доход фермы' },
  farm_hint: { en: 'Buildings bake cookies automatically, even offline (up to {n}h)', uk: 'Будівлі печуть печиво автоматично, навіть офлайн (до {n} год)', ru: 'Здания пекут печеньки автоматически, даже оффлайн (до {n}ч)' },
  buildings: { en: 'Buildings', uk: 'Будівлі', ru: 'Здания' },
  upgrades: { en: 'Upgrades', uk: 'Покращення', ru: 'Улучшения' },
  skins: { en: 'Skins', uk: 'Скіни', ru: 'Скины' },
  owned: { en: 'owned', uk: 'є', ru: 'куплено' },
  buy: { en: 'Buy', uk: 'Купити', ru: 'Купить' },
  bought: { en: 'Bought ✅', uk: 'Куплено ✅', ru: 'Куплено ✅' },
  applied: { en: 'Applied', uk: 'Активний', ru: 'Активен' },
  apply: { en: 'Apply', uk: 'Обрати', ru: 'Выбрать' },
  req_level: { en: 'Level {n} required', uk: 'Потрібен {n} рівень', ru: 'Нужен {n} уровень' },
  not_enough: { en: 'Not enough cookies', uk: 'Не вистачає печива', ru: 'Не хватает печенек' },

  // названия зданий/апгрейдов
  b_cursor: { en: 'Cursor', uk: 'Курсор', ru: 'Курсор' },
  b_granny: { en: 'Granny', uk: 'Бабуся', ru: 'Бабушка' },
  b_bakery: { en: 'Bakery', uk: 'Пекарня', ru: 'Пекарня' },
  b_factory: { en: 'Factory', uk: 'Фабрика', ru: 'Фабрика' },
  b_mine: { en: 'Cookie Mine', uk: 'Шахта печива', ru: 'Печенечная шахта' },
  b_portal: { en: 'Portal', uk: 'Портал', ru: 'Портал' },
  b_timelab: { en: 'Time Lab', uk: 'Лабораторія часу', ru: 'Лаборатория времени' },
  u_click_mult: { en: 'Click x{n}', uk: 'Клік x{n}', ru: 'Клик x{n}' },
  u_farm_mult: { en: 'Farm x{n}', uk: 'Ферма x{n}', ru: 'Ферма x{n}' },
  u_energy_cap: { en: '+{n} max energy', uk: '+{n} макс. енергії', ru: '+{n} макс. энергии' },
  u_energy_regen: { en: '+{n}/s energy regen', uk: '+{n}/с реген енергії', ru: '+{n}/с реген энергии' },
  u_passive_mult: { en: 'Merge income x{n}', uk: 'Дохід мерджу x{n}', ru: 'Доход мерджа x{n}' },

  // батл-пасс
  bp_title: { en: '🎖️ Battle Pass · Season {n}', uk: '🎖️ Батл-пас · Сезон {n}', ru: '🎖️ Батл-пасс · Сезон {n}' },
  bp_to_next: { en: 'XP to next level', uk: 'XP до наступного рівня', ru: 'XP до следующего уровня' },
  bp_buy: { en: 'Buy Premium for', uk: 'Купити Premium за', ru: 'Купить Premium за' },
  bp_free: { en: 'Free', uk: 'Безкоштовно', ru: 'Бесплатно' },
  bp_premium_on: { en: 'Premium Pass activated! 🎖️', uk: 'Premium Пас активовано! 🎖️', ru: 'Premium Пасс активирован! 🎖️' },

  // магазин Stars
  stars_hint: { en: 'Purchases with Telegram Stars ⭐ — pay in two taps', uk: 'Покупки за Telegram Stars ⭐ — оплата у два тапи', ru: 'Покупки за Telegram Stars ⭐ — оплата в пару тапов' },
  purchase_ok: { en: 'Purchase "{n}" complete! 🎉', uk: 'Покупку «{n}» завершено! 🎉', ru: 'Покупка «{n}» прошла! 🎉' },

  // профиль
  player: { en: 'Player', uk: 'Гравець', ru: 'Игрок' },
  clicks: { en: 'clicks', uk: 'кліків', ru: 'кликов' },
  merges: { en: 'merges', uk: 'злиттів', ru: 'слияний' },
  friends: { en: 'friends', uk: 'друзів', ru: 'друзей' },
  invite_title: { en: '👥 Invite friends', uk: '👥 Запроси друзів', ru: '👥 Позови друзей' },
  invite_hint: { en: 'You get +{a} 🍪, friend gets +{b} 🍪', uk: 'Тобі +{a} 🍪, другу +{b} 🍪', ru: 'Тебе +{a} 🍪, другу +{b} 🍪' },
  share_link: { en: 'Share link', uk: 'Поділитися посиланням', ru: 'Поделиться ссылкой' },
  promo_title: { en: '🎟️ Promo code', uk: '🎟️ Промокод', ru: '🎟️ Промокод' },
  promo_placeholder: { en: 'Enter code...', uk: 'Введи код...', ru: 'Введи код...' },
  promo_ok: { en: 'Promo activated! +{n} 🍪', uk: 'Промокод активовано! +{n} 🍪', ru: 'Промокод активирован! +{n} 🍪' },
  achievements: { en: '🏆 Achievements', uk: '🏆 Досягнення', ru: '🏆 Достижения' },
  ach_reward: { en: '+{n} 🍪 for achievement!', uk: '+{n} 🍪 за досягнення!', ru: '+{n} 🍪 за достижение!' },
  language: { en: '🌐 Language', uk: '🌐 Мова', ru: '🌐 Язык' },
  sound: { en: '🔊 Sound', uk: '🔊 Звук', ru: '🔊 Звук' },
  sound_on: { en: 'On', uk: 'Увімк.', ru: 'Вкл.' },
  sound_off: { en: 'Off', uk: 'Вимк.', ru: 'Выкл.' },
  music: { en: '🎵 Music', uk: '🎵 Музика', ru: '🎵 Музыка' },
  share_text: { en: 'Play Cookie Merge with me — merge cookies and get bonuses! 🍪', uk: 'Грай зі мною в Cookie Merge — з\'єднуй печиво та отримуй бонуси! 🍪', ru: 'Играй со мной в Cookie Merge — соединяй печеньки и получай бонусы! 🍪' },

  // сгруппированные вкладки
  tab_progress: { en: 'Progress', uk: 'Прогрес', ru: 'Прогресс' },
  seg_path: { en: '🗺️ Path', uk: '🗺️ Шлях', ru: '🗺️ Путь' },
  seg_bp: { en: '🎖️ Pass', uk: '🎖️ Пас', ru: '🎖️ Пасс' },
  seg_top: { en: '🏅 Top', uk: '🏅 Топ', ru: '🏅 Топ' },
  seg_profile: { en: '👤 Profile', uk: '👤 Профіль', ru: '👤 Профиль' },
  seg_shop: { en: '⭐ Stars', uk: '⭐ Stars', ru: '⭐ Stars' },
  seg_admin: { en: '🛠️ Admin', uk: '🛠️ Адмін', ru: '🛠️ Админ' },

  // лидерборд
  tab_top: { en: 'Top', uk: 'Топ', ru: 'Топ' },
  lb_title: { en: '🏅 Leaderboard', uk: '🏅 Таблиця лідерів', ru: '🏅 Таблица лидеров' },
  lb_subtitle: { en: 'Top-100 by cookies earned this season', uk: 'Топ-100 за печивом цього сезону', ru: 'Топ-100 по печенькам за сезон' },
  lb_you: { en: 'You', uk: 'Ти', ru: 'Ты' },
  lb_your_rank: { en: 'Your rank: #{n} of {m}', uk: 'Твоє місце: #{n} з {m}', ru: 'Твоё место: #{n} из {m}' },

  // ежедневная награда
  daily_title: { en: '🎁 Daily reward', uk: '🎁 Щоденна нагорода', ru: '🎁 Ежедневная награда' },
  daily_streak: { en: 'Streak: {n} days', uk: 'Стрік: {n} дн.', ru: 'Стрик: {n} дн.' },
  daily_day: { en: 'Day {n}', uk: 'День {n}', ru: 'День {n}' },
  daily_claim: { en: 'Claim +{n} 🍪', uk: 'Забрати +{n} 🍪', ru: 'Забрать +{n} 🍪' },
  daily_claimed: { en: 'Come back tomorrow!', uk: 'Повертайся завтра!', ru: 'Возвращайся завтра!' },
  daily_hint: { en: 'Claim every day to grow your streak. Miss a day — start over!', uk: 'Забирай щодня, щоб ростити стрік. Пропустиш день — почнеш спочатку!', ru: 'Забирай каждый день, чтобы растить стрик. Пропустишь день — начнёшь заново!' },
  daily_got: { en: 'Day {d} streak! +{n} 🍪', uk: 'Стрік {d} дн.! +{n} 🍪', ru: 'Стрик {d} дн.! +{n} 🍪' },

  // ежедневные задания
  quests_title: { en: '📋 Daily quests', uk: '📋 Щоденні завдання', ru: '📋 Ежедневные задания' },
  quests_hint: { en: 'New quests every day. Rewards: cookies + pass XP!', uk: 'Нові завдання щодня. Нагороди: печиво + XP пасу!', ru: 'Новые задания каждый день. Награды: печеньки + XP пасса!' },
  seg_quests: { en: '📋 Quests', uk: '📋 Завдання', ru: '📋 Задания' },
  quest_reward_got: { en: '+{n} 🍪 +{x} Pass XP!', uk: '+{n} 🍪 +{x} XP пасу!', ru: '+{n} 🍪 +{x} XP пасса!' },
  q_clicks: { en: 'Make {n} clicks', uk: 'Зроби {n} кліків', ru: 'Сделай {n} кликов' },
  q_merges: { en: 'Make {n} merges', uk: 'Зроби {n} злиттів', ru: 'Сделай {n} слияний' },
  q_spawns: { en: 'Buy {n} cookies on the board', uk: 'Купи {n} печива на дошці', ru: 'Купи {n} печенек на доске' },
  q_buildings: { en: 'Buy {n} farm buildings', uk: 'Купи {n} будівель ферми', ru: 'Купи {n} зданий фермы' },
  q_earned: { en: 'Earn {n} cookies', uk: 'Зароби {n} печива', ru: 'Заработай {n} печенек' },

  // сезоны
  season_ends: { en: 'Season ends in {n}', uk: 'Сезон закінчиться через {n}', ru: 'Сезон закончится через {n}' },
  season_num: { en: 'Season {n}', uk: 'Сезон {n}', ru: 'Сезон {n}' },
  lb_season_hint: { en: 'Top-10 get cookie prizes at season end. Progress resets each season!', uk: 'Топ-10 отримають призи наприкінці сезону. Прогрес скидається щосезону!', ru: 'Топ-10 получат призы в конце сезона. Прогресс сбрасывается каждый сезон!' },
  lb_last_season: { en: 'Last season: #{r} — +{n} 🍪', uk: 'Минулий сезон: #{r} — +{n} 🍪', ru: 'Прошлый сезон: #{r} — +{n} 🍪' },
  days_short: { en: '{n}d', uk: '{n}д', ru: '{n}д' },
  hours_short: { en: '{n}h', uk: '{n}год', ru: '{n}ч' },

  // milestone-награды рефералки
  ref_milestones: { en: '🎯 Friend milestones', uk: '🎯 Цілі за друзів', ru: '🎯 Цели за друзей' },
  ref_ms_boost: { en: 'x2 click boost for 24h', uk: 'Буст кліку x2 на 24 год', ru: 'Буст клика x2 на 24 ч' },
  ref_ms_skin: { en: 'Exclusive skin 👑', uk: 'Ексклюзивний скін 👑', ru: 'Эксклюзивный скин 👑' },
  ref_ms_premium: { en: 'Premium Pass free', uk: 'Premium Пас безкоштовно', ru: 'Premium Пасс бесплатно' },
  ref_ms_friends: { en: '{n} friends', uk: '{n} друзів', ru: '{n} друзей' },
  ref_ms_got: { en: 'Milestone reward claimed! 🎉', uk: 'Нагороду за ціль отримано! 🎉', ru: 'Награда за цель получена! 🎉' },

  // канал
  channel_title: { en: '📢 Our channel', uk: '📢 Наш канал', ru: '📢 Наш канал' },
  channel_hint: { en: 'Subscribe and get +{n} 🍪', uk: 'Підпишись та отримай +{n} 🍪', ru: 'Подпишись и получи +{n} 🍪' },
  channel_open: { en: 'Open channel', uk: 'Відкрити канал', ru: 'Открыть канал' },
  channel_check: { en: 'Check & claim', uk: 'Перевірити й забрати', ru: 'Проверить и забрать' },
  channel_got: { en: '+{n} 🍪 for subscribing!', uk: '+{n} 🍪 за підписку!', ru: '+{n} 🍪 за подписку!' },

  // золотая печенька и комбо
  golden_frenzy: { en: 'FRENZY x7 for {n}s! 🔥', uk: 'ШАЛЕНСТВО x7 на {n}с! 🔥', ru: 'БЕЗУМИЕ x7 на {n}с! 🔥' },
  golden_chain: { en: 'Golden cookie! +{n} 🍪', uk: 'Золоте печиво! +{n} 🍪', ru: 'Золотая печенька! +{n} 🍪' },
  combo: { en: 'Combo', uk: 'Комбо', ru: 'Комбо' },

  // престиж
  prestige_title: { en: '✨ Prestige', uk: '✨ Престиж', ru: '✨ Престиж' },
  prestige_hint: { en: 'Reset progress for a permanent +{p}% income per point. Skins, friends, achievements and Stars purchases are kept!', uk: 'Скинь прогрес за постійні +{p}% доходу за очко. Скіни, друзі, досягнення та покупки Stars зберігаються!', ru: 'Сбрось прогресс за постоянные +{p}% дохода за очко. Скины, друзья, достижения и покупки Stars сохраняются!' },
  prestige_now: { en: 'Now: {n} pts · x{m} income', uk: 'Зараз: {n} очок · x{m} доходу', ru: 'Сейчас: {n} очков · x{m} дохода' },
  prestige_gain: { en: 'Prestige for +{n} pts', uk: 'Престиж за +{n} очок', ru: 'Престиж за +{n} очков' },
  prestige_locked: { en: 'Earn {n} 🍪 total to unlock', uk: 'Зароби {n} 🍪 всього, щоб відкрити', ru: 'Заработай {n} 🍪 всего, чтобы открыть' },
  prestige_confirm: { en: 'Reset progress for +{n} prestige points?', uk: 'Скинути прогрес за +{n} очок престижу?', ru: 'Сбросить прогресс за +{n} очков престижа?' },
  prestige_done: { en: 'Prestige {c}! Now x{m} income ✨', uk: 'Престиж {c}! Тепер x{m} доходу ✨', ru: 'Престиж {c}! Теперь x{m} дохода ✨' },

  // шеринг
  share_ach: { en: 'Share 🎉', uk: 'Поділитись 🎉', ru: 'Похвастаться 🎉' },
  share_ach_text: { en: 'I unlocked "{a}" in Cookie Merge! Can you beat me? 🍪', uk: 'Я відкрив «{a}» у Cookie Merge! Здолаєш мене? 🍪', ru: 'Я открыл «{a}» в Cookie Merge! Сможешь круче? 🍪' },
  share_rank_text: { en: "I'm #{n} in Cookie Merge this season! Join and beat me 🍪", uk: 'Я #{n} у Cookie Merge цього сезону! Заходь і обійди мене 🍪', ru: 'Я #{n} в Cookie Merge в этом сезоне! Заходи и обгони меня 🍪' },
  share_prestige_text: { en: 'I just prestiged in Cookie Merge — x{m} income now! 🍪✨', uk: 'Я щойно зробив престиж у Cookie Merge — тепер x{m} доходу! 🍪✨', ru: 'Я сделал престиж в Cookie Merge — теперь x{m} дохода! 🍪✨' },

  // туториал
  tut_lang_title: { en: 'Choose your language', uk: 'Обери мову', ru: 'Выбери язык' },
  tut_skip: { en: 'Skip', uk: 'Пропустити', ru: 'Пропустить' },
  tut_next: { en: 'Next', uk: 'Далі', ru: 'Далее' },
  tut_start: { en: "Let's play! 🍪", uk: 'Грати! 🍪', ru: 'Играть! 🍪' },
  tut_1_title: { en: 'Tap the cookie!', uk: 'Тапай печиво!', ru: 'Тапай печеньку!' },
  tut_1_text: { en: 'Every tap gives you cookies — the main currency. Taps use energy, which refills over time.', uk: 'Кожен тап дає печиво — головну валюту. Тапи витрачають енергію, вона відновлюється з часом.', ru: 'Каждый тап даёт печеньки — основную валюту. Тапы тратят энергию, она восстанавливается со временем.' },
  tut_2_title: { en: 'Merge cookies', uk: "З'єднуй печиво", ru: 'Соединяй печеньки' },
  tut_2_text: { en: 'Buy cookies on the 5×5 board and drag one onto an identical one to merge into a higher level. Level 3+ cookies earn passive income!', uk: 'Купуй печиво на дошці 5×5 і перетягуй одне на таке саме, щоб з\'єднати у вищий рівень. Печиво 3+ рівня приносить пасивний дохід!', ru: 'Покупай печеньки на доске 5×5 и перетаскивай одну на такую же, чтобы соединить в более высокий уровень. Печеньки 3+ уровня приносят пассивный доход!' },
  tut_3_title: { en: 'Build your farm', uk: 'Будуй ферму', ru: 'Строй ферму' },
  tut_3_text: { en: 'Buy buildings that bake cookies automatically — even while you are offline. Upgrades and skins are there too!', uk: 'Купуй будівлі, що печуть печиво автоматично — навіть коли ти офлайн. Там же покращення та скіни!', ru: 'Покупай здания, которые пекут печеньки автоматически — даже когда ты оффлайн. Там же улучшения и скины!' },
  tut_4_title: { en: 'Level up & earn', uk: 'Прокачуйся та заробляй', ru: 'Прокачивайся и зарабатывай' },
  tut_4_text: { en: 'Gain XP, walk the level path, claim achievements and battle pass rewards. Invite friends — you both get bonuses!', uk: 'Отримуй XP, крокуй шляхом рівнів, забирай досягнення та нагороди батл-пасу. Клич друзів — бонуси обом!', ru: 'Получай XP, иди по тропинке уровней, забирай достижения и награды батл-пасса. Зови друзей — бонусы обоим!' },
} satisfies Record<string, Record<Lang, string>>

export type TKey = keyof typeof dict

export function translate(lang: Lang, key: TKey, vars?: Record<string, string | number>): string {
  let s: string = dict[key]?.[lang] ?? dict[key]?.en ?? key
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replace(`{${k}}`, String(v))
  return s
}

export function loadLang(): Lang {
  const saved = localStorage.getItem('lang')
  if (saved === 'en' || saved === 'uk' || saved === 'ru') return saved
  return 'en' // дефолт — английский
}

export function saveLang(lang: Lang) {
  localStorage.setItem('lang', lang)
}

export const LangCtx = createContext<{ lang: Lang; setLang: (l: Lang) => void }>({
  lang: 'en',
  setLang: () => {},
})

export function useT() {
  const { lang } = useContext(LangCtx)
  return (key: TKey, vars?: Record<string, string | number>) => translate(lang, key, vars)
}
