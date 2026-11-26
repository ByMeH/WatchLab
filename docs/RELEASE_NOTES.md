# WatchLab — Release Notes

Дата релиза: 2026-11-26
Статус: исследовательский стенд (покрытие проверок — см. `docs/RELEASE_REPORT.md`; визуальная проверка GUI на реальном десктопе — NOT RUN)

## Что это

WatchLab — исследовательский стенд для изучения восстановления локального состояния клиента etcd Watch API после перерыва наблюдения и MVCC compaction.

- **Go** — backend: Producer / Watcher / Recover (`main.go`, `recover.go`, готовый бинарник `bin/watchlab`).
- **Python + Tkinter** — WatchLab Desktop (GUI): `scripts/gui.py`, пакет `scripts/gui/`.
- **Python** — демо-оркестратор, валидация, аналитика: `scripts/demo.py`, `scripts/validation.py`, `scripts/analyze_results.py`, `scripts/run_experiments.py`.

## Требования

- Linux x86-64.
- Go >= 1.27 — только для пересборки бинарника (в архиве есть готовый `bin/watchlab`).
- Python >= 3.10 с Tkinter (Debian/Ubuntu: `sudo apt install python3-tk`; Fedora: `sudo dnf install python3-tkinter`).
- etcd 3.7+ и `etcdctl` в PATH — для `demo.py` (демо запускает собственный одноразовый экземпляр etcd на свободных портах; фиксированные 2379/2380 и чужие экземпляры не используются).
- `xdg-open` — опционально, для кнопок «открыть график» / «открыть каталог логов».
- `matplotlib` — опционально, только для перегенерации аналитических графиков (кнопка Recalculate в GUI; без него CSV/JSON создаются, графики пропускаются).
- Все Python-скрипты используют только стандартную библиотеку; `.venv` не требуется.

## Состав архива

| Путь | Содержимое |
|---|---|
| `main.go`, `recover.go`, `recover_test.go`, `go.mod`, `go.sum` | Исходный Go-код |
| `bin/watchlab` | Готовый бинарник (собран из этого кода) |
| `scripts/` | Исходный Python-код: GUI, demo, валидация, аналитика, тесты (`test_*.py`) |
| `README.md`, `AGENTS.md`, `README-RECOVER.txt`, `install-recover.sh` | Документация и установка |
| `docs/` | GUI_GUIDE.md, PROJECT-STATUS.md, WORK_PLAN.md, RELEASE_REPORT.md, RELEASE_NOTES.md, отчёты `docs/reviews/`, тезисы `docs/final/`, `docs/source/` |
| `docs/screenshots/` | Скриншоты GUI |
| `results/` | Результаты 9 основных серий (`results/automated/`) + исторические JSONL/JSON |
| `reports/analysis/` | Три аналитических графика (recovery_interval.png, snapshot_range.png, snapshot_payload_size.png), CSV, manifest, отчёт валидации |
| `reports/verification/` | Журналы проверок (исторические и release-20261125) |

Из архива исключены: `.venv`, `__pycache__`/`*.pyc`, `.kilo` (локальная конфигурация AI-агентов), `logs/` (runtime-логи GUI), `README-KILO-SETUP.md`, содержимое `/tmp`.

## Установка и сборка

```bash
unzip WatchLab-Release.zip
cd WatchLab

# опционально: пересборка бинарника из исходников
go build -o bin/watchlab .
```

## Запуск

```bash
# WatchLab Desktop (GUI)
python3 scripts/gui.py

# Демонстрация end-to-end (изолированный etcd, ~30–60 с)
python3 scripts/demo.py

# Справка по командам Recover
./bin/watchlab -h
```

Требования к запуску GUI: графическая сессия с X11 (XWayland подходит); Tkinter обязателен.

## Проверка после установки

```bash
go test -count=1 ./...                          # 2 unit-теста
python3 -m unittest discover -s scripts -p "test_*.py"   # 81 тест
python3 scripts/demo.py                         # overall: PASS
```

Все эти проверки, а также запуск GUI, State Inspector и сборка `go build` были выполнены на распакованной копии данного архива без `.venv` (системный python3 + tkinter) — см. `docs/RELEASE_REPORT.md`.

## Известные ограничения

- GUI требует Tkinter (X11-only); функционально проверен под Xvfb, визуальная проверка человеком не проводилась (см. RELEASE_REPORT.md).
- `demo.py` создаёт свои каталоги под `/tmp/watchlab-demo-*` и не затрагивает `results/`.
- `etcdctl compact` действует на весь MVCC backend экземпляра etcd — не запускать против рабочего etcd.
- Тезисы (`docs/final/*.docx/pdf`) собраны заранее; пересборка требует `python-docx` (не входит в архив).
