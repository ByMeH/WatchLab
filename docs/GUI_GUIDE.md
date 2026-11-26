# WatchLab Desktop — GUI Guide

WatchLab Desktop is a Tkinter frontend for the WatchLab research bench.
It provides a graphical interface for inspecting experiment results,
running the isolated demonstration, browsing events and viewing analytics.

The GUI is a frontend and process orchestrator. It does not reimplement
the Go backend, the demonstration or the analysis pipeline.

- Go (`bin/watchlab`) communicates with etcd and implements Producer,
  Watch and Recover operations.
- `scripts/demo.py` runs the isolated end-to-end demonstration.
- `scripts/analyze_results.py` aggregates existing experimental results
  and generates analytical reports.
- The GUI starts these Python scripts as subprocesses when required.

> **Platform status:** The current implementation is Linux/POSIX-oriented.
> Windows 10/11 and macOS have not been ported or validated.

## Requirements

- Python 3.10 or newer.
- Tkinter (Python standard library). On Debian/Ubuntu:
  `sudo apt install python3-tk`.
- Go 1.27.0 or newer, as required by `go.mod`.
- `etcd` and `etcdctl` available in PATH for the demonstration and
  the Dashboard health check.
- `matplotlib` is optional. It is needed to generate PNG charts;
  CSV and JSON analysis can still be produced without it.
- `xdg-open` is used on Linux to open charts and the GUI log directory.

The project virtual environment is not activated automatically.
Activate it before running the GUI, or explicitly use its Python interpreter.

The `demo.py` script checks the environment and builds `bin/watchlab`
when the binary is missing or older than the Go source files.

Manual build:

```bash
mkdir -p bin
go build -o bin/watchlab .
```



Run
Run from the project root:
python3 -m venv .venv
source .venv/bin/activate
python scripts/gui.py


Alternatively, without activating the environment:
.venv/bin/python scripts/gui.py


Pages
The main window contains six pages in the sidebar.
Dashboard
Displays information collected from project files and environment checks:
- Go backend binary status: ok, stale or missing.
  Staleness is determined by comparing the binary modification time
  with main.go, recover.go and go.mod.
- Python and Go toolchain versions.
- Reachability of the default etcd endpoint (127.0.0.1:2379),
  checked through etcdctl endpoint health.
- Number of completed experiment series.
- Number of selected series in the fixed nine-suite analytical sample.
- Number of verified events from
  reports/analysis/event_validation.json.
- Result of the most recent demonstration, read from its
  demo-summary.json.
Quick actions:
- Run demo starts the isolated demonstration.
- Refresh updates Dashboard information.
- Analyze results starts the analysis script and opens the Logs page.
The ordinary etcd health indicator refers to the default endpoint.
It is independent of the temporary etcd instance used by the demo.
Recovery
Controls scripts/demo.py through a managed subprocess.

The page provides:
- Run demo and Stop buttons.
- Configurable event count, event interval and timeout.
- Ten recovery stages.
- Live demonstration output with timestamps.
- A summary of the completed run.
The displayed stages are:
1. Starting isolated etcd.
2. Watch created.
3. Producer started.
4. Client disconnected.
5. Reconnect with L+1.
6. Compaction executed.
7. Compaction detected.
8. Range snapshot created.
9. Watch restarted with R+1.
10. Validation completed.
Stage completion is driven by output markers from demo.py.
A stage is not marked complete before the corresponding marker is received.

The demonstration starts its own temporary single-member etcd instance
on free localhost ports and uses a separate temporary data directory.
It does not compact the user's existing etcd instance.

The Stop button requests interruption of the GUI-started demo process.
If graceful shutdown does not finish, the GUI escalates termination
for its own dedicated process group. The demo manages cleanup of the
child processes it created. Pre-existing etcd and WatchLab processes
are not intentionally targeted.
Recovery State Inspector
After a demo has produced a recovery state file, the Recovery page
can open Inspect recovered state.

The Inspector opens in a separate window. It is not a seventh sidebar page.

It reads the state.json file saved by the Go Recover command.
The path is taken from the demo summary when available; otherwise,
the application searches for the newest matching recovery state file.

The Inspector does not query etcd. It only reads the saved file.

Displayed information:
- Snapshot revision (R): revision of the last Range snapshot.
- Last applied revision (L): checkpoint of the last fully applied revision.
- Current keys: number of keys in the current local state map.
- Data, key+value bytes: sum of UTF-8 key and value lengths,
  taken from data_bytes.
- State file size and updated_at.
The Inspector also explains whether Watch events were applied after
the snapshot. It uses the checkpoint difference L−R and key
mod_revision values.

The key table contains:
- Key.
- mod_revision.
- Value preview.
Available filters:
- Case-insensitive key substring search.
- All keys.
- Keys changed after the snapshot (mod_revision > R).
- Keys from the snapshot (mod_revision <= R).
Values are truncated in the table. Double-click a row or use
View full value to open the full value. The value dialog provides
a pretty-printed JSON view when possible and a button to copy the
raw string.

