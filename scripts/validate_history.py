#!/usr/bin/env python3
"""Validate WatchLab producer acknowledgements against etcd Watch events."""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, raw in enumerate(stream, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            value["_source"] = f"{path}:{line_no}"
            records.append(value)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare WatchLab producer JSONL records with watcher JSONL events."
    )
    parser.add_argument("--producer", action="append", required=True, help="producer JSONL; repeatable")
    parser.add_argument("--watcher", action="append", required=True, help="watcher JSONL; repeatable")
    args = parser.parse_args()

    producers: dict[str, dict[str, Any]] = {}
    watcher_events: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    latencies: list[float] = []
    ordered_revisions: list[int] = []

    try:
        for filename in args.producer:
            for record in load_jsonl(Path(filename)):
                if record.get("type") != "put_ack":
                    continue
                request_id = record.get("request_id")
                if not request_id:
                    errors.append(f"{record['_source']}: missing request_id")
                    continue
                if request_id in producers:
                    errors.append(f"duplicate producer request_id: {request_id}")
                producers[request_id] = record
                try:
                    latencies.append(float(record["latency_ms"]))
                except (KeyError, TypeError, ValueError):
                    errors.append(f"{record['_source']}: invalid latency_ms")

        for filename in args.watcher:
            for record in load_jsonl(Path(filename)):
                if record.get("type") != "watch_event":
                    continue
                if record.get("event_type") != "PUT":
                    errors.append(f"{record['_source']}: unexpected event type {record.get('event_type')!r}")
                try:
                    payload = json.loads(record.get("value", ""))
                except json.JSONDecodeError:
                    errors.append(f"{record['_source']}: event value is not valid JSON")
                    continue
                request_id = payload.get("request_id")
                if not request_id:
                    errors.append(f"{record['_source']}: payload has no request_id")
                    continue
                if request_id in watcher_events:
                    errors.append(f"duplicate watcher event request_id: {request_id}")
                watcher_events[request_id] = record
                try:
                    ordered_revisions.append(int(record["event_revision"]))
                except (KeyError, TypeError, ValueError):
                    errors.append(f"{record['_source']}: invalid event_revision")
                    continue

                # Compare after all files are loaded below; watcher/producers may be passed in any order.

    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    # Compare after loading all producers/watchers, so file argument order does not matter.
    for request_id, event in watcher_events.items():
        try:
            payload = json.loads(event.get("value", ""))
        except json.JSONDecodeError:
            continue
        producer = producers.get(request_id)
        if producer is None:
            errors.append(f"watcher event has no matching producer acknowledgement: {request_id}")
        else:
            compare_record(event, payload, producer, errors)

    missing = sorted(set(producers) - set(watcher_events))
    if missing:
        errors.append(f"{len(missing)} acknowledged producer events were not observed by Watcher")

    monotonic = all(a <= b for a, b in zip(ordered_revisions, ordered_revisions[1:]))
    if not monotonic:
        errors.append("watcher event revisions are not in non-decreasing order")

    print("=== WatchLab history validation ===")
    print(f"Producer acknowledgements : {len(producers)}")
    print(f"Watcher PUT events        : {len(watcher_events)}")
    print(f"Matched events            : {len(set(producers) & set(watcher_events))}")
    print(f"Missing watcher events    : {len(missing)}")
    print(f"Watcher revisions ordered: {'YES' if monotonic else 'NO'}")
    if latencies:
        print(f"PUT latency mean          : {statistics.fmean(latencies):.3f} ms")
        print(f"PUT latency median        : {statistics.median(latencies):.3f} ms")
        print(f"PUT latency min/max       : {min(latencies):.3f} / {max(latencies):.3f} ms")
        print(f"Latency sample count      : {len(latencies)} (descriptive only; not a benchmark)")
    if errors:
        print("\nVALIDATION: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1
    print("\nVALIDATION: PASS")
    return 0


def compare_record(
    event: dict[str, Any], payload: dict[str, Any], producer: dict[str, Any], errors: list[str]
) -> None:
    request_id = payload.get("request_id")
    checks = {
        "key": (event.get("key"), producer.get("key")),
        "event_revision": (event.get("event_revision"), producer.get("revision")),
        "payload run_id": (payload.get("run_id"), producer.get("run_id")),
        "payload sequence": (payload.get("sequence"), producer.get("sequence")),
    }
    for label, (actual, expected) in checks.items():
        if actual != expected:
            errors.append(
                f"request {request_id}: {label} mismatch: watcher={actual!r}, producer={expected!r}"
            )
    digest = hashlib.sha256(event.get("value", "").encode("utf-8")).hexdigest()
    if digest != producer.get("value_sha256"):
        errors.append(f"request {request_id}: value SHA-256 mismatch")


if __name__ == "__main__":
    raise SystemExit(main())
