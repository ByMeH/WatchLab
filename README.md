# WatchLab

WatchLab — исследовательский стенд для изучения восстановления локального
состояния клиента etcd Watch API после перерыва наблюдения и MVCC compaction.

## 1. Назначение

WatchLab воспроизводит полный цикл работы клиента etcd Watch: генерацию
событий, наблюдение, прерывание наблюдения, восстановление локального
состояния и последующую валидацию. Стенд состоит из Go-приложения с тремя
командами (`produce`, `watch`, `recover`) и Python-обвязки для демонстрации,
автоматизированных экспериментов, по-событийной валидации и анализа.

Первичные данные (JSONL/JSON) записываются в `results/`, производные
агрегаты и графики — в `reports/analysis/`. Сырые данные никогда не
перезаписываются: выходные файлы создаются с защитой от перезаписи
(`O_EXCL`), поэтому для повторного запуска нужен новый `--run-id` или явный
новый `--out`; файл состояния `--state`, напротив, переиспользуется для
продолжения сохранённого checkpoint.

## 2. Исследуемая проблема

После перерыва наблюдения клиент etcd Watch должен восстановить локальное
состояние и продолжить приём событий без пропусков. Основная гипотеза:

- если требуемая история событий доступна в MVCC-истории, клиент продолжает
  Watch с последней полностью обработанной revision `L + 1`;
- если история уже compacted, старый Watch промежуточные события не
  вернёт (`required revision has been compacted`); клиент выполняет Range
  snapshot на актуальной revision `R` — восстановление **текущей** карты
  key-value, а не исторических переходов — и продолжает Watch с `R + 1`.

Важно: snapshot восстанавливает текущее состояние ключей. Он не
восстанавливает историю событий, и это различие в выводах не подменяется.

## 3. Архитектура приложения

Путь данных:

```
Go Producer → etcd → Go Watch / Recover → JSONL → Python Validation → Python Analysis → CSV и графики
```

1. **Go Producer** пишет тестовые события (PUT) в etcd и в JSONL-журнал
   подтверждения (`put_ack`): request_id, revision, задержка, SHA-256 значения.
2. **etcd** хранит ключи и MVCC-историю ревизий; в экспериментах — отдельный
   локальный single-member экземпляр.
3. **Go Watch** подписывается на префикс и записывает полученные события;
   **Go Recover** ведёт постоянное локальное состояние (Range snapshot +
   Watch с checkpoint `L + 1`) и при compaction автоматически делает новый
   snapshot и продолжает Watch с `R + 1`.
4. **Python Validation** сопоставляет acks Producer'а с событиями Watcher'а
   по-событийно (по `request_id`: key, revision, sequence, run_id, SHA-256).
5. **Python Analysis** агрегирует завершённые серии в CSV/JSON и строит
   PNG-графики.

## 4. Технологический стек

| Слой | Технология | Роль |
|---|---|---|
| Ядро | Go 1.27.x, `go.etcd.io/etcd/client/v3` v3.7.3 | Producer, Watch, Recover, unit-тесты |
| Хранилище | etcd 3.7.x (проверено на 3.7.3) + etcdctl | MVCC-хранилище, compaction |
| Автоматизация и анализ | Python 3.10+ (только стандартная библиотека; matplotlib — опционально для графиков) | demo, оркестрация экспериментов, валидация, статистика, графики |
| Скрипты запуска | bash | запуск инструментов и локального etcd |

## 5. Go-команды

Общая справка: `./bin/watchlab help`. Флаги конкретной команды:
`./bin/watchlab <команда> -h` (usage печатается; код возврата при `-h` ненулевой).

### 5.1 `produce` — генерация событий

```
./bin/watchlab produce [флаги]
```

