"""Прогон всех наборов проверок одной командой.

Зачем отдельный запускатель. Проверки написаны обычными скриптами, а не под
pytest: каждый файл сам поднимает свою базу во временном каталоге, печатает
строки и в конце выходит с кодом. Это удобно руками, но у CI должен быть ОДИН
вход и один код возврата — иначе «зелёная» сборка означает лишь то, что
последний файл в списке отработал.

Каждый набор идёт ОТДЕЛЬНЫМ процессом, и это не мелочь: файлы подменяют
os.environ до импорта db, перезагружают server.settings и держат свои
подключения. В общем процессе первый импорт зафиксировал бы базу для всех
остальных, и половина проверок молча ушла бы работать в чужой файл.

Боевая база сюда не попадает по построению: DATABASE_URL из окружения
вычищается, а путь к файлу каждый набор задаёт себе сам во временном каталоге.
"""
import os
import subprocess
import sys
import time

# Консоль Windows по умолчанию cp1252: без этого итоговая таблица падает с
# UnicodeEncodeError, и прогон выглядит проваленным при всех зелёных наборах
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SUITES = (
    "test_dialect.py",        # перевод схемы: SQLite -> PostgreSQL
    "test_db_core.py",        # слой доступа к базе: транзакции, ретраи, снимки
    "test_economy.py",        # книга операций, идемпотентность, сверка
    "test_board_economy.py",  # доска, ферма, дуэли, рецепты
    "test_concurrency.py",    # гонки: параллельные клеймы и покупки
    "test_new_features.py",   # ручки API и админка
    "test_platform.py",       # конфиг, разделяемое состояние, роли, метрики
    "test_retention.py",      # удержание: цели, дейлики, возвраты
    "test_chaos.py",          # отказы: Redis, база, диск, Telegram, планировщик
)


def main() -> int:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # .env разработчика и переменные раннера не должны увести проверки в
    # настоящую базу: единственный законный способ проверить PostgreSQL —
    # TEST_DATABASE_URL, его test_dialect читает сам
    env.pop("DATABASE_URL", None)
    env.setdefault("BOT_TOKEN", "123456789:AAtestTOKENtestTOKENtestTOKENtest12")

    results, failed = [], []
    for suite in SUITES:
        print(f"\n{'=' * 70}\n>>> {suite}\n{'=' * 70}", flush=True)
        started = time.perf_counter()
        code = subprocess.call([sys.executable, suite], env=env)
        took = time.perf_counter() - started
        results.append((suite, code, took))
        if code != 0:
            failed.append(suite)

    print(f"\n{'=' * 70}\nИтог\n{'=' * 70}")
    for suite, code, took in results:
        print(f"  {'OK  ' if code == 0 else 'FAIL'} {suite:<24} {took:6.1f} с")
    if failed:
        print(f"\nПровалено наборов: {len(failed)} ({', '.join(failed)})")
        return 1
    print(f"\nВсе {len(results)} наборов пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
