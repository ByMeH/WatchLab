#!/usr/bin/env python3
"""Run reproducible WatchLab reconnect, compaction-recovery, and mutation pilots.

Requires: Python 3.10+, etcd/etcdctl, and a built ./bin/watchlab binary.
Run from the WatchLab project root. Uses only the Python standard library.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Event-level validation helpers live next to this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import validation


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # A process may have been interrupted during the final write.
                if number == sum(1 for _ in path.open(encoding="utf-8")):
                    continue
                raise
            if isinstance(value, dict):
                records.append(value)
    return records


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def command(args: list[str], *, timeout: int = 60) -> str:
    print("$", " ".join(args), flush=True)

    proc = subprocess.Popen(
        args,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    output = []

    try:
        assert proc.stdout is not None

        for line in proc.stdout:
            output.append(line)
            print(line, end="", flush=True)

        return_code = proc.wait(timeout=timeout)

    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        raise

    finally:
        if proc.stdout is not None:
            proc.stdout.close()

    result_text = "".join(output)

    if return_code != 0:
        raise RuntimeError(
            f"command failed ({return_code}): {' '.join(args)}"
        )

    return result_text


class ManagedProcess:
    def __init__(self, args: list[str], console_path: Path):
        console_path.parent.mkdir(parents=True, exist_ok=True)
        self.console_path = console_path
        self.handle = console_path.open("w", encoding="utf-8")
        print("$", " ".join(args), flush=True)
        self.proc = subprocess.Popen(args, stdout=self.handle, stderr=subprocess.STDOUT, text=True)

    def text(self) -> str:
        try:
            return self.console_path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return ""

    def wait_text(self, needle: str, timeout: float = 20.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            content = self.text()
            if needle in content:
                return content
            code = self.proc.poll()
            if code is not None:
                raise RuntimeError(f"process exited with code {code} before {needle!r}; log: {self.console_path}\n{content}")
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {needle!r}; log: {self.console_path}\n{self.text()}")

    def wait_jsonl_count(self, path: Path, record_type: str, count: int, timeout: float = 20.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = read_jsonl(path)
            found = sum(r.get("type") == record_type for r in records)
            if found >= count:
                return records
            code = self.proc.poll()
            if code is not None:
                raise RuntimeError(f"process exited with code {code} waiting for {count} {record_type} records; log: {self.console_path}\n{self.text()}")
            time.sleep(0.05)
        raise TimeoutError(f"timed out waiting for {count} {record_type} records in {path}; console:\n{self.text()}")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self.handle.close()
        content = self.text()
        if "RECOVER_STOPPED" not in content and "WATCH_STOPPED" not in content:
            print(f"NOTICE: process stopped without normal stop marker; inspect {self.console_path}", flush=True)


def run_producer(binary: Path, endpoint: str, run_id: str, prefix: str, count: int, interval: str, out: Path) -> list[dict[str, Any]]:
    command([
        str(binary), "produce", "--endpoint", endpoint,
        "--run-id", run_id, "--prefix", prefix,
        "--count", str(count), "--interval", interval,
        "--out", str(out),
    ])
    records = read_jsonl(out)
    require(len(records) == count, f"Producer expected {count} records, found {len(records)} in {out}")
    require(all(r.get("type") == "put_ack" for r in records), f"Unexpected Producer record type in {out}")
    return records


def validate_reconnect(
    producers: list[dict[str, Any]],
    initial: list[dict[str, Any]],
    resumed: list[dict[str, Any]],
) -> dict[str, Any]:
    """Event-level reconnect validation (see scripts/validation.py).

    Matching is by the request_id embedded in the watcher payload; every
    matched event is compared by key, revision, payload sequence, payload
    run_id and SHA-256 of the raw value string. missing/duplicate/unexpected
    counts are computed from the actual matching, never from event counts.
    """
    report = validation.validate_reconnect_split(producers, initial, resumed)
    require(report["ok"], "Reconnect validation failed: " + validation.format_anomalies(report))
    return report


def timestamp(record: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))


def run_suite(args: argparse.Namespace) -> Path:
    root = Path.cwd()
    binary = (root / args.binary).resolve()
    require(binary.is_file(), f"WatchLab binary not found: {binary}; build it with go build -o bin/watchlab .")
    require((root / "go.mod").is_file(), "Run this script from the WatchLab project root")
    require(args.count >= 2, "--count must be at least 2")
    require(args.count <= 10000, "--count must not exceed 10000")
    for tool in ("etcdctl",):
        require(subprocess.run(["bash", "-lc", f"command -v {tool}"], stdout=subprocess.DEVNULL).returncode == 0,
                f"Required command not found: {tool}")

    command(["etcdctl", f"--endpoints={args.endpoint}", "endpoint", "health"])
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suite_id = f"auto-{stamp}"
    out_dir = root / "results" / "automated" / suite_id
    out_dir.mkdir(parents=True, exist_ok=False)
    summary: list[dict[str, Any]] = []
    n = args.count
    interval = args.interval

    print(f"\n=== WatchLab automated suite: {suite_id} ===", flush=True)
    print(f"Output directory: {out_dir}", flush=True)

    # A. Reconnect using the last fully observed event revision.
    print("\n=== A. RECONNECT ===", flush=True)
    prefix = f"/watchlab/automated/{suite_id}/reconnect/events/"
    initial_jsonl = out_dir / "reconnect-initial-watch.jsonl"
    resumed_jsonl = out_dir / "reconnect-resumed-watch.jsonl"
    p1_jsonl = out_dir / "reconnect-producer-p1.jsonl"
    p2_jsonl = out_dir / "reconnect-producer-p2.jsonl"
    initial = ManagedProcess([str(binary), "watch", "--endpoint", args.endpoint, "--run-id", f"{suite_id}-recon-a", "--prefix", prefix, "--out", str(initial_jsonl)], out_dir / "reconnect-initial-console.log")
    initial.wait_text("WATCH_CREATED", args.timeout)
    p1 = run_producer(binary, args.endpoint, f"{suite_id}-recon-p1", prefix + "phase1/", n, interval, p1_jsonl)
    initial.wait_jsonl_count(initial_jsonl, "watch_event", n, args.timeout)
    initial.stop()
    first_watch = read_jsonl(initial_jsonl)
    first_events = [r for r in first_watch if r.get("type") == "watch_event"]
    require(len(first_events) == n, f"Initial Watch expected {n} events, got {len(first_events)}")
    last_revision = max(r["event_revision"] for r in first_events)
    p2 = run_producer(binary, args.endpoint, f"{suite_id}-recon-p2", prefix + "phase2/", n, interval, p2_jsonl)
    resumed = ManagedProcess([
        str(binary), "watch", "--endpoint", args.endpoint,
        "--run-id", f"{suite_id}-recon-b", "--prefix", prefix,
        "--start-revision", str(last_revision + 1), "--out", str(resumed_jsonl),
    ], out_dir / "reconnect-resumed-console.log")
    resumed.wait_jsonl_count(resumed_jsonl, "watch_event", n, args.timeout)
    resumed.stop()
    reconnect_initial = read_jsonl(initial_jsonl)
    reconnect_resumed = read_jsonl(resumed_jsonl)
    recon_report = validate_reconnect(p1 + p2, reconnect_initial, reconnect_resumed)
    summary.append({"scenario": "reconnect", "requested_events": 2*n,
                    "observed_events": recon_report["matched"],
                    "missing_events": recon_report["missing_count"],
                    "duplicate_events": recon_report["duplicate_count"],
                    "unexpected_events": recon_report["unexpected_count"],
                    "field_mismatches": recon_report["mismatch_count"],
                    "snapshot_keys": "", "snapshot_bytes": "",
                    "snapshot_range_ms": "", "client_recovery_interval_ms": "",
                    "final_keys": "", "last_revision": "",
                    "validation_scope": "event level: request_id+sequence+revision+key+value_sha256; resumed Watch must start at L+1"})
    print(f"RECONNECT PASS: {recon_report['matched']}/{2*n} matched, missing={recon_report['missing_count']}, "
          f"duplicates={recon_report['duplicate_count']}, unexpected={recon_report['unexpected_count']}", flush=True)

    # B. Force compaction past the saved checkpoint, then verify automatic resnapshot.
    print("\n=== B. COMPACTION RECOVERY ===", flush=True)
    prefix = f"/watchlab/automated/{suite_id}/compaction/events/"
    state_path = out_dir / "compaction-state.json"
    first_log = out_dir / "compaction-initial-recover.jsonl"
    second_log = out_dir / "compaction-recover-after.jsonl"
    first = ManagedProcess([
        str(binary), "recover", "--endpoint", args.endpoint,
        "--run-id", f"{suite_id}-comp-a", "--prefix", prefix,
        "--state", str(state_path), "--out", str(first_log),
    ], out_dir / "compaction-initial-console.log")
    first.wait_text("RECOVER_WATCH_CREATED", args.timeout)
    first.stop()
    cp1 = run_producer(binary, args.endpoint, f"{suite_id}-comp-p1", prefix + "phase1/", n, interval, out_dir / "compaction-producer-p1.jsonl")
    compact_revision = int(cp1[-1]["revision"])
    command(["etcdctl", f"--endpoints={args.endpoint}", "compact", str(compact_revision)])
    second = ManagedProcess([
        str(binary), "recover", "--endpoint", args.endpoint,
        "--run-id", f"{suite_id}-comp-b", "--prefix", prefix,
        "--state", str(state_path), "--out", str(second_log),
    ], out_dir / "compaction-recover-console.log")
    second.wait_text("RECOVER_COMPACTED", args.timeout)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        recovery_records = read_jsonl(second_log)
        snapshots = [r for r in recovery_records if r.get("type") == "snapshot"]
        created = [r for r in recovery_records if r.get("type") == "watch_created"]
        if snapshots and len(created) >= 2:
            break
        if second.proc.poll() is not None:
            raise RuntimeError(f"Recover exited during compaction restart:\n{second.text()}")
        time.sleep(0.05)
    else:
        raise TimeoutError(f"Recover did not create a new Watch after compaction:\n{second.text()}")

    # The new Watch is active; add the tail events and wait for them to be applied.
    cp2 = run_producer(binary, args.endpoint, f"{suite_id}-comp-p2", prefix + "phase2/", n, interval, out_dir / "compaction-producer-p2.jsonl")
    second.wait_jsonl_count(second_log, "watch_event", n, args.timeout)
    second.stop()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    recovery_records = read_jsonl(second_log)
    # Full per-event validation of the compaction tail against producer acks.
    tail_report = validation.validate_compaction_tail(
        cp2, recovery_records,
        phase2_prefix=prefix + "phase2/",
        expected_snapshot_keys=len(cp1),
    )
    require(tail_report["ok"], "Compaction-tail validation failed: " + validation.format_anomalies(tail_report))
    snapshots = [r for r in recovery_records if r.get("type") == "snapshot"]
    canceled = [r for r in recovery_records if r.get("type") == "watch_canceled"]
    created = [r for r in recovery_records if r.get("type") == "watch_created"]
    expected_keys = 2*n
    require(len(state.get("keys", {})) == expected_keys,
            f"Expected {expected_keys} keys after resnapshot and tail, got {len(state.get('keys', {}))}")
    require(state.get("last_revision") == int(cp2[-1]["revision"]),
            f"Final checkpoint {state.get('last_revision')} != last Producer revision {cp2[-1]['revision']}")
    # Client-side recovery interval: watch_canceled record -> replacement watch_created record.
    c_to_created = (timestamp(created[-1]) - timestamp(canceled[-1])).total_seconds() * 1000
    snapshot_to_created = (timestamp(created[-1]) - timestamp(snapshots[-1])).total_seconds() * 1000
    summary.append({"scenario": "compaction_recovery", "requested_events": n,
                    "observed_events": tail_report["matched"],
                    "missing_events": tail_report["missing_count"],
                    "duplicate_events": tail_report["duplicate_count"],
                    "unexpected_events": tail_report["unexpected_count"],
                    "field_mismatches": tail_report["mismatch_count"],
                    "phase1_keys_in_snapshot": snapshots[-1].get("key_count"),
                    "snapshot_keys": snapshots[-1].get("key_count"),
                    "snapshot_bytes": snapshots[-1].get("data_bytes"),
                    "snapshot_range_ms": snapshots[-1].get("duration_ms"),
                    "client_recovery_interval_ms": round(c_to_created, 3),
                    "snapshot_to_watch_ms": round(snapshot_to_created, 3),
                    "final_keys": len(state.get("keys", {})), "last_revision": state.get("last_revision"),
                    "validation_scope": "phase2 event level: request_id+sequence+revision+key+value_sha256; snapshot boundary checked; phase1 represented by snapshot keys, not events"})
    print(f"COMPACTION RECOVERY PASS: snapshot={snapshots[-1].get('key_count')} keys, "
          f"tail={tail_report['matched']}/{n}, final={len(state['keys'])} keys", flush=True)

    # C. Two PUT operations followed by DELETE on the same key.
    print("\n=== C. SAME-KEY PUT/PUT/DELETE ===", flush=True)
    prefix = f"/watchlab/automated/{suite_id}/mutations/"
    state_path = out_dir / "mutations-state.json"
    log_path = out_dir / "mutations-recover.jsonl"
    watcher = ManagedProcess([
        str(binary), "recover", "--endpoint", args.endpoint,
        "--run-id", f"{suite_id}-mut", "--prefix", prefix,
        "--state", str(state_path), "--out", str(log_path),
    ], out_dir / "mutations-console.log")
    watcher.wait_text("RECOVER_WATCH_CREATED", args.timeout)
    key = prefix + "shared-key"
    command(["etcdctl", f"--endpoints={args.endpoint}", "put", key, '{"version":1,"value":"first"}'])
    command(["etcdctl", f"--endpoints={args.endpoint}", "put", key, '{"version":2,"value":"updated"}'])
    command(["etcdctl", f"--endpoints={args.endpoint}", "del", key])
    watcher.wait_jsonl_count(log_path, "watch_event", 3, args.timeout)
    watcher.stop()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    mutation_records = read_jsonl(log_path)
    mut_report = validation.validate_mutation_sequence(mutation_records, key)
    require(mut_report["ok"], "Mutation validation failed: " + validation.format_anomalies(mut_report))
    require(key not in state.get("keys", {}), "Deleted key remains in local recovery state")
    summary.append({"scenario": "same_key_put_put_delete", "requested_events": 3,
                    "observed_events": mut_report["matched"],
                    "missing_events": mut_report["missing_count"],
                    "unexpected_events": mut_report["unexpected_count"],
                    "snapshot_keys": "", "snapshot_bytes": "",
                    "snapshot_range_ms": "", "client_recovery_interval_ms": "",
                    "final_keys": len(state.get("keys", {})),
                    "last_revision": state.get("last_revision"),
                    "validation_scope": "event_type sequence and order on the shared key; keys are written with etcdctl, so no producer acks and no SHA-256 comparison are available"})
    print(f"MUTATION PASS: PUT, PUT, DELETE; key absent; checkpoint={state['last_revision']}", flush=True)

    csv_path = out_dir / "summary.csv"
    fieldnames = sorted({key for row in summary for key in row})
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(summary)
    write_json(out_dir / "summary.json", {"suite_id": suite_id, "endpoint": args.endpoint,
                                          "events_per_phase": n, "interval": interval,
                                          "note": "One automated pilot suite; repeat suites before inferential conclusions.",
                                          "results": summary})
    print("\n=== SUITE COMPLETE ===", flush=True)
    print(f"CSV: {csv_path}", flush=True)
    print(f"JSON: {out_dir / 'summary.json'}", flush=True)
    print(f"Logs and raw records: {out_dir}", flush=True)
    return out_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WatchLab controlled recovery experiments against a dedicated test etcd instance.")
    parser.add_argument("--endpoint", default="http://127.0.0.1:2379")
    parser.add_argument("--binary", default="bin/watchlab")
    parser.add_argument("--count", type=int, default=10, help="events in each phase (default: 10)")
    parser.add_argument("--interval", default="100ms", help="interval passed to Producer (default: 100ms)")
    parser.add_argument("--timeout", type=float, default=30.0, help="seconds to wait for each process stage")
    args = parser.parse_args()
    try:
        out = run_suite(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("Any partial logs/results are retained under results/automated/.", file=sys.stderr)
        return 1
    print(f"SUCCESS: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