Пишет последовательность тестовых событий (PUT) в etcd и после каждого
подтверждённого запроса добавляет в JSONL запись `put_ack` (`run_id`,
`sequence`, `request_id`, `key`, `revision`, `latency_ms`, `value_sha256`).

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--endpoint` | `http://127.0.0.1:2379` | etcd client endpoint |
| `--run-id` | `pilot-001` | ID запуска: ASCII-буквы, цифры, `_`, `-` |
| `--prefix` | `/watchlab/runs/<run-id>/events/` | префикс ключей; обязан заканчиваться `/` |
| `--count` | `10` | число событий, 1…100000 |
| `--interval` | `500ms` | пауза между записями (Go duration) |
| `--out` | `results/<run-id>-producer.jsonl` | JSONL-файл результата |
| `--request-timeout` | `5s` | таймаут каждого PUT |

Останов по Ctrl+C (SIGINT/SIGTERM) — корректный, частично записанные события
фиксируются как `PRODUCER_STOPPED`.

### 5.2 `watch` — наблюдение за префиксом

```
./bin/watchlab watch [флаги]
```

Подписывается на префикс (`WithPrefix`, `WithCreatedNotify`, опционально
`WithRev`) и записывает JSONL-записи: `watch_created`, `watch_event`
(`event_revision`, `event_type`, `key`, `value`), `watch_canceled`
(`compact_revision`, `error`).

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--endpoint` | `http://127.0.0.1:2379` | etcd client endpoint |
| `--run-id` | `pilot-001` | ID запуска |
| `--prefix` | `/watchlab/runs/<run-id>/events/` | префикс; обязан заканчиваться `/` |
| `--start-revision` | `0` | включительная стартовая revision; `0` — события после создания Watch |
| `--out` | `results/<run-id>-watcher.jsonl` | JSONL-файл результата |

### 5.3 `recover` — восстановление состояния с checkpoint

```
./bin/watchlab recover [флаги]
```

Ведёт локальное состояние префикса и переживает compaction:

1. Если файла состояния нет — один Range-запрос снимает snapshot префикса на
   revision `R`; checkpoint `last_revision = R`; состояние сохраняется
   атомарно (временный файл + rename + fsync каталога).
2. Watch стартует с `last_revision + 1` (`WithProgressNotify` включён).
3. События применяются к локальной карте (PUT — запись/замена, DELETE —
   удаление), полный ответ Watch журналируется в JSONL и `Sync()`-ится, затем
   атомарно сохраняется состояние с новым checkpoint. Порядок
   «журнал → checkpoint» исключает молчаливую потерю события; при сбое между
   шагами возможен повтор записей в журнале (at-least-once), применение
   PUT/DELETE идемпотентно.
4. Пустой ответ с прогресс-нотификацией продвигает checkpoint до
   `Header.Revision` (запись `watch_progress`).
5. Если Watch отменён из-за compaction (`watch_canceled` с
   `compact_revision`) — снимается новый snapshot на актуальной revision,
   состояние заменяется, Watch продолжается с `R + 1`.

Записи журнала: `snapshot`, `watch_created` (`start_revision`), `watch_event`,
`watch_canceled`, `watch_progress`.

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--endpoint` | `http://127.0.0.1:2379` | etcd client endpoint |
| `--run-id` | `recover-001` | ID запуска |
| `--prefix` | `/watchlab/runs/<run-id>/events/` | префикс; обязан заканчиваться `/` |
| `--out` | `results/<run-id>-recover.jsonl` | JSONL-журнал восстановления |
| `--state` | `results/<run-id>-state.json` | файл состояния (схема `schema_version`, `prefix`, `snapshot_revision`, `last_revision`, `keys`) |
| `--request-timeout` | `15s` | таймаут каждого Range-запроса |

Файлы `--out` и `--state` обязаны различаться. При повторном запуске с тем же
`--state` состояние продолжается (проверяются schema version, префикс и
корректность checkpoint); `--out` при этом должен быть новым файлом.

## 6. Python-компоненты

Все скрипты используют только стандартную библиотеку Python (кроме
опционального matplotlib в анализаторе) и запускаются из корня проекта.

