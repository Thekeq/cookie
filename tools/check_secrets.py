"""Проверка: не уехал ли в репозиторий секрет или файл, которого там быть не должно.

Почему своим скриптом, а не готовым действием. Во-первых, оно должно работать
одинаково локально (перед коммитом) и в CI, в том числе на форке без секретов и
лицензий. Во-вторых, проверяются ровно те вещи, которые в ЭТОМ проекте означают
компрометацию: токен бота (доступ к боту навсегда — сменить его значит потерять
всех, кто открыл Mini App по старой ссылке), боевая база (это персональные
данные игроков и их балансы) и .env.

Главное свойство: смотрим то, что ЛЕЖИТ В ИНДЕКСЕ git, а не то, что лежит на
диске. Файл, попавший в коммит, удалить следующим коммитом уже поздно — он
останется в истории, и любой, у кого есть клон, достанет его за секунду.

Код возврата 1 — находка. Запуск: python tools/check_secrets.py
"""
import re
import subprocess
import sys

# Консоль Windows по умолчанию cp1252, и сообщение о находке упало бы с
# UnicodeEncodeError вместо того, чтобы её показать
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Имена, которых в индексе быть не должно ни под каким видом.
FORBIDDEN_NAMES = (
    ".env",
    "data.db", "data.db-wal", "data.db-shm",
    "id_rsa", "id_ed25519", ".pypirc", ".npmrc",
)

# Расширения: снимок базы, ключ, архив бэкапа.
FORBIDDEN_SUFFIXES = (".db", ".db.bak", ".sqlite", ".sqlite3",
                      ".pem", ".key", ".p12", ".pfx", ".keystore", ".dump")

# Файлы, которые СОДЕРЖАТ примеры и потому исключены из поиска по содержимому:
# в .env.example токен обязан выглядеть как токен.
CONTENT_SKIP = {".env.example", "tools/check_secrets.py"}

# Отдельная строка-исключение. Нужна там, где поддельный секрет — это и есть
# смысл строки (проверка самого сканера). Именно построчно, а не файлом
# целиком: файл в исключениях перестаёт проверяться навсегда, и настоящий
# секрет, дописанный в него через полгода, никто не увидит.
ALLOW_MARK = "secret-scan-ok"

TEXT_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yml", ".yaml",
                 ".toml", ".md", ".txt", ".html", ".css", ".service", ".sh", ".env")

# Настоящий токен бота: 8-10 цифр, двоеточие, 35 символов base64url. Тестовые
# токены в проверках начинаются с 123456789 и намеренно пропускаются — иначе
# правило пришлось бы выключить целиком.
PATTERNS = (
    (re.compile(r"\b(?!123456789:)\d{8,10}:[A-Za-z0-9_-]{32,}"),
     "похоже на токен бота Telegram"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "похоже на ключ OpenAI"),
    (re.compile(r"\bghp_[A-Za-z0-9]{30,}\b"), "похоже на токен GitHub"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "похоже на ключ AWS"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "приватный ключ"),
    # пароль в строке подключения к НАСТОЯЩЕМУ хосту. Заведомо примерные
    # (example.com, localhost) пропускаем: с ними написаны проверки — в том
    # числе та, что следит, чтобы пароль не уехал в argv к pg_dump
    (re.compile(r"postgres(?:ql)?://[^:\s/]+:[^@\s]{6,}@"
                r"(?!localhost|127\.0\.0\.1|[^/\s]*example\.(?:com|org))"),
     "пароль в строке подключения"),
    (re.compile(r"https://\d+@[a-z0-9.-]*sentry\.io"), "боевой DSN Sentry"),
)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                         check=True).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    problems = []
    for path in tracked_files():
        name = path.rsplit("/", 1)[-1]
        if name in FORBIDDEN_NAMES:
            problems.append(f"{path}: этот файл не должен быть в репозитории")
            continue
        if any(name.endswith(s) for s in FORBIDDEN_SUFFIXES):
            problems.append(f"{path}: база или ключ в репозитории")
            continue
        if path in CONTENT_SKIP or not path.endswith(TEXT_SUFFIXES):
            continue
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
        except OSError as e:
            problems.append(f"{path}: не прочитался ({e})")
            continue
        for num, line in enumerate(text.splitlines(), 1):
            if ALLOW_MARK in line:
                continue
            for rx, what in PATTERNS:
                if rx.search(line):
                    problems.append(f"{path}:{num}: {what}")

    # .gitignore — вторая половина защиты: правило, которое кто-то снял, вернёт
    # боевую базу в индекс уже следующим `git add -A`
    try:
        ignore = open(".gitignore", encoding="utf-8").read()
    except OSError:
        problems.append(".gitignore отсутствует")
        ignore = ""
    for rule in (".env", "*.db", "cookie.log"):
        if rule not in ignore:
            problems.append(f".gitignore: пропало правило {rule}")

    if problems:
        print("НАЙДЕНО:")
        for p in problems:
            print(f"  {p}")
        print("\nЕсли файл уже попал в коммит, удаления следующим коммитом мало: "
              "он остаётся в истории. Секрет считать скомпрометированным и "
              "перевыпустить.")
        return 1
    print(f"Чисто: проверено {len(tracked_files())} файлов в индексе")
    return 0


if __name__ == "__main__":
    sys.exit(main())
