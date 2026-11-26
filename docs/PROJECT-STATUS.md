# WatchLab — статус проекта на 21 ноября 2026

## Идея

Исследование восстановления локального состояния клиента etcd Watch API после временного прекращения наблюдения и MVCC compaction. Сравниваются два пути:

- сохранённая история доступна: продолжение Watch с `L+1`;
- требуемая история уже compacted: Watch отменяется, актуальное состояние берётся через Range snapshot на `R`, затем Watch открывается с `R+1`.

Snapshot восстанавливает текущую карту ключей, не историю промежуточных изменений.

## Что уже реализовано

- `produce`: создаёт PUT и пишет JSONL acknowledgements с request ID, sequence, key, revision, latency и SHA-256.
- `watch`: подписывается на prefix и записывает события.
- `recover`: Range snapshot, локальная карта ключей, checkpoint, Watch, PUT/DELETE, progress notifications, атомарная замена state-файла, повторный snapshot после compaction.
- `validate_history.py`: сверка Producer/Watcher журналов.
- `run_experiments.py`: автоматизированные сценарии reconnect, compaction recovery и PUT/PUT/DELETE.

## Подтверждённые функциональные сценарии

- Pilot: 10/10.
- Reconnect: 10/10.
- Compaction: старый Watch отменён с compacted revision; Range вернул текущие ключи; Watch с `R+1` получил последующие события.
- Automatic Recover: старый checkpoint 40 → compaction 45 → snapshot 45 → Watch 46 → события 46–50; итог 25 ключей.
- PUT/PUT/DELETE: revisions 51–53; ключ удалён из локального состояния.
- Restart: checkpoint 53 загружен; события 54–56 обработаны; ключ снова отсутствует.
- `go test`, `go vet`, `go build` проходили; 2 unit tests; statement coverage 4.6%.

## Девять автоматизированных серий

По 3 повтора для 10, 50 и 100 событий на фазу. В reconnect и compaction-tail сценариях пропусков не зарегистрировано.

| Events/phase | Range mean ms | Recovery mean ms | Snapshot→Watch mean ms | Snapshot payload bytes mean | Final keys |
|---:|---:|---:|---:|---:|---:|
| 10 | 1.690 | 18.646 | 1.563 | 2619.333 | 20 |
| 50 | 2.326 | 19.268 | 2.252 | 13137.333 | 100 |
| 100 | 2.893 | 21.531 | 1.653 | 26284.333 | 200 |

Эти данные — средние трёх запусков на каждом размере на одном локальном экземпляре etcd. Это описательное сравнение данного стенда, не универсальные benchmark-выводы.

## Ограничения

- Один локальный single-member etcd; не проверялся multi-node quorum.
- Остановка клиентского процесса, а не реальная сетевая partition.
- Range snapshot выполняется одним запросом; пагинация не проверялась.
- 3 повтора на размер — малая выборка.
- Низкое unit-test coverage (4.6% в зафиксированном аудите).
- Exactly-once для внешних побочных эффектов не заявляется.

## Сейчас важнее всего

1. Получить воспроизводимый общий анализ девяти завершённых suite.
2. Уточнить и проверить каждую метрику по `summary.json` и JSONL.
3. Обновить thesis draft: в старой версии ещё написано, что сценарии выполнены по одному разу и повторные PUT/DELETE не проверялись — это уже устарело.
4. Подготовить финальный текст и защиту. Не добавлять крупные функции до сдачи.

## Дополнение 21.11.2026 — исправление аналитики и валидации

- Валидатор вынесен в `scripts/validation.py`: missing/observed/unexpected/duplicate вычисляются по фактическому сопоставлению записей (request_id; поля key, revision, sequence, run_id, SHA-256 значения), PASS не присваивается по равенству количеств; несравнимые поля фиксируются явно.
- Reconnect-валидация теперь включает SHA-256; постфактум по сохранённым JSONL девяти серий: reconnect 960/960, compaction-tail 480/480 (только фаза 2), same-key 27/27 по типам; 0 аномалий (`reports/analysis/event_validation.json`).
- Метрики: `recovery_ms` → `client_recovery_interval_ms` (клиентский интервал `watch_canceled → watch_created`; старые summary читаются анализатором), `phase1_events_represented_by_snapshot` → `phase1_keys_in_snapshot` (число текущих ключей снапшота, не событий) в новых summary.
- `reports/analysis/` перегенерирован (переименованные колонки, аннотации на графиках, event_validation.json); `results/` не изменён — контрольные суммы 346 файлов до и после совпали.
- Добавлены 22 unit-теста валидатора (`scripts/test_validation.py`); `python -m py_compile` пройден для всех изменённых скриптов. etcd не запускался, новые эксперименты не проводились.
