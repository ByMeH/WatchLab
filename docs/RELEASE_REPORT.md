# WatchLab — отчёт о релизной проверке (P5)

Дата: 2026-11-26
Область: финальная подготовка релиза WatchLab Desktop (P0–P4 завершены ранее)

## Итоговый вердикт

**Проект НЕ является «полностью проверенным».** Все функциональные проверки пройдены (PASS), однако визуальная проверка интерфейса на реальном рабочем столе выполнена НЕ БЫЛА (NOT RUN): в агентской сессии нет доступа к X11-дисплею пользовательской сессии (Wayland/niri, XWayland недоступен). Функциональные проверки GUI выполнены под Xvfb :99 — рендеринг реальный, но это не заменяет человеческую визуальную оценку цветов и читаемости.

## Среда проверки

| Компонент | Значение |
|---|---|
| OS | Linux, сессия Wayland (niri); X11-доступ к пользовательскому экрану отсутствует |
| Go | go 1.27.0, module watchlab, etcd client v3.7.3 |
| Python | 3.14.9 (проектное `.venv`); требуется >= 3.10 |
| etcd | /usr/bin/etcd 3.7.3, /usr/bin/etcdctl |
| GUI | Tkinter (X11), функциональные проверки под Xvfb :99 (1280x900x24) |

## PASS

| Проверка | Результат | Артефакт |
|---|---|---|
| `gofmt -l .` | PASS — замечаний нет | `reports/verification/release-20261125/gofmt.out` |
| `go vet ./...` | PASS — замечаний нет | `reports/verification/release-20261125/go_vet.out` |
| `go test -count=1 ./...` | PASS — `ok watchlab 0.014s` (2 unit-теста) | `reports/verification/release-20261125/go_test.out` |
| `go build -o bin/watchlab .` | PASS — бинарник собран | `reports/verification/release-20261125/go_build.out` |
| `python -m py_compile` (все скрипты) | PASS | — |
| `python -m unittest discover -s scripts -p "test_*.py"` | PASS — 81 тест, 0.436s | — |
| `demo.py` end-to-end (изолированный etcd) | PASS — `overall: PASS`, exit 0; reconnect 10/10, compaction-recovery tail 5/5; 15 сверенных событий | demo-каталог `/tmp/watchlab-demo-20261126-001244-668133` (в архив не входит) |
| `bin/watchlab` не пересобирается заново | PASS — контрольная сумма бинарника не изменилась после демо | sha256 `be12a38d…` |
| GUI: запуск | PASS — окно открывается, 1180x760 | скриншоты Xvfb (`/tmp/kilo/shots/`, в архив не входят) |
| GUI: все 7 страниц | PASS — Dashboard, Recovery, State Inspector, Events, Experiments, Analytics, Logs | скриншоты Xvfb |
| GUI: таблицы заполнены реальными данными | PASS — Experiments 52 строки (9 серий x сценарии + исключённые), Events 40 строк (реальные SHA-256/revision, до 72 символов в ячейке), Analytics 57 строк статистики | — |
| GUI: изменение размера окна | PASS — 980x620 и 1280x860 применяются корректно | — |
| GUI: кнопка Run demo | PASS — demo.py запускается, этапы видны в логе | — |
| GUI: кнопка Stop | PASS — SIGINT → demo.py останавливает только свои дочерние процессы; exit 130; остаточных etcd-процессов нет | — |
| GUI: Inspect recovered state | PASS — State Inspector открывается, 15 ключей реального состояния после demo | скриншот `30-state-inspector` |
| GUI: Recalculate | PASS — analyze_results.py exit 0; артефакты reports/analysis побайтно идентичны исходным | — |
| GUI: открытие графиков | PASS — xdg-open вызывается с корректным путём PNG | — |
| GUI: открытие каталога логов | PASS — xdg-open вызывается с `logs/gui` | — |
| GUI: Stop selected | PASS — собственный управляемый процесс остановлен (exit -2 = SIGINT); чужие процессы не затронуты | — |
| GUI: закрытие окна во время выполнения процесса | PASS — собственный дочерний процесс завершён корректно | — |
| GUI: Events pagination | PASS — «next» работает (стр. 2 при size=10); на последней странице кнопка отключена | — |
| Целостность данных | PASS — sha256 всех файлов `results/` и `reports/` не изменились за всю сессию проверок (0 расхождений с базой) | `/tmp/kilo/final-checksums.txt` vs `/tmp/kilo/baseline-checksums.txt` |
| Исходные JSONL и summary.json не изменены | PASS | — |