| Скрипт | Назначение | Нужен etcd? |
|---|---|---|
| `scripts/demo.py` | Самодостаточная демонстрация на изолированном временном etcd | создаёт свой |
| `scripts/run_experiments.py` | Автоматизированные экспериментальные серии на запущенном тестовом etcd | да, внешний |
| `scripts/validation.py` | Канонический модуль по-событийной валидации + CLI офлайн-ревалидации серии | нет (CLI) |
| `scripts/validate_history.py` | Ручная сверка producer/watcher JSONL | нет |
| `scripts/analyze_results.py` | Агрегация summary.json в CSV/JSON и графики | нет |

Модульные тесты: `scripts/test_validation.py` (22 теста валидатора) и
`scripts/test_demo.py` (вспомогательная логика demo) — запускаются
`python -m unittest discover -s scripts -p "test_*.py" -v` без etcd.

### 6.1 `demo.py`

Одна команда выполняет полный цикл:

1. проверяет окружение (Python/Go/etcd/etcdctl) и при необходимости
   пересобирает `bin/watchlab`, если он отсутствует или старше исходников;
2. запускает **собственный изолированный etcd**: уникальный data-dir,
   свободные localhost-порты (зарезервированные 2379/2380 не используются и
   не трогаются), уникальный cluster token; compaction выполняется только
   против этого экземпляра;
3. сценарий **reconnect**: Watch → фаза 1 Producer'а → остановка только своего
   Watch-процесса → фаза 2 при выключенном Watch → возобновление с `L+1` →
   по-событийная валидация (request_id, revision, sequence, key, SHA-256);
4. сценарий **compaction recovery**: фаза 1 → Recover (начальный snapshot) →
   остановка Recover → фаза 2 → compact этого экземпляра etcd на последней
   revision фазы 2 → перезапуск Recover с тем же state-файлом → обнаружение
   compaction → свежий snapshot → Watch с `R+1` → фаза 3 → по-событийная
   валидация хвоста;
5. проверка финального состояния: checkpoint, число ключей, SHA-256 и
   `mod_revision` каждого ключа против acks Producer'а.

Артефакты (JSONL, консольные логи, лог etcd, `demo-summary.json`) сохраняются
в `/tmp/watchlab-demo-<demo-id>/` и автоматически не удаляются; `results/`,
`reports/` и исторические данные не изменяются.

Флаги: `--events` (событий на фазу, 1…100, по умолчанию 5), `--interval`
(пауза Producer'а, по умолчанию `100ms`), `--timeout` (секунды на этап, по
умолчанию 30), `--client-port`/`--peer-port` (тестовые хуки для фиксации
портов; по умолчанию порты выбираются автоматически). Коды возврата: `0` —
все сценарии PASS, `1` — ошибка, `130` — прерывание.

### 6.2 `run_experiments.py`

Проводит автоматизированную серию из трёх сценариев на **уже запущенном**
тестовом etcd: (A) reconnect с возобновлением с `L+1`; (B) compaction
recovery с автоматическим resnapshot; (C) PUT → PUT → DELETE одного ключа
(ключи пишет etcdctl, поэтому сверяются только типы и порядок событий).

Результаты пишутся в `results/automated/auto-<ГГГГммдд-ЧЧММСС>/`: JSONL всех
фаз, консольные логи, `compaction-state.json`, `mutations-state.json`,
`summary.csv` и `summary.json`.

Флаги: `--endpoint` (по умолчанию `http://127.0.0.1:2379`), `--binary`
(по умолчанию `bin/watchlab`), `--count` (событий на фазу, 2…10000, по
умолчанию 10), `--interval` (по умолчанию `100ms`), `--timeout` (секунды на
этап, по умолчанию 30). Скрипт требует запуска из корня проекта,
`etcdctl` в PATH и собранного бинарника.

