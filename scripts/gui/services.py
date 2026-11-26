"""Tkinter-free service layer for WatchLab Desktop.

All filesystem access, subprocess checks and data parsing live here so the
pages stay thin and the logic is testable headless (no display required).

Data sources (read-only; raw experiment data is never modified):
  - bin/watchlab, main.go, recover.go, go.mod  -> Go backend status
  - results/automated/<suite>/summary.json     -> experiment series
  - reports/analysis/event_validation.json     -> per-event validation totals
  - reports/analysis/*.csv, *.png, README.md   -> analytics
  - /tmp/watchlab-demo-*/demo-summary.json     -> latest demo result
  - /tmp/watchlab-demo-*/compaction/state.json -> recovered local KV state
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
RESULTS_DIR = PROJECT_ROOT / "results"
AUTOMATED_DIR = RESULTS_DIR / "automated"
ANALYSIS_DIR = PROJECT_ROOT / "reports" / "analysis"
TMP_DIR = Path("/tmp")
DEMO_DIR_GLOB = "watchlab-demo-*"
DEMO_SUMMARY_NAME = "demo-summary.json"
DEFAULT_ENDPOINT = "http://127.0.0.1:2379"
GUI_LOG_DIR = PROJECT_ROOT / "logs" / "gui"

# Selected main sample: the nine verified suites fixed by
# scripts/analyze_results.py::DEFAULT_SUITES (never edited by the GUI).
try:  # import from the project script when available
    sys.path.insert(0, str(SCRIPTS_DIR))
    from analyze_results import DEFAULT_SUITES as _DEFAULT_SUITES, SCENARIOS as _SCENARIOS
except Exception:  # pragma: no cover - fallback keeps the GUI working
    _DEFAULT_SUITES = {  # keep in sync with analyze_results.py
        "auto-20261119-233338", "auto-20261119-233343", "auto-20261119-233348",
        "auto-20261119-233353", "auto-20261119-233415", "auto-20261119-233438",
        "auto-20261119-233500", "auto-20261119-233544", "auto-20261119-233628",
    }
    _SCENARIOS = ("reconnect", "compaction_recovery", "same_key_put_put_delete")

SELECTED_SUITES: frozenset[str] = frozenset(_DEFAULT_SUITES)
SCENARIOS: tuple[str, ...] = tuple(_SCENARIOS)
# event_validation.json uses different scenario keys than summary.json.
VALIDATION_SCENARIO_KEY = {
    "reconnect": "reconnect",
    "compaction_recovery": "compaction_tail",
    "same_key_put_put_delete": "same_key",
}
SCENARIO_LABEL = {
    "reconnect": "reconnect",
    "compaction_recovery": "compaction recovery",
    "same_key_put_put_delete": "same-key put/put/delete",
}


# --------------------------------------------------------------- environment
@dataclass
class EnvInfo:
    python_version: str
    go_version: str
    go_ok: bool
    binary_path: str
    binary_status: str  # "ok" | "missing" | "stale" | "go unavailable"
    binary_detail: str = ""
    etcd_path: str = ""
    etcdctl_path: str = ""
    demo_script: str = ""


def _run_version(cmd: list[str], timeout: float = 8.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False, ""
    out = (proc.stdout or "").strip()
    if proc.returncode != 0 or not out:
        return False, ""
    return True, out.splitlines()[0]


def binary_status(project_root: Path = PROJECT_ROOT) -> tuple[str, str, str]:
    """Status of bin/watchlab: present and how it compares to the Go sources.

    Mirrors scripts/demo.py::ensure_binary: the binary is stale when it is
    older than any of main.go / recover.go / go.mod.
    """
    binary = project_root / "bin" / "watchlab"
    if not binary.exists():
        return "missing", str(binary), ""
    go_ok, _ = _run_version(["go", "version"], timeout=8.0)
    sources = [project_root / name for name in ("main.go", "recover.go", "go.mod")]
    newest = max((p.stat().st_mtime for p in sources if p.exists()), default=0.0)
    if not go_ok:
        return "ok", str(binary), "go unavailable; cannot compare with sources"
    if binary.stat().st_mtime < newest:
        return "stale", str(binary), "bin/watchlab is older than the Go sources"
    return "ok", str(binary), "up to date with the Go sources"


def check_environment(project_root: Path = PROJECT_ROOT) -> EnvInfo:
    go_ok, go_version = _run_version(["go", "version"])
    status, binary_path, detail = binary_status(project_root)
    etcd_path = _which("etcd")
    etcdctl_path = _which("etcdctl")
    return EnvInfo(
        python_version=sys.version.split()[0],
        go_version=go_version or "unavailable",
        go_ok=go_ok,
        binary_path=binary_path,
        binary_status=status,
        binary_detail=detail,
        etcd_path=etcd_path,
        etcdctl_path=etcdctl_path,
        demo_script=str(SCRIPTS_DIR / "demo.py"),
    )


def _which(name: str) -> str:
    """Locate an executable on PATH (shutil.which without the import cost)."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def etcd_health(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 4.0) -> tuple[bool, str]:
    """Check one etcd endpoint. Blocking; run it from a worker thread.

    Uses `etcdctl endpoint health` with a command timeout (3-5s window).
    """
    etcdctl = _which("etcdctl")
    if not etcdctl:
        return False, "etcdctl not found in PATH"
    try:
        proc = subprocess.run(
            [etcdctl, f"--endpoints={endpoint}", "endpoint", "health",
             f"--command-timeout={max(1.0, timeout - 1.0):.0f}s"],
            capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout:.0f}s"
    except OSError as exc:
        return False, f"cannot run etcdctl: {exc}"
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0 and "is healthy" in out:
        return True, "healthy"
    detail = " ".join(out.split())[:120] or f"exit code {proc.returncode}"
    return False, detail


