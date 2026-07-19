import os
import sqlite3
import time
from contextlib import contextmanager


class DataBase:
    def __init__(self, db_file=None):
        # путь можно переопределить (тесты используют временную БД)
        db_file = db_file or os.environ.get("DATABASE_PATH", "data.db")
        # timeout=10 говорит базе: если занято, подожди 10 сек, а не падай сразу
        self.connection = sqlite3.connect(db_file, check_same_thread=False, timeout=10)
        # автокоммит на каждый statement; многошаговые операции — явно через tx()
        self.connection.isolation_level = None

        # Включаем WAL-режим (МЕГА-ВАЖНО для онлайна и скорости)
        self.connection.execute('PRAGMA journal_mode=WAL;')

        # Чтобы получать результаты как словари, а не кортежи (удобнее читать)
        self.connection.row_factory = sqlite3.Row
        self.cursor = self.connection.cursor()
        self._tx_depth = 0

        self.tables_schema = {
            'users': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER UNIQUE',
                'username': 'TEXT',
                'first_name': 'TEXT',
                'lang': 'TEXT DEFAULT "en"',          # язык Mini App (en/uk/ru) для бота/пушей
                'cookies': 'REAL DEFAULT 0',          # основная валюта
                'total_earned': 'REAL DEFAULT 0',     # всего заработано (для ачивок/уровней)
                'total_clicks': 'INTEGER DEFAULT 0',
                'total_merges': 'INTEGER DEFAULT 0',
                'click_level': 'INTEGER DEFAULT 1',   # прокачка силы клика
                'energy': 'REAL DEFAULT 500',
                'energy_updated_at': 'REAL DEFAULT 0',
                'level': 'INTEGER DEFAULT 1',         # уровень на тропинке
                'xp': 'REAL DEFAULT 0',
                'passive_collected_at': 'REAL DEFAULT 0',  # когда забирали пассивный доход
                'referrer_id': 'INTEGER',
                'source_code': 'TEXT',
                'bp_xp': 'REAL DEFAULT 0',
                'bp_premium': 'INTEGER DEFAULT 0',
                'bp_claimed_free': 'TEXT DEFAULT "[]"',     # json-список уровней БП
                'bp_claimed_premium': 'TEXT DEFAULT "[]"',
                'farm_collected_at': 'REAL DEFAULT 0',      # когда забирали доход фермы
                'active_skin': 'TEXT DEFAULT "classic"',    # скин большой печеньки
                'created_at': 'REAL DEFAULT 0',
                # --- сезоны ---
                'season_id': 'INTEGER DEFAULT 0',           # сезон, в котором живут bp_* и season_earned
                'season_earned': 'REAL DEFAULT 0',          # заработано за текущий сезон (лидерборд)
                # --- ежедневная награда ---
                'daily_streak': 'INTEGER DEFAULT 0',        # текущий стрик (дней подряд)
                'daily_claimed_at': 'REAL DEFAULT 0',       # когда забирали дневную награду
                # --- пуши от бота ---
                'last_notified_at': 'REAL DEFAULT 0',
                'last_seen_at': 'REAL DEFAULT 0',           # последний запрос к API
                'notify_blocked': 'INTEGER DEFAULT 0',      # юзер заблокировал бота
                # --- подписка на канал ---
                'channel_claimed': 'INTEGER DEFAULT 0',
                # --- золотая печенька ---
                'golden_next_at': 'REAL DEFAULT 0',     # когда появится следующая
                'golden_expires_at': 'REAL DEFAULT 0',  # пока > now — активна, можно тапнуть
                'golden_effect': 'TEXT',                # эффект активной ("frenzy"/"chain")
                # --- комбо ---
                'combo_mult': 'REAL DEFAULT 1',
                'combo_last_at': 'REAL DEFAULT 0',
                # --- престиж ---
                'prestige_points': 'REAL DEFAULT 0',
                'prestige_count': 'INTEGER DEFAULT 0',
                # --- дневной кап XP за клики ---
                'clicks_day': 'TEXT',                   # 'YYYY-MM-DD' (UTC)
                'clicks_day_count': 'INTEGER DEFAULT 0',
                # --- CPS-лимит (переживает рестарт и мульти-worker) ---
                'cps_ts': 'REAL DEFAULT 0',             # окно анти-чита: время
                'cps_allowance': 'REAL DEFAULT 0',      # окно анти-чита: остаток кликов
            },
            'click_batches': {  # обработанные батчи кликов (дедуп ретраев, TTL ~1ч)
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'batch_id': 'TEXT',
                'created_at': 'REAL DEFAULT 0',
            },
            'farm': {  # здания автофарма: одна строка = тип здания у юзера
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'building_key': 'TEXT',
                'count': 'INTEGER DEFAULT 0',
            },
            'upgrades': {  # купленные одноразовые апгрейды
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'upgrade_key': 'TEXT',
            },
            'skins': {  # купленные скины
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'skin_key': 'TEXT',
            },
            'board': {  # merge-доска: одна строка = занятая клетка
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'cell': 'INTEGER',        # 0..24
                'item_level': 'INTEGER',  # уровень печеньки в клетке
            },
            'referrals': {
                'id': 'INTEGER PRIMARY KEY',
                'referrer_id': 'INTEGER',
                'referred_id': 'INTEGER UNIQUE',
                'created_at': 'REAL DEFAULT 0',
            },
            'promo_codes': {
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT UNIQUE',
                'reward_cookies': 'REAL DEFAULT 0',
                'reward_energy': 'REAL DEFAULT 0',
                'max_uses': 'INTEGER DEFAULT 0',   # 0 = безлимит
                'uses': 'INTEGER DEFAULT 0',
                'active': 'INTEGER DEFAULT 1',
                'created_at': 'REAL DEFAULT 0',
            },
            'promo_redemptions': {
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT',
                'user_id': 'INTEGER',
                'redeemed_at': 'REAL DEFAULT 0',
            },
            'sources': {  # отслеживаемые ссылки t.me/bot?startapp=src_CODE
                'id': 'INTEGER PRIMARY KEY',
                'code': 'TEXT UNIQUE',
                'title': 'TEXT',
                'registrations': 'INTEGER DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
            },
            'achievements': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'key': 'TEXT',
                'claimed': 'INTEGER DEFAULT 0',
            },
            'purchases': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'item_key': 'TEXT',
                'stars_amount': 'INTEGER',
                'tg_payment_id': 'TEXT',
                'status': 'TEXT DEFAULT "pending"',
                'created_at': 'REAL DEFAULT 0',
            },
            'boosts': {
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'boost_key': 'TEXT',
                'expires_at': 'REAL DEFAULT 0',
            },
            'daily_quests': {  # прогресс ежедневных заданий: строка = юзер+день+задание
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'day': 'TEXT',            # 'YYYY-MM-DD' (UTC)
                'quest_key': 'TEXT',
                'progress': 'REAL DEFAULT 0',
                'claimed': 'INTEGER DEFAULT 0',
            },
            'ref_claims': {  # забранные milestone-награды рефералки
                'id': 'INTEGER PRIMARY KEY',
                'user_id': 'INTEGER',
                'milestone_key': 'TEXT',
                'claimed_at': 'REAL DEFAULT 0',
            },
            'season_results': {  # снапшот топа прошедших сезонов + выданные награды
                'id': 'INTEGER PRIMARY KEY',
                'season_id': 'INTEGER',
                'user_id': 'INTEGER',
                'rank': 'INTEGER',
                'earned': 'REAL DEFAULT 0',
                'reward_cookies': 'REAL DEFAULT 0',
                'created_at': 'REAL DEFAULT 0',
            },
        }

        self._auto_migrate()
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_board_user ON board(user_id)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_ach_user ON achievements(user_id)")
        # UNIQUE обязателен: _ensure_quest_rows полагается на INSERT OR IGNORE
        self.cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dq_user_day "
            "ON daily_quests(user_id, day, quest_key)")
        self.cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_season_earned ON users(season_earned)")
        self._dedupe_and_unique(db_file)
        self.connection.commit()

    # пары колонок, которые обязаны быть уникальными; код и так это проверяет,
    # но параллельные запросы могли бы создать дубли — БД теперь не даст
    UNIQUES = {
        "board": ("user_id", "cell"),
        "farm": ("user_id", "building_key"),
        "upgrades": ("user_id", "upgrade_key"),
        "skins": ("user_id", "skin_key"),
        "achievements": ("user_id", "key"),
        "promo_redemptions": ("user_id", "code"),
        "ref_claims": ("user_id", "milestone_key"),
        "season_results": ("season_id", "user_id"),
        "click_batches": ("user_id", "batch_id"),
    }

    def _has_duplicates(self) -> bool:
        for table, cols in self.UNIQUES.items():
            if self.q1(f"SELECT 1 AS x FROM {table} GROUP BY {', '.join(cols)} "
                       f"HAVING COUNT(*) > 1 LIMIT 1"):
                return True
        return bool(self.q1(
            "SELECT 1 AS x FROM purchases WHERE tg_payment_id IS NOT NULL "
            "GROUP BY tg_payment_id HAVING COUNT(*) > 1 LIMIT 1"))

    def _backup(self, db_file: str):
        """Копия базы перед разрушительной миграцией (sqlite backup API)."""
        path = f"{db_file}.pre-dedup-{int(time.time())}.bak"
        dest = sqlite3.connect(path)
        try:
            self.connection.backup(dest)
            print(f"[*] Миграция: найдены дубли, бэкап сохранён в {path}")
        finally:
            dest.close()

    def _dedupe_and_unique(self, db_file: str):
        """Схлопывает дубли С УЧЁТОМ ДАННЫХ (ферма — суммируем количество,
        доска — оставляем лучшую печеньку, ачивки — сохраняем claimed,
        платежи — сохраняем fulfilled), затем вешает UNIQUE-индексы."""
        if self._has_duplicates() and db_file != ":memory:":
            self._backup(db_file)

        # ферма: у выжившей строки — суммарное количество зданий
        self.cursor.execute(
            "UPDATE farm SET count = (SELECT SUM(f2.count) FROM farm f2 "
            " WHERE f2.user_id = farm.user_id AND f2.building_key = farm.building_key) "
            "WHERE id IN (SELECT MIN(id) FROM farm GROUP BY user_id, building_key "
            "             HAVING COUNT(*) > 1)")
        # доска: в клетке выживает печенька максимального уровня
        self.cursor.execute(
            "DELETE FROM board WHERE EXISTS (SELECT 1 FROM board b2 "
            " WHERE b2.user_id = board.user_id AND b2.cell = board.cell "
            " AND (b2.item_level > board.item_level "
            "      OR (b2.item_level = board.item_level AND b2.id < board.id)))")
        # ачивки: если хоть один дубль заклеймлен — сохраняем claimed=1
        self.cursor.execute(
            "UPDATE achievements SET claimed = (SELECT MAX(a2.claimed) FROM achievements a2 "
            " WHERE a2.user_id = achievements.user_id AND a2.key = achievements.key) "
            "WHERE id IN (SELECT MIN(id) FROM achievements GROUP BY user_id, key "
            "             HAVING COUNT(*) > 1)")
        # платежи: fulfilled важнее paid — переносим статус на выжившую строку
        self.cursor.execute(
            "UPDATE purchases SET status = 'fulfilled' "
            "WHERE tg_payment_id IS NOT NULL AND status != 'fulfilled' AND EXISTS "
            "(SELECT 1 FROM purchases p2 WHERE p2.tg_payment_id = purchases.tg_payment_id "
            " AND p2.status = 'fulfilled')")

        for table, cols in self.UNIQUES.items():
            col_list = ", ".join(cols)
            self.cursor.execute(
                f"DELETE FROM {table} WHERE id NOT IN "
                f"(SELECT MIN(id) FROM {table} GROUP BY {col_list})")
            self.cursor.execute(
                f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table}_{'_'.join(cols)} "
                f"ON {table}({col_list})")
        # один Stars-платёж — одна запись (charge_id уникален, NULL допустим)
        self.cursor.execute(
            "DELETE FROM purchases WHERE tg_payment_id IS NOT NULL AND id NOT IN "
            "(SELECT MIN(id) FROM purchases WHERE tg_payment_id IS NOT NULL "
            " GROUP BY tg_payment_id)")
        self.cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_purchases_charge "
            "ON purchases(tg_payment_id) WHERE tg_payment_id IS NOT NULL")

    def _auto_migrate(self):
        """ Умная система: создает таблицы или добавляет новые столбцы на лету """
        for table_name, columns in self.tables_schema.items():
            cols_sql = ", ".join([f"{col} {ctype}" for col, ctype in columns.items()])
            self.cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({cols_sql})")

            self.cursor.execute(f"PRAGMA table_info({table_name})")
            existing_columns = [row['name'] for row in self.cursor.fetchall()]

            for col_name, col_type in columns.items():
                if col_name not in existing_columns:
                    print(f"[*] Миграция: Добавлен новый столбец {col_name} в {table_name}")
                    self.cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}")

        self.connection.commit()

    # ---------- универсальные хелперы ----------

    @contextmanager
    def tx(self):
        """Атомарный блок: все exec() внутри коммитятся одним куском или
        откатываются целиком. Вложенные tx() присоединяются к внешнему."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self._tx_depth = 1
        self.cursor.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._tx_depth = 0

    def q(self, sql, params=()):
        """SELECT: список dict"""
        self.cursor.execute(sql, params)
        return [dict(r) for r in self.cursor.fetchall()]

    def q1(self, sql, params=()):
        """SELECT: одна строка dict или None"""
        self.cursor.execute(sql, params)
        row = self.cursor.fetchone()
        return dict(row) if row else None

    def exec(self, sql, params=()):
        """INSERT/UPDATE/DELETE, возвращает lastrowid.
        Вне tx() — автокоммит; внутри tx() коммитит внешний блок."""
        self.cursor.execute(sql, params)
        if not self._tx_depth:
            self.connection.commit()
        return self.cursor.lastrowid

    # ---------- юзеры ----------

    def get_user(self, user_id):
        return self.q1("SELECT * FROM users WHERE user_id = ?", (user_id,))

    def create_user(self, user_id, username, first_name, referrer_id=None, source_code=None):
        now = time.time()
        self.exec(
            "INSERT OR IGNORE INTO users (user_id, username, first_name, referrer_id, "
            "source_code, energy_updated_at, passive_collected_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, first_name, referrer_id, source_code, now, now, now),
        )
        return self.get_user(user_id)

    def update_user(self, user_id, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.exec(f"UPDATE users SET {cols} WHERE user_id = ?", (*fields.values(), user_id))
