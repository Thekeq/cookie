"""Одноразовый фикс: старый неуникальный индекс daily_quests -> UNIQUE.

INSERT OR IGNORE в _ensure_quest_rows работает только при UNIQUE-ограничении,
иначе строки задания дублируются при каждом заходе. Дубликаты схлопываем,
суммарный прогресс сохраняем (берём MAX, т.к. прогресс размазывался по копиям).
"""
import sqlite3

conn = sqlite3.connect("data.db")
conn.execute("PRAGMA journal_mode=WAL;")

# суммируем прогресс дубликатов в первую строку каждой группы
rows = conn.execute(
    "SELECT user_id, day, quest_key, SUM(progress) p, MAX(claimed) c, MIN(id) keep "
    "FROM daily_quests GROUP BY user_id, day, quest_key HAVING COUNT(*) > 1").fetchall()
for user_id, day, quest_key, p, c, keep in rows:
    conn.execute("UPDATE daily_quests SET progress = ?, claimed = ? WHERE id = ?",
                 (p, c, keep))
    conn.execute("DELETE FROM daily_quests WHERE user_id = ? AND day = ? "
                 "AND quest_key = ? AND id != ?", (user_id, day, quest_key, keep))

conn.execute("DROP INDEX IF EXISTS idx_dq_user_day")
conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_dq_user_day "
             "ON daily_quests(user_id, day, quest_key)")
conn.commit()
print(f"merged {len(rows)} duplicate groups, unique index created")