# ------------------------------------------------------------- experiment series
def read_json(path: Path) -> Any:
    """Parse JSON or raise ValueError with a readable message."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"corrupted JSON in {path}: {exc}") from exc


def suite_summary_path(suite_id: str, root: Path = AUTOMATED_DIR) -> Path:
    return root / suite_id / "summary.json"


def load_suite_summary(suite_id: str, root: Path = AUTOMATED_DIR) -> dict[str, Any]:
    """Validated summary.json contents; raises ValueError when unusable."""
    data = read_json(suite_summary_path(suite_id, root))
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError(f"unexpected summary.json structure in {suite_id}")
    if not isinstance(data["results"], list):
        raise ValueError(f"summary.json 'results' is not a list in {suite_id}")
    return data


def suite_is_complete(suite_id: str, root: Path = AUTOMATED_DIR) -> tuple[bool, str]:
    """(complete, reason) for one suite directory."""
    path = suite_summary_path(suite_id, root)
    if not path.exists():
        return False, "summary.json is missing"
    try:
        data = load_suite_summary(suite_id, root)
    except ValueError as exc:
        return False, str(exc)
    try:
        present = {str(row["scenario"]) for row in data["results"]}
    except (KeyError, TypeError):
        return False, "summary.json scenarios are unreadable"
    missing = [s for s in SCENARIOS if s not in present]
    if missing:
        return False, "missing scenarios: " + ", ".join(missing)
    return True, "complete"


def completed_series(root: Path = AUTOMATED_DIR) -> list[str]:
    """Suite IDs with a valid summary.json and all scenarios completed."""
    return sorted(s for s in _suite_dirs(root) if suite_is_complete(s, root)[0])


def excluded_series(root: Path = AUTOMATED_DIR) -> list[tuple[str, str]]:
    """Suite IDs without a completed summary plus the reason."""
    return [(s, suite_is_complete(s, root)[1]) for s in _suite_dirs(root)
            if not suite_is_complete(s, root)[0]]


def _suite_dirs(root: Path = AUTOMATED_DIR) -> list[str]:
    if not root.exists():
        return []
    return [p.name for p in sorted(root.iterdir())
            if p.is_dir() and p.name.startswith("auto-")]


def load_validation_totals(root: Path = ANALYSIS_DIR) -> dict[str, Any] | None:
    """reports/analysis/event_validation.json or None when unavailable."""
    path = root / "event_validation.json"
    if not path.exists():
        return None
    try:
        data = read_json(path)
    except ValueError:
        return None
    return data if isinstance(data, dict) and "totals" in data else None


def verified_events(root: Path = ANALYSIS_DIR) -> int | None:
    """Sum of matched events across validation categories (960+480+27=1467)."""
    data = load_validation_totals(root)
    if not data:
        return None
    total = 0
    for category in data["totals"].values():
        if isinstance(category, dict) and isinstance(category.get("matched"), int):
            total += category["matched"]
    return total or None


def selected_series() -> int:
    return len(SELECTED_SUITES)


# --------------------------------------------------------------- demo results
@dataclass
class DemoSummary:
    demo_id: str
    started_at: str
    overall: str
    verified_events: int | None
    scenarios: list[dict[str, Any]] = field(default_factory=list)
    path: str = ""
    mtime: float = 0.0

    def status_word(self) -> str:
        return self.overall if self.overall else "unknown"


def last_demo_summary(tmp_dir: Path = TMP_DIR) -> DemoSummary | None:
    """Newest demo-summary.json under /tmp/watchlab-demo-*/ (by mtime)."""
    candidates: list[tuple[float, Path]] = []
    for path in tmp_dir.glob(f"{DEMO_DIR_GLOB}/{DEMO_SUMMARY_NAME}"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        return None
    _, path = max(candidates, key=lambda pair: pair[0])
    try:
        data = read_json(path)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    scenarios = data.get("scenarios")
    verified = data.get("total_verified_events")
    return DemoSummary(
        demo_id=str(data.get("demo_id", "?")),
        started_at=str(data.get("started_at", "")),
        overall=str(data.get("overall", "")),
        verified_events=int(verified) if isinstance(verified, int) else None,
        scenarios=scenarios if isinstance(scenarios, list) else [],
        path=str(path),
        mtime=path.stat().st_mtime,
    )


# --------------------------------------------------------------- recovery state
SCOPE_ALL = "all"
SCOPE_CHANGED_AFTER_SNAPSHOT = "changed_after_snapshot"
SCOPE_FROM_SNAPSHOT = "from_snapshot"
SCOPE_LABELS: dict[str, str] = {
    SCOPE_ALL: "all keys",
    SCOPE_CHANGED_AFTER_SNAPSHOT: "changed after snapshot (mod_revision > R)",
    SCOPE_FROM_SNAPSHOT: "from snapshot (mod_revision \u2264 R)",
}


class RecoveryStateError(ValueError):
    """A recovery state file is missing, unreadable, corrupted or alien.

    Subclass of ValueError so existing `except ValueError` handling still
    works. `reason` is one of "missing" | "unreadable" | "corrupted" |
    "unexpected"; the Recovery State Inspector shows a distinct message per
    cause.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class StateKV:
    """One key of the recovered state map (recover.go::recoveryKV)."""

    key: str
    value: str
    mod_revision: int | None = None
    create_revision: int | None = None
    version: int | None = None