> **Предупреждение.** `etcdctl compact` действует на всю MVCC-историю
> (backend) целевого экземпляра etcd, а не только на префикс эксперимента.
> Запускать только против отдельного локального тестового etcd. Запрещено
> использовать общий или рабочий сервер: compaction необратимо удалит
> исторические ревизии для всех данных этого экземпляра.

### 6.3 `validation.py`

Канонический модуль правил сопоставления (используется `demo.py`,
`run_experiments.py` и офлайн-ревалидацией). Сопоставление — по `request_id`
из payload значения, никогда по количеству событий; каждое совпавшее событие
сверяется по полям `key`, `revision`, `sequence`, `run_id`, `value_sha256`.
Счётчики: missing / duplicate / unexpected / mismatch / incomparable. PASS
требует нулевых счётчиков и монотонного порядка ревизий; равенство количеств
событий само по себе PASS не даёт. Реализованы проверки границ: reconnect —
первое событие возобновлённого Watch равно `L+1`; compaction-tail — одна
запись `watch_canceled`, один replacement-snapshot, новый Watch стартует с
`snapshot_revision + 1`; same-key — только последовательность и порядок типов
событий (producer-acks для etcdctl-записей отсутствуют, и это не выдаётся за
полную проверку).

CLI офлайн-ревалидации сохранённой серии (etcd не нужен):

```
python scripts/validation.py --suite results/automated/<suite_id>
```

Коды возврата: `0` — PASS, `1` — FAIL, `2` — ошибка (нет каталога, битые файлы).

### 6.4 `validate_history.py`

Ручной CLI-сверки producer/watcher JSONL (сохранён для интерактивного
использования; канонический сопоставитель для автоматизации — `validation.py`):

```
python scripts/validate_history.py \
  --producer results/<run-id>-producer.jsonl \
  --watcher results/<run-id>-watcher.jsonl
```

`--producer` и `--watcher` обязательны и могут повторяться (несколько файлов).
Выводит число acks, событий, совпадений, пропусков, порядок ревизий и
описательную статистику задержек PUT. Коды возврата: `0` — PASS, `1` — FAIL,
`2` — ошибка.

### 6.5 `analyze_results.py`

Агрегирует только завершённые серии (`auto-*/summary.json`) в `reports/analysis/`:
`master_runs.csv` (по строке на серию×сценарий), `group_statistics.csv`
(`n`, `mean`, `median`, `stdev_sample`, `min`, `max` по метрикам),
`excluded_runs.csv` (не включённые каталоги с причинами),
`analysis_manifest.json` (включённые `suite_id`, источники, примечания) и
PNG-графики (`snapshot_range.png`, `recovery_interval.png`,
`snapshot_payload_size.png`) — при наличии matplotlib, иначе CSV/JSON
создаются без графиков. Серии с вычисленным ненулевым `missing_events`
исключаются явно. Исторические имена метрик (`recovery_ms`,
`phase1_events_represented_by_snapshot`) читаются через алиасы; новые
отчёты используют `client_recovery_interval_ms` и `phase1_keys_in_snapshot`.

Флаги: `--root` (по умолчанию `results/automated`), `--out` (по умолчанию
`reports/analysis`), `--sizes` (по умолчанию `10,50,100`), `--exclude`
(по умолчанию `auto-20261119-230935`), `--suite-ids` (по умолчанию девять
зафиксированных серий), `--all-matching` (включить все завершённые серии
подходящих размеров вместо фиксированной девятки).

### 6.6 `gui.py` — WatchLab Desktop

Графический frontend (Tkinter, только стандартная библиотека Python):
дашборд с реальными показателями окружения и данных, управление
демонстрацией (`scripts/demo.py` запускается как subprocess, этапы
восстановления отображаются по фактическим маркерам вывода), постраничный
просмотр сохранённых JSONL-событий с фильтрами, таблица экспериментальных
серий, просмотр аналитики из `reports/analysis/` с запуском
`scripts/analyze_results.py` по кнопке и журнал GUI-процессов
(`logs/gui/`). GUI — только frontend и оркестратор: Go-бинарник остаётся
backend для etcd, orchestrator демонстрации и аналитика не
дублируются. Новые эксперименты из GUI не запускаются (compaction
затрагивает весь MVCC backend целевого etcd).

