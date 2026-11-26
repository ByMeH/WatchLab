# WatchLab — Release Notes

ата актуализации: 2026-11-26

Статус: исследовательский прототип. сновная проверенная платформа — Linux x86-64.
Windows 10/11 и macOS пока не поддерживаются.

## то это

WatchLab — исследовательский стенд для изучения восстановления локального
состояния клиента etcd Watch API после прерывания наблюдения и MVCC compaction.

роект состоит из трёх частей:

- **Go backend** — Producer, Watch и Recover (`main.go`, `recover.go`).
- **Python Desktop GUI** — графический интерфейс на Tkinter (`scripts/gui.py`,
  `scripts/gui/`).
- **Python tools** — демонстрация, автоматизация экспериментов, валидация
  событий и анализ (`demo.py`, `run_experiments.py`, `validation.py`,
  `analyze_results.py`).

GUI управляет существующими компонентами, но не заменяет Go backend.

## Требования

- Linux x86-64.
- Go 1.27.0 или новее — для сборки backend. Go также требуется для автоматической
  сборки бинарника при запуске demo, если он отсутствует или устарел.
- Python 3.10 или новее с Tkinter.
  Debian/Ubuntu: `sudo apt install python3-tk`.
  Fedora: `sudo dnf install python3-tkinter`.
- etcd 3.7+ и `etcdctl` в PATH — для Demo и проверки доступности стандартного
  endpoint.
- `xdg-open` — необязательно, используется для открытия графиков и каталога логов.
- `matplotlib` — необязательно, необходимо для генерации PNG-графиков.
  CSV и JSON аналитика может быть создана без matplotlib.

ольшинство Python-инструментов используют стандартную библиотеку.
иртуальное окружение `.venv` необязательно.

## становка из исходников

```bash
git clone https://github.com/ByMeH/WatchLab.git
cd WatchLab

go mod download
mkdir -p bin
go build -o bin/watchlab .
```

## апуск

Desktop GUI:

```bash
python3 scripts/gui.py
```

золированная end-to-end демонстрация:

```bash
python3 scripts/demo.py
```

нализ существующих экспериментальных данных:

```bash
python3 scripts/analyze_results.py
```

Справка Go CLI:

```bash
./bin/watchlab -h
```

ля Tkinter необходима графическая сессия с X11 или XWayland.

## Структура репозитория

| уть | азначение |
|---|---|
| `main.go`, `recover.go` | Go backend: Producer, Watch, Recover |
| `recover_test.go` | Go unit-тесты |
| `go.mod`, `go.sum` | Go-модуль и зависимости |
| `scripts/demo.py` | золированная демонстрация восстановления |
| `scripts/run_experiments.py` | втоматизированные экспериментальные серии |
| `scripts/validation.py` | о-событийная валидация |
| `scripts/validate_history.py` | роверка исторических журналов |
| `scripts/analyze_results.py` | Статистический анализ и построение графиков |
| `scripts/gui.py`, `scripts/gui/` | Desktop GUI |
| `scripts/test_*.py` | Python unit-тесты |
| `docs/GUI_GUIDE.md` | нструкция по Desktop GUI |
| `docs/RELEASE_NOTES.md` | нформация о проекте и запуске |
| `docs/RELEASE_REPORT.md` | тчёт P5 |
| `docs/WORK_PLAN.md` | лан работ |
| `docs/archive/` | сторические документы |
| `results/` | ервичные экспериментальные данные |
| `reports/analysis/` | CSV, JSON, статистика и графики |
| `reports/verification/` | Сохранённые результаты проверок |

аталог `bin/` и готовые бинарники не входят в исходный Git-репозиторий.
ни собираются из исходников.

ичные материалы «врики» и конфигурация Kilo не являются частью публичной
структуры исходников и исключены из Git.

## роверки

Go:

```bash
gofmt -l .
go vet ./...
go test -count=1 ./...
go build -o bin/watchlab .
```

Python:

```bash
python3 -m compileall -q scripts
python3 -m unittest discover -s scripts -p "test_*.py" -v
```

олный набор тестов необходимо запускать из корня проекта.

## езопасность экспериментов

`demo.py` создаёт собственный временный экземпляр etcd на свободном localhost-порту
и использует отдельный временный каталог данных.

`run_experiments.py` предназначен для отдельного тестового экземпляра etcd.
перация compaction действует на весь MVCC backend указанного сервера.

е запускайте экспериментальную compaction против production etcd.

## граничения

- сновная проверенная платформа — Linux x86-64.
- Windows 10/11 и macOS не проверялись.
- сследования проводились на одном локальном single-member etcd.
- становка процесса клиента не эквивалентна сетевому partition.
- Multi-node отказоустойчивость, выборы лидера и кворумные сценарии
  не исследовались.
- сновная аналитическая выборка состоит из девяти серий с тремя повторами
  для каждого размера. езультаты являются описательной статистикой,
  а не универсальным benchmark.
- учная визуальная проверка всех страниц GUI не завершена.

Старый `WatchLab-Release.zip` относится к предыдущей версии состава проекта.
еред распространением нового архива его необходимо пересобрать из актуального
дерева исходников.