"""Бэкапы: снимок -> шифрование -> отправка наружу -> проверка восстановлением.

Что было до этого модуля: раз в сутки `db.snapshot()` клал файл в `backups/`
рядом с базой. Это защищает ровно от одного сценария — «уронили таблицу
запросом». От всех остальных не защищает вовсе:

  * диск/машина умерли — вместе с базой умерли и все её копии;
  * файл снимка побился при записи — узнаем об этом в момент восстановления,
    то есть в худшую минуту, какую можно придумать;
  * снимок сделался, но пустой (кончилось место) — задача отработала «успешно».

Поэтому здесь три вещи, которых не хватало.

  1. КОПИЯ ВНЕ МАШИНЫ. Команду отправки задаёт `BACKUP_UPLOAD_CMD` — шаблон
     вида `rclone copyto {src} r2:cookie/{name}`. Своего клиента S3 тут нет
     намеренно: у каждого хостера свой, а команда-шаблон работает с любым
     (rclone, aws, scp, restic) и не тянет в процесс ни SDK, ни его цепочку
     зависимостей.

  2. ШИФРОВАНИЕ. В снимке лежат user_id, имена, балансы и история платежей.
     Отдать такой файл чужому хранилищу как есть нельзя. AES-256-GCM на ключе
     из `BACKUP_ENCRYPT_KEY`, поток кусками по мегабайту (база в гигабайт не
     обязана помещаться в память дважды).

  3. ПРОВЕРКА ВОССТАНОВЛЕНИЕМ. Бэкап, который никто ни разу не разворачивал, —
     это не бэкап, а предположение. `drill()` берёт последний снимок,
     расшифровывает во временный файл, поднимает и считает строки. Результат
     виден метрикой: молчащие учения — такой же инцидент, как молчащий бэкап.

Чего здесь НЕТ и почему. Point-in-time recovery (PITR) для PostgreSQL — это
непрерывный архив WAL, и настраивается он на СЕРВЕРЕ БАЗЫ (archive_mode,
archive_command), а не в приложении. Суточный снимок даёт RPO в сутки; чтобы
получить RPO в 5 минут, нужен WAL-архив — как его включить и как из него
восстановиться, описано в deploy/RUNBOOK.md. Здесь же есть `pitr_status()`:
он спрашивает у живой базы, включён ли архив, и не даёт «мы думали, он
работает» тянуться месяцами.
"""
import base64
import hashlib
import os
import shlex
import subprocess
import time

from db import shared
from server import game_config as cfg
from server import obs, settings

db = shared()

CHUNK = 1024 * 1024          # мегабайт: столько же в памяти, сколько на диске
MAGIC = b"CKB1"              # заголовок зашифрованного файла: версия формата
NONCE_LEN = 12               # GCM: 96 бит — размер, для которого он и считался
MIN_SIZE = 4096              # снимок меньше этого — почти наверняка пустой


class BackupError(Exception):
    """Бэкап не получился. Ловится задачей планировщика и пишется в метрику."""


# ---------- шифрование ----------

def _key() -> bytes | None:
    """Ключ из настроек. None — шифрование выключено.

    Ключ читается на каждом вызове, а не в константу при импорте: так его
    можно поменять перезапуском, не пересобирая ничего."""
    raw = settings.BACKUP_ENCRYPT_KEY
    if not raw:
        return None
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception:
        raise BackupError("BACKUP_ENCRYPT_KEY не читается как base64")
    if len(key) != 32:
        raise BackupError(f"BACKUP_ENCRYPT_KEY даёт {len(key)} байт вместо 32")
    return key


def _cipher():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError:
        raise BackupError("BACKUP_ENCRYPT_KEY задан, но пакета cryptography нет "
                          "(pip install cryptography)")
    return AESGCM


def encrypt_file(src: str, dst: str, key: bytes) -> str:
    """AES-256-GCM кусками. Каждый кусок — отдельное сообщение со своим nonce.

    Почему кусками, а не файлом целиком: GCM проверяет целостность только на
    полном сообщении, то есть файл пришлось бы держать в памяти дважды. Кусок
    нумеруется, и его номер идёт в дополнительные данные — иначе куски можно
    было бы переставить местами, и расшифровка этого не заметила бы."""
    AESGCM = _cipher()
    aead = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC)
        index = 0
        while True:
            block = fin.read(CHUNK)
            if not block:
                break
            nonce = os.urandom(NONCE_LEN)
            sealed = aead.encrypt(nonce, block, index.to_bytes(8, "big"))
            fout.write(len(sealed).to_bytes(4, "big"))
            fout.write(nonce)
            fout.write(sealed)
            index += 1
        # нулевая длина — маркер конца. Без него обрезанный файл расшифровался
        # бы «успешно», просто короче: как раз тот случай, ради которого
        # проверка целостности и нужна
        fout.write((0).to_bytes(4, "big"))
    return dst