@dataclass(frozen=True)
class RecoveryState:
    """Parsed state.json written by the Go recover command.

    The three revision kinds are kept strictly apart:
      snapshot_revision (R) - revision of the last Range snapshot;
      last_revision (L)     - checkpoint: last fully applied revision;
      mod_revision          - last-change revision of one key.
    `keys` is the current state map; its size is NOT a historical event
    count. `data_bytes` is the sum of key+value UTF-8 lengths (recover.go),
    not the on-disk JSON size (see `file_size_bytes`).
    """

    path: str
    schema_version: int | None
    prefix: str
    snapshot_revision: int | None
    last_revision: int | None
    updated_at: str
    data_bytes: int | None
    file_size_bytes: int
    keys: tuple[StateKV, ...]

    @property
    def key_count(self) -> int:
        return len(self.keys)

    @property
    def checkpoint_delta(self) -> int | None:
        """Revisions applied after the snapshot (L - R); None when unknown."""
        if self.snapshot_revision is None or self.last_revision is None:
            return None
        return self.last_revision - self.snapshot_revision

    @property
    def changed_after_snapshot(self) -> tuple[StateKV, ...]:
        """Keys whose mod_revision is newer than the snapshot revision R.

        Those changes were applied by Watch events after the snapshot was
        taken; keys with mod_revision <= R come from the snapshot itself.
        """
        if self.snapshot_revision is None:
            return ()
        return tuple(kv for kv in self.keys
                     if kv.mod_revision is not None
                     and kv.mod_revision > self.snapshot_revision)


def _int_or_none(container: dict[str, Any], field: str,
                 state_path: Path) -> int | None:
    value = container.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecoveryStateError(
            "unexpected", f"field {field!r} must be an integer in {state_path}")
    return value


