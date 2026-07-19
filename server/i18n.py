"""Локализация серверных текстов: бот (/start, оплата, пуши), магазин, ачивки.

Язык юзера хранится в users.lang (синкается из Mini App), для /start до
регистрации берём language_code из Telegram. Ошибки API переводятся на фронте
по кодам err_* (см. webapp/src/i18n.ts).
"""

LANGS = ("en", "uk", "ru")


def norm_lang(code: str | None) -> str:
    code = (code or "en").lower()[:2]
    return code if code in LANGS else "en"


T: dict[str, dict[str, str]] = {
    # ---------- бот ----------
    "start_text": {
        "en": "🍪 <b>Cookie Merge</b>\n\nMerge cookies, level up your clicker, "
              "invite friends and earn rewards!\n\n",
        "uk": "🍪 <b>Cookie Merge</b>\n\nЗ'єднуй печиво, прокачуй клікер, "
              "клич друзів та отримуй нагороди!\n\n",
        "ru": "🍪 <b>Cookie Merge</b>\n\nСоединяй печеньки, качай кликер, "
              "зови друзей и получай награды!\n\n",
    },
    "start_go": {
        "en": "Tap the button and let's go 👇",
        "uk": "Тисни кнопку і погнали 👇",
        "ru": "Жми кнопку и погнали 👇",
    },
    "start_play": {"en": "🍪 Play!", "uk": "🍪 Грати!", "ru": "🍪 Играть!"},
    "dev_link": {
        "en": "🛠 <b>Dev link</b> (open in a browser on this machine):",
        "uk": "🛠 <b>Dev-посилання</b> (відкрий у браузері на цій машині):",
        "ru": "🛠 <b>Dev-ссылка</b> (открой в браузере на этой машине):",
    },
    "pay_ok": {
        "en": "✅ Purchase <b>{title}</b> activated! Thank you 🍪",
        "uk": "✅ Покупку <b>{title}</b> активовано! Дякуємо 🍪",
        "ru": "✅ Покупка <b>{title}</b> активирована! Спасибо 🍪",
    },
    # ---------- пуши ----------
    "notif_streak": {
        "en": "🔥 Your {days}-day streak burns out in a couple of hours!\n"
              "Come back and claim the daily reward 🍪",
        "uk": "🔥 Твій стрік {days} дн. згорить за пару годин!\n"
              "Зайди та забери щоденну нагороду 🍪",
        "ru": "🔥 Твой стрик {days} дн. сгорит через пару часов!\n"
              "Зайди и забери ежедневную награду 🍪",
    },
    "notif_farm": {
        "en": "🏭 The farm is full! Cookies stopped piling up.\n"
              "Collect your income and restart production 🍪",
        "uk": "🏭 Ферма забита! Печиво більше не накопичується.\n"
              "Забери дохід і запусти виробництво знову 🍪",
        "ru": "🏭 Ферма забита! Печеньки больше не копятся.\n"
              "Забери доход и запусти производство снова 🍪",
    },
    "notif_energy": {
        "en": "⚡ Energy fully restored — time to click some cookies! 🍪",
        "uk": "⚡ Енергію повністю відновлено — час наклікати печива! 🍪",
        "ru": "⚡ Энергия полностью восстановлена — время накликать печенек! 🍪",
    },
    # ---------- магазин Stars ----------
    "shop_energy_full_t": {"en": "Full energy", "uk": "Повна енергія", "ru": "Полная энергия"},
    "shop_energy_full_d": {
        "en": "Instantly refill your energy",
        "uk": "Миттєво відновити енергію",
        "ru": "Мгновенно восстановить энергию",
    },
    "shop_boost_x2_1h_t": {"en": "x2 boost (1 hour)", "uk": "Буст x2 (1 год)", "ru": "Буст x2 (1 час)"},
    "shop_boost_x2_1h_d": {
        "en": "Clicks give x2 cookies for 1 hour",
        "uk": "Кліки дають x2 печива 1 годину",
        "ru": "Клики дают x2 печенек 1 час",
    },
    "shop_boost_x2_24h_t": {"en": "x2 boost (24 h)", "uk": "Буст x2 (24 год)", "ru": "Буст x2 (24 ч)"},
    "shop_boost_x2_24h_d": {
        "en": "Clicks give x2 cookies for 24 hours",
        "uk": "Кліки дають x2 печива 24 години",
        "ru": "Клики дают x2 печенек 24 часа",
    },
    "shop_cookies_pack_t": {"en": "Cookie pack", "uk": "Пачка печива", "ru": "Пачка печенек"},
    "shop_cookies_pack_d": {
        "en": "2 hours of your income instantly",
        "uk": "2 години твого доходу миттєво",
        "ru": "2 часа твоего дохода мгновенно",
    },
    "shop_cookies_crate_t": {"en": "Cookie crate", "uk": "Ящик печива", "ru": "Ящик печенек"},
    "shop_cookies_crate_d": {
        "en": "10 hours of your income instantly",
        "uk": "10 годин твого доходу миттєво",
        "ru": "10 часов твоего дохода мгновенно",
    },
    "shop_bp_premium_t": {"en": "Premium Pass", "uk": "Premium Пас", "ru": "Premium Пасс"},
    "shop_bp_premium_d": {
        "en": "Unlocks premium battle pass rewards",
        "uk": "Відкриває premium-нагороди батл-пасу",
        "ru": "Открывает premium-награды батл-пасса",
    },
    # ---------- достижения ----------
    "ach_clicks_100_t": {"en": "Warm-up", "uk": "Розминка", "ru": "Разминка"},
    "ach_clicks_100_d": {"en": "Make 100 clicks", "uk": "Зроби 100 кліків", "ru": "Сделай 100 кликов"},
    "ach_clicks_1000_t": {"en": "Clicker", "uk": "Клікер", "ru": "Кликер"},
    "ach_clicks_1000_d": {"en": "Make 1 000 clicks", "uk": "Зроби 1 000 кліків", "ru": "Сделай 1 000 кликов"},
    "ach_clicks_10000_t": {"en": "Click machine", "uk": "Клікомашина", "ru": "Кликомашина"},
    "ach_clicks_10000_d": {"en": "Make 10 000 clicks", "uk": "Зроби 10 000 кліків", "ru": "Сделай 10 000 кликов"},
    "ach_merges_10_t": {"en": "First merges", "uk": "Перші злиття", "ru": "Первые слияния"},
    "ach_merges_10_d": {"en": "Make 10 merges", "uk": "Зроби 10 злиттів", "ru": "Сделай 10 слияний"},
    "ach_merges_100_t": {"en": "Confectioner", "uk": "Кондитер", "ru": "Кондитер"},
    "ach_merges_100_d": {"en": "Make 100 merges", "uk": "Зроби 100 злиттів", "ru": "Сделай 100 слияний"},
    "ach_merges_500_t": {"en": "Cookie factory", "uk": "Фабрика печива", "ru": "Фабрика печенек"},
    "ach_merges_500_d": {"en": "Make 500 merges", "uk": "Зроби 500 злиттів", "ru": "Сделай 500 слияний"},
    "ach_earned_10k_t": {"en": "Getting rich", "uk": "Багатій", "ru": "Богач"},
    "ach_earned_10k_d": {"en": "Earn 10 000 cookies", "uk": "Зароби 10 000 печива", "ru": "Заработай 10 000 печенек"},
    "ach_earned_100k_t": {"en": "Tycoon", "uk": "Магнат", "ru": "Магнат"},
    "ach_earned_100k_d": {"en": "Earn 100 000 cookies", "uk": "Зароби 100 000 печива", "ru": "Заработай 100 000 печенек"},
    "ach_level_5_t": {"en": "Traveler", "uk": "Мандрівник", "ru": "Путешественник"},
    "ach_level_5_d": {"en": "Reach level 5", "uk": "Досягни 5 рівня", "ru": "Достигни 5 уровня"},
    "ach_level_10_t": {"en": "Pioneer", "uk": "Першопроходець", "ru": "Первопроходец"},
    "ach_level_10_d": {"en": "Reach level 10", "uk": "Досягни 10 рівня", "ru": "Достигни 10 уровня"},
    "ach_refs_1_t": {"en": "Friend", "uk": "Друг", "ru": "Друг"},
    "ach_refs_1_d": {"en": "Invite 1 friend", "uk": "Запроси 1 друга", "ru": "Пригласи 1 друга"},
    "ach_refs_3_t": {"en": "Company", "uk": "Компанія", "ru": "Компания"},
    "ach_refs_3_d": {"en": "Invite 3 friends", "uk": "Запроси 3 друзів", "ru": "Пригласи 3 друзей"},
    "ach_refs_10_t": {"en": "Influencer", "uk": "Лідер думок", "ru": "Лидер мнений"},
    "ach_refs_10_d": {"en": "Invite 10 friends", "uk": "Запроси 10 друзів", "ru": "Пригласи 10 друзей"},
}


def tr(lang: str, key: str, **vars) -> str:
    lang = norm_lang(lang)
    s = T.get(key, {}).get(lang) or T.get(key, {}).get("en") or key
    return s.format(**vars) if vars else s
