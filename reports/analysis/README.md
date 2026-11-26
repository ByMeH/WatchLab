# reports/analysis — описание файлов

Машиночитаемые агрегаты девяти завершённых автоматических серий WatchLab
(основная выборка `scripts/analyze_results.py::DEFAULT_SUITES`, девять
зафиксированных suite_id; не меняется). Сырые данные в `results/automated/`
не изменяются. Перегенерация: `python scripts/analyze_results.py`
(defaults: `--sizes 10,50,100`, `--exclude auto-20261119-230935`).

## Экспериментальные фазы (не смешивать)

1. **Producer phase 1** — PUT-записи до отключения клиента и compaction.
2. **Range snapshot** — актуальные ключи и значения на ревизии `R`
   (текущая карта KV; это НЕ исторические события).
3. **Producer phase 2** — PUT-записи после отключения (в compaction-сценарии —
   после resnapshot).
4. **Compaction recovery Watch** — события, полученные новым Watch после
   снапшота (только фаза 2; валидируются по-событийно).

Числа: **480** — количество по-событийно проверенных событий phase 2 по девяти
выбранным сериям (3×10 + 3×50 + 3×100). Это НЕ общее число событий всех фаз и
НЕ количество строк JSONL. **516** — общее число записей журналов второй фазы
с учётом служебных записей (480 `watch_event` + 18 `watch_created` +
9 `watch_canceled` + 9 `snapshot`). Эти величины не взаимозаменяемы.

## Файлы

- `master_runs.csv` — построчный уровень: 27 строк = 9 серий × 3 сценария.
  Поля: `suite_id`, `events_per_phase`, `interval`, `scenario`, `endpoint`,
  метрики сценария (см. ниже), `validation_scope`. Пустое значение = метрика
  не применима к сценарию (для исторических summary также = поле отсутствовало).
- `group_statistics.csv` — групповой уровень по паре
  (events_per_phase, scenario, metric): `n`, `mean`, `median`, `stdev_sample`
  (выборочное SD, n−1), `min`, `max`. Метрики без числовых значений опускаются.
- `excluded_runs.csv` — все не включённые каталоги с причиной: 8 валидных серий
  вне зафиксированной девятки («not in selected suite ID set») и неполная серия
  `auto-20261119-230935` («incomplete run: summary.json is missing»; SIGINT на
  7-м из 50 событий фазы 2). Каталог сохранён, в статистику не входит.
- `analysis_manifest.json` — источник: включённые/запрошенные suite_id,
  причины исключений, число строк, примечание о среде и переименования метрик.
- `event_validation.json` — по-событийная офлайн-ревалидация девяти серий
  (`python scripts/validation.py --suite <dir>`): reconnect 960/960,
  compaction-tail 480/480, same-key 27/27; сверка request_id, sequence,
  revision, key и SHA-256 значения (SHA — только где есть producer acks);
  0 пропусков, дублей, неожиданных событий и несовпадений.
- `snapshot_range.png`, `recovery_interval.png`, `snapshot_payload_size.png` —
  средние по размерам 10/50/100 для сценария compaction_recovery с полосами
  выборочного SD и аннотацией (n, среда, исключённые серии).

## Определения метрик (не менять)

- `snapshot_range_ms` — длительность одного Range-запроса снапшота префикса.
- `client_recovery_interval_ms` — клиентский интервал от записи `watch_canceled`
  до следующей записи `watch_created` в журнале конкретной реализации Recover
  (включает Range, сохранение state и создание нового Watch). Это характеристика
  клиента, а НЕ время работы etcd. Исторические summary.json хранят ту же
  величину под именем `recovery_ms`; анализатор читает оба имени, новые отчёты
  используют новое название.
- `snapshot_to_watch_ms` — интервал от записи `snapshot` до `watch_created`.
  Три интервала выше — вложенные, их нельзя складывать или подменять.
- `snapshot_bytes` — сумма длин (UTF-8 байтов) ключей и значений снапшота;
  НЕ размер JSON state-файла на диске.
- `phase1_keys_in_snapshot` — число ТЕКУЩИХ ключей, захваченных Range-снапшотом
  (ключи фазы 1), а НЕ число восстановленных исторических событий. Исторические
  summary.json хранят его под именем `phase1_events_represented_by_snapshot`.
- `observed_events` / `missing_events` — число событий, фактически
  сопоставленных валидатором (1:1 по request_id), и разность с ожидаемым.
- `duplicate_events` / `unexpected_events` / `field_mismatches` — повторные
  request_id; события без producer ack или вне фазы; несовпадения полей
  (key, revision, sequence, run_id, value_sha256). Для исторических summary
  пусты; фактические значения для девяти серий — в `event_validation.json`.
- `validation_scope` — что именно проверено в сценарии: reconnect и
  compaction-tail — по-событийная сверка с SHA-256; same-key — только
  последовательность и порядок типов событий (ключи пишутся etcdctl, producer
  acks и SHA-256 недоступны; проверка не выдаётся за полную).

## Ограничения

Один локальный single-member etcd (127.0.0.1:2379), один компьютер, n=3 на
размер, остановка процесса клиента вместо сетевого partition, один Range без
пагинации. Описательные статистики, не универсальный benchmark; значимость и
причинность из n=3 не выводятся. Данные: 2026-11-19, interval 100ms.
