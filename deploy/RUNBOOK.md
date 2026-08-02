# Runbook: потеря и восстановление данных

Этот файл читают в плохой день. Поэтому здесь только команды и порядок, без
рассуждений: почему всё устроено именно так — в `server/backup.py` и README.

## Цели

| | Значение | Чем обеспечено |
|---|---|---|
| **RPO** (сколько данных теряем) | ≤ 5 минут | непрерывный архив WAL PostgreSQL |
| **RTO** (за сколько поднимаемся) | 30–60 минут | базовая копия + WAL, скрипты ниже |
| Хранение | 7 суточных снимков + 7 суток WAL | `BACKUP_KEEP`, срок жизни в хранилище |

Без WAL-архива RPO равен суткам: авария в 23:50 стирает игрокам весь день.
Один только суточный снимок — это не «почти пять минут», это «до 24 часов».

## Что где лежит

- `backups/` рядом с базой — локальные снимки (`*.bak` для SQLite,
  `*.dump` для PostgreSQL) и `*.sha256` к каждому.
- Хранилище из `BACKUP_UPLOAD_CMD` — те же снимки, зашифрованные AES-256-GCM
  ключом `BACKUP_ENCRYPT_KEY`. **Без ключа файл — мусор.**
- Архив WAL (см. ниже) — там же или в отдельном ведре.

**Ключ шифрования хранится не на этом сервере.** Менеджер паролей + бумажная
копия в сейфе. Ключ, лежащий рядом с бэкапом, не защищает ни от чего.

---

## Сценарий 1. Машина потеряна целиком

1. Поднять новый сервер, поставить пакеты, разложить код и `/opt/cookie/.env`
   (в нём `BOT_TOKEN`, `DATABASE_URL`, `BACKUP_ENCRYPT_KEY`).
2. Забрать последний снимок из хранилища:
   ```sh
   rclone copy r2:cookie-backups/ /tmp/restore/ --max-age 48h
   ls -la /tmp/restore/
   ```
3. Расшифровать:
   ```sh
   python -c "import sys; sys.path.insert(0,'/opt/cookie'); \
     from server import backup; \
     print(backup.decrypt_file(sys.argv[1], sys.argv[2], backup._key()))" \
     /tmp/restore/<файл>.dump.enc /tmp/restore/db.dump
   ```
4. Развернуть:
   ```sh
   # PostgreSQL
   createdb cookie
   pg_restore --dbname=cookie --no-owner --no-privileges -j4 /tmp/restore/db.dump
   # SQLite
   cp /tmp/restore/db.bak /opt/cookie/data.db
   ```
5. Накатить WAL (см. сценарий 2), если архив жив — иначе теряем до суток.
6. `systemctl start cookie-api cookie-scheduler`, затем **обязательно**:
   ```sh
   curl -s localhost:8000/healthz | python -m json.tool
   ```
   Смотреть `backup.age_hours` и `scheduler` — если пусто, задачи не поднялись.
7. При `BOT_MODE=webhook` адрес переехал вместе с машиной — проверить:
   `curl "https://api.telegram.org/bot$BOT_TOKEN/getWebhookInfo"`.

## Сценарий 2. Откат на точку во времени (PITR)

Нужен, когда данные испортились в известный момент: неудачная миграция,
массовый `UPDATE` не с тем `WHERE`, баг раздачи наград.

1. Остановить приложение — **до** восстановления, иначе поверх старых данных
   лягут новые записи: `systemctl stop cookie-api cookie-scheduler`.
2. Развернуть базовую копию, **сделанную ДО** нужного момента (шаги 2–4 выше),
   но не запускать сервер базы.
3. Положить в каталог данных `recovery.signal` и в `postgresql.conf`:
   ```conf
   restore_command = 'rclone cat r2:cookie-wal/%f > %p'
   recovery_target_time = '2026-08-02 14:35:00+00'
   recovery_target_action = 'promote'
   ```
4. Запустить PostgreSQL и дождаться в логе `archive recovery complete`.
5. Проверить данные ДО включения трафика:
   ```sql
   SELECT COUNT(*) FROM users;
   SELECT MAX(created_at) FROM economy_ledger;
   ```
6. Поднять приложение.

Точное время берётся из `economy_ledger`: у порчи всегда есть первая строка.

## Сценарий 3. Плохая миграция схемы

Миграции идут по журналу `schema_migrations` и вперёд. Обратной команды нет —
это сценарий 2 с временем «за минуту до деплоя». До восстановления:
остановить **оба** юнита, иначе `scheduler` продолжит писать.

## Сценарий 4. Испорчен один участок данных

Полное восстановление не нужно. Развернуть снимок в **отдельную** базу
(`cookie_tmp`) и перенести таблицу:

```sh
pg_restore --dbname=cookie_tmp --no-owner -t users /tmp/restore/db.dump
psql cookie -c "UPDATE users u SET cookies = t.cookies
                FROM cookie_tmp.users t WHERE u.user_id = t.user_id
                AND u.user_id IN (...)"
```

Боевую базу под `pg_restore` не подставлять никогда: `--clean` в ней снесёт
таблицы целиком.

---

## Включить WAL-архив (делается один раз, до аварии)

`postgresql.conf`:

```conf
wal_level = replica
archive_mode = on
archive_command = 'rclone copyto %p r2:cookie-wal/%f'
archive_timeout = 300        # сегмент закрывается минимум раз в 5 минут = RPO
```

`archive_timeout = 300` — это и есть RPO: без него неполный сегмент лежит на
умершем диске сколько угодно долго. Перезапуск сервера обязателен
(`archive_mode` не перечитывается через reload).

Проверка, что архив реально работает:

```sh
psql -c "SELECT * FROM pg_stat_archiver"
```

`failed_count` растёт — архива нет, что бы ни говорил конфиг. То же самое
показывает `python -c "from server import backup; print(backup.pitr_status())"`.

## Учения

Задача `backup_drill` раз в сутки берёт свежий снимок, сверяет контрольную
сумму, прогоняет шифрование туда-обратно и открывает базу. Это проверяет файл,
но **не** проверяет человека и не проверяет PITR.

Раз в квартал — вручную, с секундомером, по сценарию 1 на чистой машине.
Записывать фактический RTO. Учения, которые никто не проводил, ничем не лучше
бэкапа, который никто не разворачивал.

## Алерты, по которым сюда приходят

| Метрика | Порог | Что случилось |
|---|---|---|
| `job_last_ok_age_seconds{job="db_backup"}` | > 2 суток | снимков нет |
| `job_last_ok_age_seconds{job="backup_drill"}` | > 2 суток | учения молчат |
| `backup_age_seconds` | > 172800 | файл не появился, хоть задача и «прошла» |
| `job_fails_total{job="db_backup"}` | растёт | место, права или хранилище |
| `pg_stat_archiver.failed_count` | растёт | RPO уехал с 5 минут на сутки |