Запуск из корня проекта (требуется Tkinter из системного пакета,
например `python3-tk`):

```bash
python scripts/gui.py
```

Подробности: `docs/GUI_GUIDE.md`.

## 7. Требования к окружению

- Go 1.27.x (`go.mod` требует `go 1.27.0`).
- etcd 3.7.x и `etcdctl` в PATH (проверено с 3.7.3).
- Python 3.10+ (для demo/экспериментов; проверено на 3.14). Для графиков —
  matplotlib в используемом окружении Python, иначе анализ обойдётся без PNG.
- Локальная машина с Linux; все сценарии привязаны к `127.0.0.1`.

## 8. Установка и подготовка окружения

```bash
# 1. Клонировать репозиторий и перейти в каталог проекта.
# 2. Проверить инструменты:
go version            # 1.27.x
etcd --version        # 3.7.x
etcdctl version
python3 --version     # 3.10+

# 3. Опционально: виртуальное окружение Python с matplotlib для графиков.
#    Без него все скрипты работают, но PNG-графики не создаются.
python3 -m venv .venv
.venv/bin/pip install matplotlib
```

Go-зависимости скачиваются автоматически при первой сборке (`go build`
или `go mod tidy`); отдельной установки пакетов Python по умолчанию не требуется.

## 9. Сборка Go-бинарника

```bash
go mod tidy
go test -count=1 ./...
mkdir -p bin
go build -o bin/watchlab .
```

Результат: `bin/watchlab`. `scripts/demo.py` пересобирает бинарник
автоматически, если он отсутствует или старше `main.go`/`recover.go`/`go.mod`/`go.sum`.

## 10. Режим 1 — Demo: демонстрация одной командой

```bash
python scripts/demo.py
```

Режим самодостаточен: сам запускает изолированный временный etcd, сам
демонстрирует reconnect и compaction recovery, сам проверяет результат,
останавливает собственный экземпляр etcd и сохраняет артефакты в `/tmp`.
Общий или рабочий сервер не используется и не затрагивается; компакция
выполняется только внутри собственного экземпляра demo.

Что ожидать в конце: `=== WATCHLAB DEMO SUMMARY ===` с `overall: PASS`,
списком артефактов и путём к `demo-summary.json`. Успешный код возврата — `0`.

## 11. Запуск отдельных Go-команд

Требуется запущенный etcd на `--endpoint` (по умолчанию
`http://127.0.0.1:2379` — свой тестовый экземпляр).

Файлы результатов не перезаписываются, поэтому используй свежий `--run-id`
(в репозитории уже есть данные `pilot-001` и серий автоматизации).

Терминал 1 — наблюдатель:

```bash
./bin/watchlab watch --run-id demo-001
```

Дождись строки `WATCH_CREATED`. Терминал 2 — генератор:

```bash
./bin/watchlab produce --run-id demo-001 --count 10 --interval 500ms
```

Останови watcher через Ctrl+C. Результаты:

- `results/demo-001-producer.jsonl` — подтверждённые PUT-запросы;
- `results/demo-001-watcher.jsonl` — полученные события.

Восстановление состояния (тот же запущенный etcd):

```bash
./bin/watchlab recover \
  --run-id recover-demo \
  --prefix /watchlab/runs/demo-001/events/ \
  --out results/recover-demo-recover.jsonl \
  --state results/recover-demo-state.json
```

Первый запуск снимает snapshot и пишет `RECOVER_SNAPSHOT`; останов по
Ctrl+C сохраняет checkpoint. Повторный запуск с тем же `--state` продолжит
состояние (`RECOVER_STATE_LOADED`) — но указывай новый `--out`, так как
существующие JSONL-файлы не перезаписываются. Если перед повторным запуском
выполнить compaction, Recover сам снимет новый snapshot и продолжит Watch.

