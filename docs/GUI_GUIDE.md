# WatchLab Desktop — GUI Guide

WatchLab Desktop is a Tkinter frontend for the existing WatchLab research bench.
It unifies dashboard, demo control, event browsing, experiment overview and
analytics in one window. It is a **frontend and orchestrator only**:

- Go (bin/watchlab) remains the backend that talks to etcd;
- `scripts/demo.py` remains the demo orchestrator (the GUI starts it as a
  subprocess and parses its output; it does not re-implement it);
- `scripts/analyze_results.py` remains the analytics tool (the GUI's
  Recalculate button runs it as a subprocess).

## Requirements

- Python 3 with Tkinter (standard library; no extra pip dependencies).
  Debian/Ubuntu: `sudo apt install python3-tk`.
- The project venv (`.venv`) is used automatically by the run instructions
  below; plain `python3` works too if it has Tkinter.
- `bin/watchlab` (build with `go build -o bin/watchlab .` when missing or stale;
  the Dashboard shows its status).
- `etcd` / `etcdctl` on PATH for the demo and the etcd health check.

## Run

From the project root:

```bash
python scripts/gui.py          # or: .venv/bin/python scripts/gui.py
```

## Pages

### Dashboard
Real values only, read from files and live checks: Python and Go versions,
`bin/watchlab` status (ok / stale / missing — staleness compares binary mtime
with `main.go` / `recover.go` / `go.mod`), etcd reachability
(`etcdctl endpoint health`, checked on demand), number of completed series and
selected series (the nine-suite main sample), verified events count from
`reports/analysis/event_validation.json`, and the latest demo result
(`/tmp/watchlab-demo-*/demo-summary.json`). Quick actions: Run demo,
Recalculate analytics, Refresh.

### Recovery
Controls `python scripts/demo.py` via a managed subprocess. Shows the ten
recovery stages as they actually happen (parsed from demo.py output markers —
no stage is shown as done before its marker arrives), a live log with
timestamps, and a compact run summary. Stage list: Starting isolated etcd →
Watch created → Producer started → Client disconnected → Reconnect with L+1 →
Compaction executed → Compaction detected → Range snapshot created → Watch
restarted with R+1 → Validation completed. Stop button sends SIGINT/SIGTERM to
the demo process tree only (the same sequence demo.py itself uses for its
children). The demo is safe: it runs its own isolated etcd on a free port with
a throwaway data dir under /tmp.