## FAIL

Зарегистрированных провалов нет.

## NOT RUN

| Проверка | Причина |
|---|---|
| Визуальная проверка всех страниц на реальном Linux desktop (цвета, читаемость, длинные значения глазами) | Нет доступа к X11-дисплею пользовательской сессии (Wayland/niri; XWayland недоступен, X0 принадлежит sddm-greeter с X-авторизацией). Функциональные проверки выполнены под Xvfb :99 — рендер реальный, но человеческая визуальная оценка цветов/читаемости не проводилась. Не выдавать Xvfb за полноценную ручную проверку. |

## Целостность данных

- База контрольных сумм снята до начала любых проверок; финальная сверка: 0 расхождений.
- `bin/watchlab` пересобран один раз (`go build`), контрольная сумма стабильна после повторной сборки демо-проверкой (`demo.py` собирает бинарник только при устаревании).
- Recalculate (GUI) перегенерировал артефакты `reports/analysis` побайтно идентично — детерминированная аналитика подтверждена.

## Исторические артефакты

- Предыдущие проверки от 2026-11-21: `reports/verification/` (gofmt, vet, unittest, demo_run и др.) — сохранены без изменений.
- Аналитика 9 серий: `reports/analysis/` (manifest, event_validation.json, CSV, 3 PNG) — сохранена без изменений.
- Неполный каталог `auto-20261119-230935` сохранён и не включён в статистику (как и предписано AGENTS.md).

## Верификация релизного архива

Архив собран из чистого staging-каталога (rsync с исключениями). Проверки выполнялись на распакованной копии без `.venv` (системный python3 3.14.9 + tkinter 8.6):

| Проверка из распакованного архива | Результат |
|---|---|
| Состав: мусорные файлы (`.venv`, `.kilo`, `logs/`, `__pycache__`, `*.pyc`, `*.swp`, `*~`) | PASS — 0 записей |
| Состав: etcd data-dir (`data/`) | PASS — исключён, 0 записей |
| Состав: `bin/watchlab`, `main.go`, `recover.go`, `go.mod/go.sum`, `scripts/`, `results/` (367 файлов), `reports/` (32 файла), `docs/screenshots/` (4 PNG), 3 аналитических PNG, RELEASE_REPORT/RELEASE_NOTES | PASS — всё присутствует |
| `go build -o bin/watchlab-verify .` | PASS |
| `go test -count=1 ./...` | PASS |
| `python3 -m py_compile` (все скрипты) | PASS |
| `python3 -m unittest discover -s scripts -p "test_*.py"` | PASS — 81 тест |
| Запуск GUI (`python3 scripts/gui.py`, класс `WatchLabApp`) | PASS — все 6 встроенных страниц отображаются: dashboard, recovery, events (40 строк), experiments (52), analytics (57), logs |
| State Inspector из архива | PASS — Toplevel открылся, 15 строк состояния (R=21, L=26) из состояния demo, запущенного из архива |
| `python3 scripts/demo.py` из архива | PASS — `overall: PASS`, 15/15 сверенных событий (reconnect 10/10, compaction-tail 5/5), 15 ключей состояния; изолированный etcd на свободных портах (2379/2380 избегаются), `preexisting_etcd_touched: false`, остановлен собственным SIGTERM; demo-каталог в `/tmp` (в архив не входит) |

## Релизный архив

`WatchLab-Release.zip` — размер ≈ 13.8 МБ (точное значение: `ls -l WatchLab-Release.zip` рядом с этим документом); состав и инструкция запуска: `docs/RELEASE_NOTES.md`. Точная команда запуска GUI из корня распакованного архива:

```bash
python3 scripts/gui.py
```