## 12. Режим 2 — Research experiments: автоматизированные эксперименты

```bash
# 1. Запусти отдельный локальный тестовый etcd (свой data-dir, не рабочий сервер).
#    Пример для локального бинарника etcd:
etcd --name watchlab-test \
  --data-dir /tmp/watchlab-etcd-data \
  --listen-client-urls http://127.0.0.1:2379 \
  --advertise-client-urls http://127.0.0.1:2379 \
  --listen-peer-urls http://127.0.0.1:2380 \
  --initial-advertise-peer-urls http://127.0.0.1:2380 \
  --initial-cluster watchlab-test=http://127.0.0.1:2380 \
  --initial-cluster-token watchlab-test \
  --initial-cluster-state new

# 2. Из корня проекта:
python scripts/run_experiments.py            # --count 10 --interval 100ms по умолчанию
python scripts/run_experiments.py --count 50 # другой размер фазы
```

Скрипт проверяет здоровье endpoint'а через `etcdctl`, выполняет три сценария
(reconnect, compaction recovery, same-key PUT/PUT/DELETE) и сохраняет серию
в `results/automated/auto-<штамп>/`. Успешное завершение печатает
`SUCCESS: <каталог серии>`.

> **Ещё раз о безопасности.** Сценарий compaction recovery выполняет
> `etcdctl compact` на **весь MVCC backend целевого экземпляра etcd**,
> а не только на префикс эксперимента. Это необратимая кластерная операция
> данного экземпляра. Используй только отдельный локальный тестовый etcd
> с отдельным data-dir; запрещено использовать общий или рабочий сервер.

## 13. Независимая проверка результатов

Проверить одну сохранённую серию по-событийно (etcd не нужен):

```bash
python scripts/validation.py --suite results/automated/auto-20261119-233338
```

Ручная сверка произвольных файлов producer/watcher:

```bash
python scripts/validate_history.py \
  --producer results/pilot-001-producer.jsonl \
  --watcher results/pilot-001-watcher.jsonl
```

Модульные тесты:

```bash
go test -count=1 ./...                                            # Go
python -m unittest discover -s scripts -p "test_*.py" -v          # Python
```

Проверка сборки и статики: `go vet ./...` и `gofmt -l .` (пустой вывод).
Новые экспериментальные серии можно валидировать той же командой
`scripts/validation.py --suite <каталог>` сразу после прогона.

## 14. Режим 3 — Analysis: анализ результатов и графики

```bash
python scripts/analyze_results.py
```

etcd для этого режима не требуется: читаются только сохранённые
`results/automated/auto-*/summary.json`. По умолчанию агрегируются девять
зафиксированных серий (3 повтора × размеры 10/50/100; неполная серия
`auto-20261119-230935` исключена).

Выходные файлы в `reports/analysis/`: `master_runs.csv`,
`group_statistics.csv`, `excluded_runs.csv`, `analysis_manifest.json` и три
PNG-графика (при наличии matplotlib). Подробное описание файлов и определений
метрик — `reports/analysis/README.md`. Выборку можно переопределить:
`--sizes`, `--suite-ids`, `--all-matching`, `--exclude`, `--root`, `--out`.

## 15. Структура файлов и расположение результатов