def load_recovery_state(path: str | Path) -> RecoveryState:
    """Parse a recovery state file (recover.go recoveryState JSON).

    Raises RecoveryStateError with `reason`:
      "missing"    - the file does not exist;
      "unreadable" - the file exists but cannot be read;
      "corrupted"  - the file is not valid JSON;
      "unexpected" - valid JSON but not the expected state structure.
    """
    state_path = Path(path)
    if not state_path.exists():
        raise RecoveryStateError("missing", f"state file not found: {state_path}")
    try:
        data = read_json(state_path)
    except ValueError as exc:
        reason = "corrupted" if "corrupted JSON in" in str(exc) else "unreadable"
        raise RecoveryStateError(reason, str(exc)) from exc
    if not isinstance(data, dict):
        raise RecoveryStateError(
            "unexpected", f"state file is not a JSON object: {state_path}")
    keys_raw = data.get("keys", {})
    if not isinstance(keys_raw, dict):
        raise RecoveryStateError(
            "unexpected", f"'keys' is not an object in {state_path}")
    prefix = data.get("prefix", "")
    updated_at = data.get("updated_at", "")
    if not isinstance(prefix, str) or not isinstance(updated_at, str):
        raise RecoveryStateError(
            "unexpected", f"'prefix'/'updated_at' must be strings in {state_path}")
    entries: list[StateKV] = []
    for key, entry in keys_raw.items():
        if not isinstance(entry, dict):
            raise RecoveryStateError(
                "unexpected", f"entry for key {key!r} is not an object in {state_path}")
        value = entry.get("value", "")
        if not isinstance(value, str):
            raise RecoveryStateError(
                "unexpected", f"'value' for key {key!r} is not a string in {state_path}")
        entries.append(StateKV(
            key=str(key),
            value=value,
            mod_revision=_int_or_none(entry, "mod_revision", state_path),
            create_revision=_int_or_none(entry, "create_revision", state_path),
            version=_int_or_none(entry, "version", state_path),
        ))
    entries.sort(key=lambda kv: kv.key)
    try:
        file_size = state_path.stat().st_size
    except OSError:
        file_size = 0
    return RecoveryState(
        path=str(state_path),
        schema_version=_int_or_none(data, "schema_version", state_path),
        prefix=prefix,
        snapshot_revision=_int_or_none(data, "snapshot_revision", state_path),
        last_revision=_int_or_none(data, "last_revision", state_path),
        updated_at=updated_at,
        data_bytes=_int_or_none(data, "data_bytes", state_path),
        file_size_bytes=file_size,
        keys=tuple(entries),
    )


def demo_state_path(summary_path: str | Path) -> str | None:
    """`paths.state_file` recorded in a demo-summary.json, or None."""
    try:
        data = read_json(Path(summary_path))
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    paths = data.get("paths")
    if not isinstance(paths, dict):
        return None
    state_file = paths.get("state_file")
    if isinstance(state_file, str) and state_file:
        return state_file
    return None


def latest_recovery_state_path(tmp_dir: Path = TMP_DIR) -> Path | None:
    """Newest compaction/state.json under /tmp/watchlab-demo-*/ (by mtime)."""
    candidates: list[tuple[float, Path]] = []
    for path in tmp_dir.glob(f"{DEMO_DIR_GLOB}/compaction/state.json"):
        try:
            candidates.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda pair: pair[0])[1]


def filter_state_keys(state: RecoveryState, substring: str = "",
                      scope: str = SCOPE_ALL) -> tuple[StateKV, ...]:
    """Client-side filter over the parsed state keys.

    `substring` matches case-insensitively anywhere in the key. `scope`
    selects relative to the snapshot revision R: changed after the snapshot
    (mod_revision > R, i.e. applied by Watch events) or from the snapshot
    itself (mod_revision <= R). Keys without a mod_revision can only be
    classified as "all".
    """
    needle = substring.strip().lower()
    result: list[StateKV] = []
    for kv in state.keys:
        if needle and needle not in kv.key.lower():
            continue
        if scope == SCOPE_CHANGED_AFTER_SNAPSHOT:
            if kv.mod_revision is None or state.snapshot_revision is None \
                    or kv.mod_revision <= state.snapshot_revision:
                continue
        elif scope == SCOPE_FROM_SNAPSHOT:
            if kv.mod_revision is not None and state.snapshot_revision is not None \
                    and kv.mod_revision > state.snapshot_revision:
                continue
        result.append(kv)
    return tuple(result)


# --------------------------------------------------------------- experiments table
@dataclass
class SuiteRow:
    suite_id: str
    events_per_phase: int | str
    scenario: str
    observed_events: int | str
    missing_events: int | str
    duplicates: int | str
    unexpected_events: int | str
    status: str  # "ok" | "anomalies" | "excluded (...)" | "no validation"