After a finished run the card header offers **Inspect recovered state**. The
inspector opens a separate window over the page (no new sidebar entry) and
shows the state file that the Go `recover` command saved
(`/tmp/watchlab-demo-*/compaction/state.json`, resolved from the finished
demo's `demo-summary.json`, falling back to the newest file). It never
queries etcd — only the saved file is read. The window shows:

- Snapshot revision (R) — the revision of the last Range snapshot;
- Last applied revision (L) — the checkpoint of the last fully applied
  revision (these two are always shown as separate values);
- Current keys — the size of the current state map (not a historical event
  count);
- Data, key+value bytes — the `data_bytes` field (sum of key+value UTF-8
  lengths, not the on-disk JSON size; the file size on disk and
  `updated_at` are shown next to the state file path);
- a note stating whether Watch events were applied after the snapshot
  (checkpoint delta L−R and how many keys have `mod_revision > R`).

Below the metrics is a table (key / mod_revision / value) with a live
case-insensitive key-substring search and a scope filter relative to the
snapshot revision: all keys, changed after the snapshot (`mod_revision > R`,
shown in violet) or from the snapshot (`mod_revision ≤ R`). Values are
truncated in the table; double-click a row (or "View full value") opens the
full value with a pretty-printed JSON view and a "copy value" button that
copies the raw string. A missing, corrupted or unexpected state file is
reported in a banner instead of a table; the table caps at the first 500
matching rows and the counter always shows the true total. Refresh re-reads
the same path after a new demo run.

### Events
Paginated table of saved JSONL events for one suite at a time. Three groups:
Producer acknowledgements, Watch events, Recover events. Filters: event type,
revision (exact), key substring, run_id substring. Records are streamed line by
line; never the whole file. The page size default is 100 rows.

### Experiments
Table of existing series (one row per suite x scenario): suite_id,
events_per_phase, scenario, observed/missing/duplicates/unexpected, status.
The nine-suite main sample is marked `ok`; suites outside it are labelled
`excluded (not in selected nine)`; suites without a valid summary.json are
`excluded (incomplete)` (e.g. `auto-20261119-230935`). The GUI never starts
experiments: compaction acts on the whole MVCC backend of the target etcd.

### Analytics
Reads `reports/analysis/`: group statistics table (with a metric filter),
PNG charts with a viewer button (opens the system image viewer), and the
metric definitions from `reports/analysis/README.md`. Recalculate runs
`python scripts/analyze_results.py` as a subprocess and refreshes the page.

### Logs
Table of all GUI-started processes with status, exit code, duration and log
file; live output pane (monospaced, timestamps, auto-scroll toggle), Stop
selected button, and a link to the log directory (`logs/gui/`, one file per
run). Only GUI-started processes appear here and only they can be stopped.

## Process handling

- Long commands use `subprocess.Popen` with a reader thread; output reaches
  the UI through a thread-safe queue and is drained on the UI thread only
  (Tkinter is never touched from worker threads).
- Closing the window stops every GUI-started process (SIGINT → SIGTERM →
  SIGKILL on the child's own process group) and waits for the reader threads.
  Pre-existing etcd or WatchLab processes are never touched.
- All subprocess output is saved to `logs/gui/<name>-<timestamp>.log`.

## Failure handling

- Missing Tkinter: clear message with the system package name, exit code 2.
- Missing / stale / unbuildable Go binary: shown on the Dashboard; the demo
  refuses to start with an explanatory status.
- Unreachable etcd: health check reports the etcdctl error text; the demo
  still runs because it starts its own isolated etcd.
- demo.py failure: the exit code and the last log lines are shown; the run
  summary is read from demo-summary.json when written.
- Missing / corrupted / unexpected recovery state.json: the Recovery State
  Inspector shows a banner naming the cause and the path instead of a table;
  it never queries etcd to "fix" the file.
- Corrupted summary.json / JSONL / CSV: corrupt lines are skipped (truncated
  final lines tolerated like in run_experiments.py); unreadable summaries
  make the suite "excluded" instead of crashing the GUI.
- Missing charts: the Analytics page shows an empty state instead of an error.

## Architecture

```
scripts/gui.py          entry point (Tkinter availability check)
scripts/gui/
  __init__.py           package doc
  theme.py              dark graphite/violet palette, fonts, ttk styles
  process.py            ProcessRunner: managed Popen + reader thread + queue
  services.py           Tkinter-free service layer (files, subprocess checks)
  app.py                main window: sidebar, status bar, page dispatch
  dashboard.py          Dashboard page
  recovery.py           Recovery page (demo control + stage model)
  state_inspector.py    Recovery State Inspector (state.json viewer dialog)
  events.py             Events page (paginated JSONL tables)
  experiments.py        Experiments page (series table)
  analytics.py          Analytics page (stats, charts, definitions)
  logs.py               Logs page
scripts/test_gui_services.py   unittest for the service layer
docs/GUI_GUIDE.md       this file
```

## Tests

```bash
python -m py_compile scripts/gui.py scripts/gui/*.py
.venv/bin/python -m unittest scripts.test_gui_services -v
```

The tests cover summary.json reading/validation, suite completeness, JSONL
loading with normalization and filtering, corrupted-file handling, demo
summary discovery, analytics loaders, binary staleness, the recovery
state.json parsing (snapshot_revision vs last_revision vs mod_revision,
missing/corrupted/unexpected/empty files, key filtering) and the managed
ProcessRunner (start/stream/stop/exit codes). The GUI itself was smoke-tested
headlessly under Xvfb (all pages visited, demo control wired, process
start/stop/close verified; the Recovery State Inspector verified against a
real demo state file — metrics, filtering, value dialog and the
missing-file banner).

## Limitations

- No new experiments can be started from the GUI (by design).
- The Events page filters match the saved JSONL record shapes of the
  automated suites; other file layouts would need small parser additions.
- Chart images are opened in an external viewer (xdg-open); no embedded
  plotting, to avoid extra dependencies.
- The etcd health check on the Dashboard is on demand, not polled
  continuously.