The table displays at most 500 matching rows. Its counter reports
the total number of matching keys.

Missing, corrupted, unreadable or unexpected state files are reported
in a banner instead of being displayed as a normal table.

The Inspector's Refresh button rereads the currently selected file.
To inspect a newer demo run, close the Inspector and reopen it from
the Recovery page after that run has finished.
Events
Displays saved JSONL records for one selected experiment suite.

Records are grouped into:
- Producer acknowledgements.
- Watch events.
- Recover events.
Filters include:
- Event type.
- Exact revision.
- Key substring.
- run_id substring.
Records are read line by line rather than loading an entire JSONL file
into memory. The default page size is 100 rows.
Experiments
Displays existing experiment series, with one row per suite and scenario.

The table includes:
- suite_id.
- events_per_phase.
- Scenario.
- Observed events.
- Missing events.
- Duplicate events.
- Unexpected events.
- Validation status.
The nine suites selected for the main analysis are marked as selected.
Other complete suites are labelled as excluded from the selected sample.
Suites without a valid summary.json are marked as incomplete.

The GUI does not start automated benchmark series from this page.
The separate scripts/run_experiments.py script is used for that purpose.

Compaction affects the entire MVCC history of the target etcd instance.
Only use automated experiments against a dedicated test instance.
Analytics
Reads existing data from reports/analysis/.

The page displays:
- Group statistics table.
- Metric filter.
- PNG chart previews.
- Buttons for opening charts in the system image viewer.
- Metric definitions from reports/analysis/README.md.
The Recalculate action starts:
python scripts/analyze_results.py


The analysis script aggregates existing experiment summaries.
It does not start new experiments.

If matplotlib is unavailable, CSV and JSON results can still be generated,
but PNG chart generation is skipped.
Logs
Displays processes started by the GUI.

The page provides:
- Process name and status.
- Exit code.
- Runtime duration.
- Associated log file.
- Live process output.
- Timestamps and auto-scroll controls.
- Stop selected action.
- Open log directory action.
GUI subprocess output is saved under logs/gui/, with a separate
log file for each run.

Only processes registered by the GUI are listed and managed here.
Process handling
Long-running commands are started with subprocess.Popen.

A reader thread collects subprocess output and writes it to a log file.
Output is passed to the Tkinter interface through a thread-safe queue.
Tkinter widgets are updated only from the main UI thread.

Each GUI-started process is tracked by a ProcessRunner.

When the main window closes, the application attempts to stop
its GUI-started processes. It does not intentionally stop pre-existing
etcd or unrelated WatchLab processes.
Failure handling
The GUI reports common failures:
- Missing Tkinter: a message explains the missing system component.
- Missing or stale Go binary: the Dashboard reports its status;
  the demo checks and builds the binary when necessary.
- Missing Go, etcd or etcdctl: the demo exits with an explanatory error.
- Unreachable default etcd endpoint: the Dashboard displays the
  etcdctl error or timeout.
- Demo failure: the exit code and recent output are available in
  the Recovery page and Logs page.
- Missing or corrupted recovery state: the Inspector displays
  the problem and file path.
- Corrupted experiment summaries or records: invalid data is handled
  by the corresponding data-loading and validation code.
- Missing charts: Analytics displays an empty state instead of a chart.
Architecture
scripts/gui.py              GUI entry point
scripts/gui/
  __init__.py               Package marker
  theme.py                  Colors, fonts and ttk styles
  process.py                Managed subprocess execution and output queue
  services.py               File access, environment checks and data parsing
  app.py                    Main window and page navigation
  dashboard.py              Dashboard page
  recovery.py               Demo controls and recovery stage display
  state_inspector.py        Read-only recovery state dialog
  events.py                 Paginated JSONL event browser
  experiments.py            Experiment series table
  analytics.py              Statistics and chart viewer
  logs.py                   GUI process log viewer

scripts/demo.py              Isolated end-to-end demonstration
scripts/run_experiments.py   Automated experiment runner
scripts/validation.py        Canonical event-level validator
scripts/analyze_results.py   Results analysis and graph generation

scripts/test_demo.py
scripts/test_gui_services.py
scripts/test_validation.py
recover_test.go


Tests
Run from the project root.

Check Python syntax:
python -m compileall -q scripts


Run all Python unit tests:
python -m unittest discover -s scripts -p "test_*.py" -v


Run GUI service tests only:
python -m unittest scripts.test_gui_services -v


Run Go tests:
go test -count=1 ./...


Run static Go analysis:
go vet ./...


The Python tests cover demo helpers, event validation, suite summaries,
JSONL handling, analytics loaders, binary staleness, Recovery State
Inspector parsing and filtering, and managed subprocess behavior.

The GUI has also been functionally exercised under Xvfb in Linux.
This does not replace a manual visual check on a real desktop.