```
watchlab-pilot/
├── main.go, recover.go, recover_test.go   # Go: Producer, Watch, Recover и тесты
├── go.mod, go.sum                          # модуль watchlab, etcd client v3 v3.7.3
├── bin/watchlab                            # собранный бинарник (после go build)
├── scripts/
│   ├── demo.py                             # Режим 1: demo на изолированном etcd
│   ├── run_experiments.py                  # Режим 2: автоматизированные серии
│   ├── validation.py                       # канонический валидатор + CLI --suite
│   ├── validate_history.py                 # ручная сверка producer/watcher
│   ├── analyze_results.py                  # Режим 3: CSV/JSON/графики
│   ├── test_validation.py, test_demo.py    # модульные тесты Python
│   ├── test_gui_services.py                # модульные тесты сервисного слоя GUI
│   ├── gui.py, gui/                        # WatchLab Desktop (frontend Tkinter)
│   └── __pycache__/                        # служебное
├── results/                                # ПЕРВИЧНЫЕ данные; не редактировать
│   ├── <run-id>-producer.jsonl             # ручные запуски Go-команд
│   ├── <run-id>-watcher.jsonl
│   ├── <run-id>-recover.jsonl / -state.json
│   └── automated/auto-<suite_id>/          # серии run_experiments.py
│       ├── reconnect-*.jsonl, compaction-*.jsonl, mutations-*.jsonl
│       ├── *-console.log                   # консольные логи процессов
│       ├── compaction-state.json, mutations-state.json
│       └── summary.csv, summary.json
├── reports/analysis/                       # ПРОИЗВОДНЫЕ артефакты анализа
│   ├── master_runs.csv, group_statistics.csv, excluded_runs.csv
│   ├── analysis_manifest.json, event_validation.json
│   ├── snapshot_range.png, recovery_interval.png, snapshot_payload_size.png
│   └── README.md                           # определения метрик и ограничения
└── docs/                                   # статус проекта и рецензии
```

- Данные demo лежат вне репозитория: `/tmp/watchlab-demo-<demo-id>/`
  (сохраняются, автоматически не удаляются).
- data-dir тестового etcd создаётся пользователем вне `results/`
  (например, `/tmp/watchlab-etcd-data`); каталоги `member/`, `wal/`
  etcd вручную не очищаются.
- В `results/automated/` также хранятся неполные каталоги (например,
  `auto-20261119-230935`) — они сохраняются, но в статистику не включаются
  (см. `excluded_runs.csv` и `analysis_manifest.json`).

## 16. Ограничения приложения

- **Локальный single-member etcd.** Все сценарии рассчитаны на один узел
  на `127.0.0.1`; кластерных конфигураций нет.
- **Сетевые partition не тестируются.** Прерывание наблюдения моделируется
  остановкой клиентского процесса (SIGINT), а не разрывом сети; поведение
  gRPC-переподключений внутри клиентской библиотеки отдельно не
  изолируется.
- **Multi-node отказоустойчивость не проверяется.** Выводы не переносятся на
  кластеры из нескольких членов, выборы лидера и кворумные сценарии.
- **Ограниченное покрытие Go unit-тестами.** По состоянию на 21.11.2026 —
  два unit-теста, statement coverage около 4.6%; основная проверка
  корректности — по-событийная валидация JSONL в Python.
- **Ограниченная экспериментальная выборка.** Опубликованная статистика —
  девять серий (n=3 на размер 10/50/100), одна машина, один etcd, описательные
  статистики; это не универсальный benchmark, значимость и причинность из
  n=3 не выводятся.
- **Snapshot — текущее состояние.** Range snapshot восстанавливает карту
  key-value на revision `R`, а не исторические переходы; промежуточные
  события до `R` после compaction невосстановимы по определению.
- **Журнал at-least-once при сбоях процесса.** Crash между записью события
  в журнал и сохранением checkpoint может дать дубликаты записей при
  повторе; молчаливые пропуски исключены порядком «журнал → checkpoint».
  Exactly-once для внешних эффектов не заявляется.
- **Один Range без пагинации.** Snapshot подходит для текущих малых объёмов
  экспериментов; большие префиксы потребуют пагинации с фиксацией revision.
- **Значения — UTF-8 строки** (JSON-тестовые payload); произвольные бинарные
  ключи/значения не являются целью.
- **same-key без producer-acks.** Ключи пишет etcdctl, поэтому сверяются
  только типы и порядок событий; сравнение значений для этого сценария
  недоступно и не заявляется.