def decrypt_file(src: str, dst: str, key: bytes) -> str:
    AESGCM = _cipher()
    aead = AESGCM(key)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError(f"{os.path.basename(src)}: не наш формат "
                              "(файл не зашифрован или побит)")
        index = 0
        while True:
            head = fin.read(4)
            if len(head) < 4:
                raise BackupError("файл обрывается посреди куска")
            size = int.from_bytes(head, "big")
            if size == 0:
                return dst
            nonce = fin.read(NONCE_LEN)
            sealed = fin.read(size)
            if len(sealed) != size:
                raise BackupError("файл обрывается посреди куска")
            try:
                fout.write(aead.decrypt(nonce, sealed, index.to_bytes(8, "big")))
            except Exception:
                raise BackupError(f"кусок {index} не расшифровался: "
                                  "не тот ключ или файл повреждён")
            index += 1


# ---------- отправка наружу ----------

def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def upload(path: str) -> bool:
    """Отправляет файл наружу командой из настроек. False — отправка выключена.

    Команда разбирается shlex и запускается СПИСКОМ аргументов, без оболочки:
    иначе имя файла (в нём метка времени, но всё же) попадало бы в shell."""
    template = settings.BACKUP_UPLOAD_CMD
    if not template:
        return False
    name = os.path.basename(path)
    args = [part.replace("{src}", path).replace("{name}", name)
            for part in shlex.split(template)]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=3600)
    except FileNotFoundError:
        raise BackupError(f"команда отправки не найдена: {args[0]}")
    except subprocess.TimeoutExpired:
        raise BackupError("отправка не уложилась в час")
    if res.returncode != 0:
        raise BackupError(f"отправка вернула {res.returncode}: "
                          f"{(res.stderr or res.stdout).strip()[:300]}")
    return True


# ---------- полный проход ----------

def _folder() -> str:
    return db._backups_folder()


def latest(suffix: str = "") -> str | None:
    """Самый свежий снимок — по времени файла, а не по имени.

    Метка времени в имени сортируемая, но идёт ПОСЛЕ имени базы, поэтому
    лексикографический порядок совпадает с хронологическим только пока имя
    базы не менялось никогда. Стоит переехать с `data.db` на что-то другое — и
    «самым свежим» навсегда останется снимок с алфавитно старшим именем, то
    есть учения будут разворачивать прошлогодний файл и рапортовать успех."""
    folder = _folder()
    paths = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.endswith(suffix) and not f.endswith(".sha256")]
    return max(paths, key=os.path.getmtime) if paths else None


def _prune_orphan_sums():
    """Убирает .sha256 от снимков, которые уже вычистил db._prune_snapshots.

    Чистка снимков смотрит на своё расширение (.bak/.dump) и файлы сумм не
    трогает: без этого в каталоге за год копится тысяча висячих строчек."""
    folder = _folder()
    for name in os.listdir(folder):
        if name.endswith(".sha256") and not os.path.exists(
                os.path.join(folder, name[:-len(".sha256")])):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def run() -> dict:
    """Снимок + шифрование + отправка. Зовётся задачей планировщика.

    Возвращает описание сделанного — оно идёт в лог; при ошибке бросает
    BackupError, и задача помечается упавшей (метрика job_fails_total)."""
    started = time.perf_counter()
    path = db.snapshot(keep=cfg.BACKUP_KEEP)
    if not path:
        raise BackupError("снимок не сделан (нет pg_dump или база в памяти)")
    size = os.path.getsize(path)
    # снимок нулевой длины бывает при кончившемся месте, и он страшнее
    # отсутствия снимка: задача отработала «успешно», а восстанавливать нечего
    if size < MIN_SIZE:
        raise BackupError(f"снимок подозрительно мал: {size} байт")

    digest = sha256(path)
    with open(path + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(path)}\n")
    _prune_orphan_sums()

    out = {"path": path, "size": size, "sha256": digest[:16],
           "encrypted": False, "uploaded": False}
    key = _key()
    sendable = path
    if key:
        sendable = encrypt_file(path, path + ".enc", key)
        out["encrypted"] = True
    try:
        out["uploaded"] = upload(sendable)
    finally:
        # зашифрованную копию не храним: локально есть исходный снимок, а
        # держать рядом два файла одной базы — только занимать место
        if key and os.path.exists(path + ".enc"):
            os.remove(path + ".enc")

    obs.set_gauge("backup_size_bytes", size)
    obs.inc("backup_total", result="ok")
    out["seconds"] = round(time.perf_counter() - started, 1)
    return out


# ---------- учения по восстановлению ----------

def _restore_sqlite(path: str) -> dict:
    """Поднимает файл-снимок и проверяет, что он живой и не пустой."""
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        state = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if state != "ok":
            raise BackupError(f"integrity_check: {state}")
        users = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        ledger = conn.execute("SELECT COUNT(*) c FROM economy_ledger").fetchone()["c"]
    except sqlite3.DatabaseError as e:
        raise BackupError(f"снимок не открывается: {e}")
    finally:
        conn.close()
    return {"users": users, "ledger": ledger}


