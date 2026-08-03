"""Канареечная выкладка: проверяется решение, а не оболочка вокруг systemd.

Ценность канарейки целиком в одном месте — в решении «откатывать или нет».
Всё остальное (git checkout, systemctl restart, reload nginx) проверяется тем,
что оно либо отработало, либо вернуло ненулевой код. А вот арифметика решения
не проверяется ничем: она принимает разницу двух выгрузок Prometheus и
отвечает словом, и ошибиться в ней можно ровно двумя способами, оба дорогие.

  ПРОПУСТИТЬ ПЛОХОЙ РЕЛИЗ — очевидный. Канарейка сыплет пятисотками, порог
  посчитан от итогов, а не от прироста за окно, и десять свежих отказов тонут
  в миллионе старых успехов. Выкладка едет дальше на всех.

  ОТКАТИТЬ ХОРОШИЙ — менее очевидный и в итоге более вредный. Автооткат,
  который срабатывает на пустом окне, на ночном шуме или на общей аварии,
  приучает выкатывать с выключенной проверкой. Проверка, которую выключают,
  ничем не лучше отсутствующей.

Поэтому здесь по обе стороны: релиз, который обязан быть откачен, и релиз,
который откатывать нельзя, — включая случай «плохо всем, а не канарейке».

Ни systemd, ни nginx, ни сети тут нет: всё чистые функции плюс один прогон
всего цикла в режиме --dry-run.

Запуск: python test_canary.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "deploy"))

import canary  # noqa: E402

ok = fail = 0


def check(name, cond, extra=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  OK  {name}")
    else:
        fail += 1
        print(f"  FAIL {name} {extra}")


def metrics(**rows) -> dict:
    """Собрать выгрузку из коротких описаний: ('200', 12) -> 12 ответов 200."""
    out = {}
    for code, count in rows.get("codes", {}).items():
        out[("http_requests_total",
             (("route", "/api/click"), ("status", code)))] = float(count)
    for edge, count in rows.get("buckets", {}).items():
        le = "+Inf" if edge == math.inf else f"{edge:g}"
        out[("http_request_duration_seconds_bucket", (("le", le),))] = float(count)
    for name, value in rows.get("extra", {}).items():
        out[(name, ())] = float(value)
    for name_labels, value in rows.get("labelled", {}).items():
        out[name_labels] = float(value)
    return out


print("\n=== 1. Разбор выгрузки Prometheus ===")

SAMPLE = """# HELP http_requests_total HTTP-запросы
# TYPE http_requests_total counter
http_requests_total{route="/api/click",status="200"} 1200
http_requests_total{route="/api/click",status="500"} 3
http_requests_total{route="/api/state",status="200"} 800
cache_backend_up 1
http_request_duration_seconds_bucket{le="0.05"} 900
http_request_duration_seconds_bucket{le="+Inf"} 1000
"""

parsed = canary.parse_metrics(SAMPLE)
check("1.1 комментарии и TYPE не попадают в данные",
      all(not k[0].startswith("#") for k in parsed))
check("1.2 метки разобраны вместе с именем",
      parsed[("http_requests_total",
              (("route", "/api/click"), ("status", "500")))] == 3)
check("1.3 метрика без меток тоже читается",
      parsed[("cache_backend_up", ())] == 1)
check("1.4 сумма по всем меткам",
      canary.total(parsed, "http_requests_total") == 2003)
check("1.5 фильтр по метке", canary.total(parsed, "http_requests_total",
                                          route="/api/state") == 800)

# Значение метки — это в том числе маршрут и причина операции, и запятая внутри
# них встречается. Наивный split(",") тут молча теряет метрику, а потерянная
# метрика читается как «отказов не было»
COMMA = 'http_requests_total{route="/api/x,y",status="500"} 7\n'
comma = canary.parse_metrics(COMMA)
check("1.6 запятая внутри значения метки не ломает разбор",
      canary.total(comma, "http_requests_total", status="500") == 7)
check("1.7 битая строка пропускается, а не роняет наблюдателя",
      canary.parse_metrics("мусор без числа\nfoo 1\n")[("foo", ())] == 1)


print("\n=== 2. Квантиль по корзинам ===")

# 1000 наблюдений: 990 быстрых, 10 в хвосте между 1 и 2.5 с
BUCKETS = {0.05: 900.0, 0.1: 950.0, 0.5: 985.0, 1.0: 990.0, 2.5: 1000.0,
           math.inf: 1000.0}
p99 = canary.quantile(BUCKETS, 0.99)
check(f"2.1 p99 попадает в свою корзину ({p99:.2f} с)", 1.0 <= p99 <= 2.5)
check("2.2 p50 в первой корзине", canary.quantile(BUCKETS, 0.50) <= 0.05)
check("2.3 пустая гистограмма — ноль, а не исключение",
      canary.quantile({}, 0.99) == 0.0)
# Хвост за последней границей — единственный случай, где честный ответ
# «бесконечность»: любое конечное число здесь было бы выдумкой в пользу релиза
TAIL = {0.05: 10.0, 10.0: 90.0, math.inf: 100.0}
check("2.4 хвост за последней границей даёт бесконечность, а не последнюю "
      "границу", canary.quantile(TAIL, 0.99) == math.inf)
check("2.5 бесконечность печатается словами, а не 'inf мс'",
      "inf" not in canary.ms(math.inf))


print("\n=== 3. Приросты за окно, а не итоги ===")

before = metrics(codes={"200": 1_000_000, "500": 100})
after = metrics(codes={"200": 1_000_300, "500": 130})
grew, share = canary.status_share(before, after, "5")
check("3.1 считается прирост, а не итог", grew == 30)
check(f"3.2 доля от трафика ОКНА, а не от всей истории ({share:.1%})",
      abs(share - 30 / 330) < 1e-9)

# 429 — работающий лимитер, а не поломка: посчитать его отказом значит
# откатывать релиз каждый раз, когда кто-то тапает быстрее разрешённого
b4 = metrics(codes={"200": 100, "429": 0})
a4 = metrics(codes={"200": 200, "429": 500})
denied, denied_share = canary.status_share(b4, a4, "4", skip=("429",))
check("3.3 429 не считается отказом", denied == 0 and denied_share == 0.0)


print("\n=== 4. Решение: что откатывать ===")

LIMITS = canary.Limits(min_requests=200)


def window(codes_before, codes_after, **rest):
    return (metrics(codes=codes_before, **rest.get("extra_before", {})),
            metrics(codes=codes_after, **rest.get("extra_after", {})))


healthy = window({"200": 1000}, {"200": 1500})
calm = window({"200": 10000}, {"200": 20000})
check("4.1 чистое окно — откатывать нечего",
      canary.verdict(healthy, calm, LIMITS, unready=0) == [])

# Релиз, который никто не потрогал, — не проверенный релиз. Пустое окно ОБЯЗАНО
# быть провалом: иначе ночная выкладка всегда «зелёная», а канарейка становится
# ритуалом
empty = window({"200": 1000}, {"200": 1010})
check("4.2 окно без трафика — провал, а не успех",
      len(canary.verdict(empty, calm, LIMITS, unready=0)) == 1)

broken = window({"200": 1000, "500": 0}, {"200": 1400, "500": 60})
check("4.3 пятисотки выше порога — откат",
      any("5xx" in p for p in canary.verdict(broken, calm, LIMITS, unready=0)))

# Главная защита от вредного автооткага: когда легла база, плохо ВСЕМ. Откат
# релиза в этот момент — второй инцидент поверх первого
fleet_down = window({"200": 10000, "500": 0}, {"200": 14000, "500": 600})
check("4.4 когда парку так же плохо — не откатываем",
      canary.verdict(broken, fleet_down, LIMITS, unready=0) == [])

# …но «парку тоже плохо» не индульгенция: вдесятеро хуже — всё равно откат
worse = window({"200": 1000, "500": 0}, {"200": 1000, "500": 400})
check("4.5 вдесятеро хуже парка — откат даже во время аварии",
      any("5xx" in p for p in canary.verdict(worse, fleet_down, LIMITS,
                                             unready=0)))

# 4xx снаружи выглядит хуже пятисотки: игрок видит не «ошибка сервера», а тихо
# не работающую кнопку
contract = window({"200": 1000}, {"200": 1200, "400": 300})
check("4.6 сломанный договор с фронтом (4xx) — откат",
      any("4xx" in p for p in canary.verdict(contract, calm, LIMITS, unready=0)))

slow = (metrics(codes={"200": 1000}, buckets={0.05: 0, 10.0: 0, math.inf: 0}),
        metrics(codes={"200": 1500}, buckets={0.05: 10, 10.0: 500,
                                              math.inf: 500}))
fast_fleet = (metrics(codes={"200": 0}, buckets={0.05: 0, math.inf: 0}),
              metrics(codes={"200": 5000}, buckets={0.05: 4990,
                                                    math.inf: 5000}))
check("4.7 медленный релиз при быстром парке — откат",
      any("p99" in p for p in canary.verdict(slow, fast_fleet, LIMITS,
                                             unready=0)))

check("4.8 процесс сам сказал «не готов» — откат без разговоров",
      any("readyz" in p for p in canary.verdict(healthy, calm, LIMITS,
                                                unready=2)))

dirty = (metrics(codes={"200": 1000}, extra={"db_dirty_connections_total": 0}),
         metrics(codes={"200": 1500}, extra={"db_dirty_connections_total": 2}))
check("4.9 залипшая транзакция не бывает «немного» — откат на первой же",
      any("залипш" in p for p in canary.verdict(dirty, calm, LIMITS, unready=0)))

CONFLICT = ("economy_ops_total", (("result", "conflict"),))
conflicts = (metrics(codes={"200": 1000}, labelled={CONFLICT: 0}),
             metrics(codes={"200": 1500}, labelled={CONFLICT: 50}))
check("4.10 конфликты идемпотентности выше порога — откат",
      any("идемпотент" in p for p in canary.verdict(conflicts, calm, LIMITS,
                                                    unready=0)))


print("\n=== 5. Апстрим nginx ===")

# У nginx weight=0 — это НЕ «не давать трафик», а ошибка конфигурации. Доля
# ноль обязана выражаться отсутствием строки, иначе откат — это команда,
# которая ничего не откатила и вдобавок уронила reload
zero = canary.upstream_conf("http://127.0.0.1:8000", "http://127.0.0.1:8001", 0)
check("5.1 нулевая доля убирает канарейку из апстрима целиком",
      "8001" not in zero)
check("5.2 weight=0 не появляется никогда", "weight=0" not in zero)
check("5.3 парк остаётся в апстриме при нулевой доле", "8000" in zero)

ten = canary.upstream_conf("http://127.0.0.1:8000", "http://127.0.0.1:8001", 10)
check("5.4 доля 10% — веса 90/10",
      "weight=90" in ten and "weight=10" in ten and "8001" in ten)
# У канарейки max_fails=1: nginx уводит её из ротации сам, не дожидаясь
# наблюдателя, который считает цифры раз в пятнадцать секунд
check("5.5 канарейка выводится из ротации агрессивнее парка",
      "max_fails=1" in ten and "max_fails=3" in ten)


print("\n=== 6. Схема общей базы ===")

# База у канарейки и парка ОДНА, и миграция уезжает на неё вместе с релизом.
# Откат кода схему не вернёт: обратной команды у миграций нет вовсе
MIGRATION_DIFF = """--- a/db.py
+++ b/db.py
@@ -900,0 +901,2 @@
+        if self._migration("add_column_streak"):
+            self.exec("ALTER TABLE users ADD COLUMN streak INTEGER DEFAULT 0")
"""
check("6.1 миграция в дифе видна", len(canary.schema_risk(MIGRATION_DIFF)) == 2)
check("6.2 удалённые строки не считаются добавленными",
      canary.schema_risk("--- a/db.py\n-        ALTER TABLE users\n") == [])
check("6.3 безобидный диф не поднимает тревогу",
      canary.schema_risk("+++ b/db.py\n+    log.info('привет')\n") == [])


print("\n=== 7. Весь цикл вхолостую ===")

# Прогон всей выкладки с --dry-run: ни одна команда не выполняется, но порядок
# шагов и их аргументы проходят через тот же код, что и в бою. Это ловит
# опечатку в имени юнита и перепутанный порядок «снять трафик / погасить
# процесс» — то, что иначе выясняется в плохой день.
canary.MAIN_URL = "http://127.0.0.1:59999"      # заведомо никого нет
canary.CANARY_URL = "http://127.0.0.1:59998"
code = canary.main(["release", "--ref", "v-test", "--dry-run",
                    "--skip-frontend", "--window", "1"])
check("7.1 полный цикл вхолостую доходит до конца", code == 0)

code = canary.main(["rollback", "--dry-run"])
check("7.2 откат работает отдельной командой", code == 0)

code = canary.main(["deploy", "--dry-run"])
check("7.3 без --ref команда отказывается работать", code == 2)

canary.DRY_RUN = False
check("7.4 недоступный адрес — это код 0, а не исключение наблюдателя",
      canary.fetch("http://127.0.0.1:59999/readyz", timeout=1)[0] == 0)

# Адреса приходят из файла юнита. Опечатка в схеме не должна превращать
# наблюдателя в читалку локальных файлов, которая примет /etc/passwd за
# выгрузку метрик и объявит окно пустым
check("7.5 не-HTTP схема отвергается, а не открывается",
      canary.fetch("file:///etc/passwd")[0] == 0
      and "схем" in canary.fetch("file:///etc/passwd")[1])

print(f"\n{ok} passed, {fail} failed")
raise SystemExit(1 if fail else 0)
