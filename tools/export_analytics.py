"""Выгрузка сырых событий аналитики в NDJSON. Запускается на машине с базой.

Зачем скрипт, если есть ручка админки. Ручка нужна человеку — посмотреть, что
там вообще накопилось, и дёрнуть выгрузку руками. Регулярную выгрузку человек
не делает: её делает cron, а cron не умеет подписывать initData Telegram.
Поэтому скрипт ходит в базу напрямую, но через ТЕ ЖЕ функции
(`game_logic.export_batch` / `mark_exported`), что и ручка, — второй, слегка
другой логики пометки в проекте нет.

Главное свойство — порядок шагов: ЗАБРАТЬ, ЗАПИСАТЬ НА ДИСК, СБРОСИТЬ БУФЕР,
и только потом подтвердить. Пока подтверждения нет, TTL строки не трогает
(bot/notifier._prune_events), поэтому падение в любой точке означает лишний
повтор партии на следующем запуске, а не дыру в данных. Повтор безвреден:
у каждой строки есть `event_id`, по которому приёмник давит дубли.

Запуск (по одному разу в сутки, cron на машине с базой):

    cd /opt/cookie
    venv/bin/python tools/export_analytics.py --out /var/lib/cookie/analytics
    # проверить, не осталось ли хвоста:
    venv/bin/python tools/export_analytics.py --status

Файл именуется по времени старта: analytics-YYYYMMDD-HHMMSS.ndjson. Дальше он
уезжает в хранилище тем же способом, что и бэкапы (rclone/BACKUP_UPLOAD_CMD) —
этот скрипт до отправки не касается намеренно: «положить на диск» и «увезти с
машины» отказывают по-разному, и подтверждать выгрузку до отправки нельзя.

Коды возврата: 0 — выгружено (в том числе «выгружать было нечего»),
1 — ошибка. Непустой код нужен cron'у, чтобы молчание не выглядело успехом.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from server import game_logic as gl                            # noqa: E402


def status() -> int:
    """Сколько ждёт выгрузки и насколько давно. Ничего не меняет."""
    info = gl.export_batch(limit=1)
    print(f"ждут выгрузки:      {info['pending']}")
    print(f"самое старое, суток: {info['oldest_pending_age_days']}")
    print(f"TTL:                 {info['ttl_days']} суток "
          f"(+{info['grace_days']} запаса до принудительного удаления)")
    # Отставание больше TTL означает, что часть событий уже живёт в долг: они
    # ещё целы только благодаря запасу, и это последнее предупреждение
    if info["oldest_pending_age_days"] > info["ttl_days"]:
        print("ВНИМАНИЕ: есть события старше TTL — выгрузка отстаёт, "
              "запас (ANALYTICS_EXPORT_GRACE_DAYS) уже расходуется")
        return 1
    return 0


def run(out_dir: str, batch: int, max_batches: int, dry_run: bool) -> int:
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    path = os.path.join(out_dir, f"analytics-{stamp}.ndjson")

    after_id, total, batches = 0, 0, 0
    fh = None if dry_run else open(path, "w", encoding="utf-8")
    try:
        while batches < max_batches:
            page = gl.export_batch(after_id, batch)
            if not page["rows"]:
                break
            if fh:
                for row in page["rows"]:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                # ФАЙЛ НА ДИСКЕ ДО ПОДТВЕРЖДЕНИЯ. flush + fsync, а не «потом
                # закроем»: подтверждение разрешает TTL удалить строки, и
                # подтвердить данные, лежащие только в буфере процесса, значит
                # потерять их при первом же падении машины
                fh.flush()
                os.fsync(fh.fileno())
            last_id = page["next_after_id"]
            if not dry_run:
                gl.mark_exported(last_id)
            after_id = last_id
            total += page["count"]
            batches += 1
    finally:
        if fh:
            fh.close()

    if dry_run:
        print(f"[dry-run] выгрузилось бы событий: {total}, файл не создан")
        return 0
    if total == 0:
        os.remove(path)
        print("выгружать нечего")
        return 0
    left = gl.export_batch(limit=1)["pending"]
    print(f"выгружено событий: {total} -> {path}")
    print(f"осталось в очереди: {left}"
          + (" (упёрлись в --max-batches, запустить ещё раз)" if left else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="analytics_export",
                    help="каталог для NDJSON-файлов")
    ap.add_argument("--batch", type=int, default=1000,
                    help="строк за один заход в базу")
    ap.add_argument("--max-batches", type=int, default=10_000,
                    help="предохранитель от бесконечного цикла")
    ap.add_argument("--dry-run", action="store_true",
                    help="посчитать, ничего не писать и не подтверждать")
    ap.add_argument("--status", action="store_true",
                    help="только показать отставание очереди")
    args = ap.parse_args()
    if args.status:
        return status()
    try:
        return run(args.out, args.batch, args.max_batches, args.dry_run)
    except Exception as e:                                     # noqa: BLE001
        print(f"выгрузка не удалась: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