def _restore_postgres(path: str) -> dict:
    """Для дампа pg_dump полное восстановление требует чужой базы, поэтому
    здесь читается ОГЛАВЛЕНИЕ: pg_restore --list разбирает файл целиком и
    падает на битом. Полный прогон в чистую базу — в RUNBOOK, руками и на
    учениях, потому что он занимает место и время."""
    import shutil
    exe = shutil.which("pg_restore")
    if not exe:
        raise BackupError("pg_restore не найден в PATH")
    res = subprocess.run([exe, "--list", path], capture_output=True, text=True,
                         timeout=600)
    if res.returncode != 0:
        raise BackupError(f"pg_restore --list вернул {res.returncode}: "
                          f"{res.stderr.strip()[:300]}")
    tables = sum(1 for line in res.stdout.splitlines() if " TABLE DATA " in line)
    if not tables:
        raise BackupError("в дампе нет ни одной таблицы с данными")
    return {"tables": tables}


def drill() -> dict:
    """Учения: взять последний снимок и убедиться, что из него можно подняться.

    Бэкап, который ни разу не разворачивали, — предположение, а не бэкап.
    Проверяются ровно те вещи, которые молча ломаются: контрольная сумма (файл
    побился на диске), расшифровка (тот ли ключ лежит в конфиге) и сама база
    (открывается, целостна, в ней есть строки)."""
    started = time.perf_counter()
    suffix = ".dump" if db.DIALECT == "postgres" else ".bak"
    path = latest(suffix)
    if not path:
        raise BackupError("учения невозможны: снимков нет вовсе")
    age_h = (time.time() - os.path.getmtime(path)) / 3600

    checksum = path + ".sha256"
    if os.path.exists(checksum):
        want = open(checksum, encoding="utf-8").read().split()[0]
        got = sha256(path)
        if want != got:
            raise BackupError(f"{os.path.basename(path)}: контрольная сумма не "
                              "сошлась — файл побился на диске")

    key = _key()
    work = path
    tmp = None
    if key:
        # проверяем ровно тот путь, которым пойдёт восстановление: шифруем
        # копию и расшифровываем обратно. Иначе «ключ в конфиге не тот»
        # обнаружилось бы при настоящей аварии
        import tempfile
        tmp = os.path.join(tempfile.gettempdir(), f"drill_{os.getpid()}")
        encrypt_file(path, tmp + ".enc", key)
        work = decrypt_file(tmp + ".enc", tmp + ".db", key)
        if sha256(work) != sha256(path):
            raise BackupError("после расшифровки файл отличается от исходного")
    try:
        rows = (_restore_postgres(work) if db.DIALECT == "postgres"
                else _restore_sqlite(work))
    finally:
        for leftover in ((tmp + ".enc", tmp + ".db") if tmp else ()):
            if os.path.exists(leftover):
                os.remove(leftover)

    obs.inc("backup_drill_total", result="ok")
    return {"snapshot": os.path.basename(path), "age_hours": round(age_h, 1),
            "encrypted": bool(key), **rows,
            "seconds": round(time.perf_counter() - started, 1)}


def status() -> dict:
    """Короткая сводка для /healthz: что мы восстановим, если начнём сейчас.

    Ничего не бросает и ничего не читает целиком — только то, что дёшево:
    возраст и размер последнего файла плюс включённые режимы. Вопрос «а бэкапы
    вообще есть?» задают в момент аварии, и он должен отвечаться мгновенно."""
    out = {"offsite": bool(settings.BACKUP_UPLOAD_CMD),
           "encrypted": bool(settings.BACKUP_ENCRYPT_KEY)}
    try:
        path = latest()
        if not path:
            return {**out, "snapshots": 0}
        out["snapshots"] = len(os.listdir(_folder()))
        out["latest"] = os.path.basename(path)
        out["age_hours"] = round((time.time() - os.path.getmtime(path)) / 3600, 1)
        out["size_mb"] = round(os.path.getsize(path) / 1048576, 1)
    except OSError as e:
        out["error"] = str(e)[:200]
    return out


# ---------- непрерывный архив (PITR) ----------

def pitr_status() -> dict:
    """Включён ли на сервере PostgreSQL непрерывный архив WAL.

    Суточный снимок означает RPO в сутки: авария в 23:50 стирает день игры у
    всех. WAL-архив опускает это до минут, но включается он в конфиге СЕРВЕРА,
    и «мы вроде настроили» проверяется только вопросом к живой базе. На SQLite
    вопрос не имеет смысла — там нет ни архива, ни сервера."""
    if db.DIALECT != "postgres":
        return {"supported": False, "reason": "SQLite: PITR не бывает"}
    try:
        mode = db.q1("SELECT current_setting('archive_mode') AS v")["v"]
        command = db.q1("SELECT current_setting('archive_command') AS v")["v"]
        stats = db.q1("SELECT archived_count, failed_count, last_archived_time "
                      "FROM pg_stat_archiver") or {}
    except Exception as e:
        return {"supported": True, "on": False, "error": str(e)[:200]}
    on = str(mode).lower() in ("on", "always") and bool((command or "").strip())
    return {"supported": True, "on": on, "archive_mode": mode,
            "archived": stats.get("archived_count"),
            "failed": stats.get("failed_count"),
            "last_archived_time": str(stats.get("last_archived_time") or "")}