def _validation_for_suite(validation: dict[str, Any] | None, suite_id: str,
                          scenario: str) -> dict[str, Any]:
    if not validation:
        return {}
    suites = validation.get("suites")
    if not isinstance(suites, dict):
        return {}
    data = suites.get(suite_id)
    if not isinstance(data, dict):
        return {}
    return data.get(VALIDATION_SCENARIO_KEY.get(scenario, scenario), {}) or {}


def build_suite_rows(root: Path = AUTOMATED_DIR, analysis: Path = ANALYSIS_DIR,
                     include_selected_only: bool = False) -> list[SuiteRow]:
    """Rows for the Experiments page: one row per suite x scenario.

    Incomplete suites (no summary.json) appear as excluded rows and are not
    part of the selected statistics.
    """
    validation = load_validation_totals(analysis)
    rows: list[SuiteRow] = []
    for suite_id in _suite_dirs(root):
        complete, _reason = suite_is_complete(suite_id, root)
        try:
            data = load_suite_summary(suite_id, root)
        except ValueError:
            data = None
        if not complete:
            rows.append(SuiteRow(suite_id, "-", "-", "-", "-", "-", "-",
                                 "excluded (incomplete)"))
            continue
        if include_selected_only and suite_id not in SELECTED_SUITES:
            continue
        phase = data.get("events_per_phase", "-") if data else "-"
        by_scenario = {}
        if data:
            by_scenario = {str(r.get("scenario")): r for r in data["results"]}
        for scenario in SCENARIOS:
            row = by_scenario.get(scenario, {})
            ev = _validation_for_suite(validation, suite_id, scenario)
            missing = row.get("missing_events", "-")
            dup = ev.get("duplicates", "-")
            unexpected = ev.get("unexpected", "-")
            ok = ev.get("ok")
            if suite_id not in SELECTED_SUITES:
                status = "excluded (not in selected nine)"
            elif ok is True and missing == 0:
                status = "ok"
            elif ok is False or (missing not in ("-", 0) and missing):
                status = "anomalies"
            else:
                status = "no validation"
            rows.append(SuiteRow(
                suite_id=suite_id,
                events_per_phase=phase,
                scenario=SCENARIO_LABEL.get(scenario, scenario),
                observed_events=row.get("observed_events", "-"),
                missing_events=missing,
                duplicates=dup,
                unexpected_events=unexpected,
                status=status,
            ))
    return rows


# --------------------------------------------------------------- events page
EVENT_SOURCES: dict[str, tuple[str, ...]] = {
    "producer": (
        "reconnect-producer-p1.jsonl", "reconnect-producer-p2.jsonl",
        "compaction-producer-p1.jsonl", "compaction-producer-p2.jsonl",
        "compaction-producer-p3.jsonl",
    ),
    "watch": (
        "reconnect-initial-watch.jsonl", "reconnect-resumed-watch.jsonl",
    ),
    "recover": (
        "compaction-initial-recover.jsonl", "compaction-recover-after.jsonl",
        "mutations-recover.jsonl",
    ),
}
EVENT_KIND_LABEL = {
    "producer": "Producer acknowledgements",
    "watch": "Watch events",
    "recover": "Recover events",
}


@dataclass
class EventRecord:
    kind: str
    source_file: str
    type: str
    revision: str
    key: str
    sequence: str
    request_id: str
    timestamp: str
    run_id: str

    def as_row(self) -> tuple[str, ...]:
        return (self.type, self.revision, self.key, self.sequence,
                self.request_id, self.timestamp, self.run_id)


