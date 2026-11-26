WatchLab Recover command — pilot implementation

Install
-------
1. Extract this bundle into the existing watchlab-pilot directory, alongside
   main.go and go.mod. Do not overwrite main.go with a different version.
2. From that directory run:
      ./install-recover.sh
   The script makes guarded, exact edits to main.go, runs gofmt, go test, and
   rebuilds bin/watchlab. If an expected source fragment is missing or occurs
   more than once, it stops rather than guessing.

Command
-------
./bin/watchlab recover \
  --run-id recover-r1 \
  --prefix /watchlab/compact-r1/events/ \
  --state results/recover-r1-state.json \
  --out results/recover-r1-first.jsonl

Behavior
--------
- If --state does not exist, one Range request obtains a consistent prefix
  snapshot and its response-header revision R. The state file is written
  atomically and starts with checkpoint R.
- Watch starts inclusively at R+1.
- PUT and DELETE events update the local key-value state. Each complete Watch
  response is journaled before the checkpoint is atomically advanced.
- If the saved checkpoint is too old because history was compacted, Watch is
  canceled; Recover takes a fresh Range snapshot and starts Watch at new R+1.
- Ctrl+C stops the command. The state file is kept for a later restart.

Important
---------
- Every invocation needs a NEW --out path because the existing JSONL helper
  deliberately refuses to overwrite result files. Reuse the same --state path
  when you intend to resume the saved local state.
- The local state is the current key-value view, not a reconstruction of every
  historical transition. Compaction can make intermediate changes unrecoverable.
- The JSONL event journal is at-least-once around a process crash: if the event
  log is synced but the state checkpoint is not yet replaced, replay can add
  duplicate records. This is preferable to silently advancing the checkpoint
  without a durable event record. State PUT/DELETE application is idempotent.
- This pilot uses one Range response for the prefix. It is suitable for the
  current small experiment dataset; a larger dataset may need paginated Range
  requests pinned to the same revision R.
- The pilot values are treated as UTF-8 strings, matching WatchLab's JSON test
  payloads. Arbitrary binary key/value support is not the target of this MVP.