def _first(record: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in record and record[name] not in (None, ""):
            return record[name]
    return ""


def _normalize(kind: str, source_file: str, record: dict[str, Any]) -> EventRecord:
    """Map a raw JSONL record to the table columns."""
    rtype = str(record.get("type", ""))
    if kind == "producer":
        rtype = "put_ack"
        revision = str(record.get("revision", ""))
        key = str(record.get("key", ""))
        sequence = str(record.get("sequence", ""))
        request_id = str(record.get("request_id", ""))
        timestamp = str(_first(record, "acknowledged_at", "started_at"))
    elif kind == "watch":
        rtype = str(record.get("event_type", "") or record.get("type", ""))
        revision = str(_first(record, "event_revision", "header_revision"))
        if record.get("type") == "watch_event" and record.get("event_type"):
            rtype = str(record["event_type"])  # PUT / DELETE
        key = str(record.get("key", ""))
        sequence = ""
        request_id = ""
        value = record.get("value")
        if isinstance(value, str) and value.startswith("{"):
            try:
                payload = json.loads(value)
                sequence = str(payload.get("sequence", ""))
                request_id = str(payload.get("request_id", ""))
            except json.JSONDecodeError:
                pass
        timestamp = str(_first(record, "received_at", "timestamp"))
    else:  # recover journal
        rtype = str(record.get("type", ""))
        if rtype == "watch_event" and record.get("event_type"):
            rtype = str(record["event_type"])  # PUT / DELETE
        revision = str(_first(record, "event_revision", "snapshot_revision",
                              "last_revision", "header_revision",
                              "start_revision", "compact_revision"))
        key = str(record.get("key", ""))
        sequence = ""
        request_id = ""
        timestamp = str(_first(record, "timestamp", "received_at"))
    return EventRecord(
        kind=kind, source_file=source_file, type=rtype, revision=revision,
        key=key, sequence=sequence, request_id=request_id,
        timestamp=timestamp, run_id=str(record.get("run_id", "")),
    )


@dataclass
class EventFilter:
    event_type: str = ""
    revision: str = ""
    key_substring: str = ""
    run_id: str = ""

    def matches(self, record: EventRecord) -> bool:
        if self.event_type and record.type != self.event_type:
            return False
        if self.revision:
            try:
                if int(record.revision) != int(self.revision):
                    return False
            except ValueError:
                return False
        if self.key_substring and self.key_substring.lower() not in record.key.lower():
            return False
        if self.run_id and self.run_id.lower() not in record.run_id.lower():
            return False
        return True


def iter_events(root: Path, suite_id: str, kind: str,
                filter_: EventFilter | None = None,
                max_lines: int = 200_000) -> Iterator[EventRecord]:
    """Stream matching records from the suite's JSONL files.

    Files are read line by line (never loaded whole); a truncated final
    line (crash during write) is skipped like scripts/run_experiments.py does.
    """
    filter_ = filter_ or EventFilter()
    directory = root / suite_id
    for name in EVENT_SOURCES.get(kind, ()):
        path = directory / name
        if not path.exists():
            continue
        count = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    count += 1
                    if count > max_lines:
                        return
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # truncated final write
                    if not isinstance(record, dict):
                        continue
                    normalized = _normalize(kind, name, record)
                    if filter_.matches(normalized):
                        yield normalized
        except OSError:
            continue


def event_types(root: Path, suite_id: str, kind: str) -> list[str]:
    """Distinct record types in the files (for the type filter combobox)."""
    seen: dict[str, None] = {}
    for record in iter_events(root, suite_id, kind):
        seen.setdefault(record.type or "(none)", None)
    return list(seen)


def load_events_page(root: Path, suite_id: str, kind: str, page: int,
                     page_size: int, filter_: EventFilter | None = None
                     ) -> tuple[list[EventRecord], int, int]:
    """One page of matching events plus (total_matched, total_pages)."""
    page = max(0, page)
    page_size = max(1, page_size)
    matched: list[EventRecord] = []
    total = 0
    for record in iter_events(root, suite_id, kind, filter_):
        total += 1
        if len(matched) < page_size and page * page_size <= total - 1 < (page + 1) * page_size:
            matched.append(record)
    pages = max(1, (total + page_size - 1) // page_size)
    return matched, total, pages


# --------------------------------------------------------------- analytics
def load_group_stats(root: Path = ANALYSIS_DIR) -> list[dict[str, str]]:
    """Rows of group_statistics.csv (list of dicts with string values)."""
    path = root / "group_statistics.csv"
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    except (OSError, UnicodeDecodeError, csv.Error):
        return []


def analysis_charts(root: Path = ANALYSIS_DIR) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.glob("*.png") if p.is_file())


def read_analysis_readme(root: Path = ANALYSIS_DIR) -> str:
    path = root / "README.md"
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def analysis_manifest_summary(root: Path = ANALYSIS_DIR) -> str:
    """Short human-readable line from analysis_manifest.json."""
    path = root / "analysis_manifest.json"
    if not path.exists():
        return "manifest unavailable"
    try:
        data = read_json(path)
    except ValueError:
        return "manifest corrupted"
    included = data.get("included_suite_ids")
    excluded = data.get("excluded_or_skipped")
    if isinstance(included, list) and isinstance(excluded, list):
        return f"{len(included)} included, {len(excluded)} excluded/skipped"
    return "manifest available"
