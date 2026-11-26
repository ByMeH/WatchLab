#!/usr/bin/env python3
"""Event-level validation for WatchLab producer/watcher/recover JSONL logs.

This module is the canonical implementation of the matching rules used by
run_experiments.py and by the offline re-validation CLI of this module
(see docs/reviews/ANALYTICS_CORRECTIONS.md). Matching is performed per event,
keyed by the request_id embedded in the watcher payload; it is never inferred
from event counts alone.

Definitions:
- expected    : producer put_ack acknowledgements (one per request_id)
- matched     : watcher watch_event (PUT) whose `value` payload carries the same request_id
- missing     : expected request_ids with no matched watcher event
- duplicate   : watcher events repeating an already matched request_id
- unexpected  : watcher events with no producer ack, with an unparseable payload,
                without a request_id, with a non-PUT event type in PUT-only
                comparisons, or outside the requested phase prefix
- mismatch    : per-field inequality (key, revision, sequence, run_id, value_sha256)
- incomparable: a field that could not be compared because the source record
                lacks the data; reported explicitly, never treated as success

A validation PASS requires zero missing, duplicate, unexpected, mismatch and
incomparable items. Equal event counts alone are never sufficient for a PASS.

The SHA-256 comparison hashes the watcher `value` string exactly as stored in
the JSONL (UTF-8 bytes of that string), which matches the producer's
value_sha256 computed over the original PUT bytes; the JSON payload is never
re-serialized before hashing.

Only the Python standard library is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

COMPARED_FIELDS = ("key", "revision", "sequence", "run_id", "value_sha256")


def sha256_hex(text: str) -> str:
    """SHA-256 over the exact UTF-8 bytes of the stored string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_event_payload(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the watcher `value` payload; return (payload, error)."""
    value = event.get("value")
    if value is None:
        return None, "watcher event has no value field"
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        return None, f"value is not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "value payload is not a JSON object"
    return payload, None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def match_events(
    producers: list[dict[str, Any]],
    watcher_records: list[dict[str, Any]],
    *,
    phase_prefix: str | None = None,
) -> dict[str, Any]:
    """Match producer acknowledgements to watcher events by request_id.

    Every matched pair is compared field by field (key, revision, payload
    sequence, payload run_id, SHA-256 of the raw value string). The missing,
    duplicate and unexpected counts are computed from the actual matching.
    """
    expected: dict[str, dict[str, Any]] = {}
    producer_duplicates: list[str] = []
    producer_anomalies: list[str] = []
    for record in producers:
        if record.get("type") != "put_ack":
            continue
        request_id = record.get("request_id")
        if not request_id:
            producer_anomalies.append("producer put_ack without request_id")
            continue
        if request_id in expected:
            producer_duplicates.append(request_id)
            continue
        expected[request_id] = record

    matched: list[str] = []
    duplicates: list[dict[str, Any]] = []
    unexpected: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    incomparable: list[dict[str, Any]] = []
    revisions: list[int] = []
    seen: set[str] = set()
    observed = 0

    def compare(request_id: str, field: str, actual: Any, wanted: Any,
                comparable: bool, reason: str) -> None:
        if not comparable:
            incomparable.append({"request_id": request_id, "field": field, "reason": reason})
            return
        if actual != wanted:
            mismatches.append({
                "request_id": request_id, "field": field,
                "watcher": actual, "producer": wanted,
            })

    for event in watcher_records:
        if event.get("type") != "watch_event":
            continue  # control records (watch_created, watch_canceled, ...) are not events
        observed += 1
        if event.get("event_type") != "PUT":
            unexpected.append({
                "reason": f"non-PUT event_type {event.get('event_type')!r} in PUT-only comparison",
                "key": event.get("key"),
                "event_revision": event.get("event_revision"),
            })
            continue
        if phase_prefix is not None and not str(event.get("key", "")).startswith(phase_prefix):
            unexpected.append({
                "reason": f"key outside phase prefix {phase_prefix!r}",
                "key": event.get("key"),
                "event_revision": event.get("event_revision"),
            })
            continue
        revision = _as_int(event.get("event_revision"))
        if revision is not None:
            revisions.append(revision)

        payload, payload_error = parse_event_payload(event)
        if payload is None:
            unexpected.append({
                "reason": payload_error,
                "key": event.get("key"),
                "event_revision": event.get("event_revision"),
            })
            continue
        request_id = payload.get("request_id")
        if not request_id:
            unexpected.append({
                "reason": "payload has no request_id",
                "key": event.get("key"),
                "event_revision": event.get("event_revision"),
            })
            continue
        producer = expected.get(request_id)
        if producer is None:
            unexpected.append({
                "reason": "no matching producer acknowledgement",
                "request_id": request_id,
                "key": event.get("key"),
                "event_revision": event.get("event_revision"),
            })
            continue
        if request_id in seen:
            duplicates.append({
                "request_id": request_id,
                "event_revision": event.get("event_revision"),
            })
            continue
        seen.add(request_id)
        matched.append(request_id)

        event_key = event.get("key")
        producer_key = producer.get("key")
        compare(request_id, "key", event_key, producer_key,
                event_key is not None and producer_key is not None,
                "watcher key or producer key missing")
        producer_revision = _as_int(producer.get("revision"))
        compare(request_id, "revision", revision, producer_revision,
                revision is not None and producer_revision is not None,
                "watcher event_revision or producer revision missing")
        compare(request_id, "sequence", payload.get("sequence"), producer.get("sequence"),
                payload.get("sequence") is not None and producer.get("sequence") is not None,
                "payload sequence or producer sequence missing")
        compare(request_id, "run_id", payload.get("run_id"), producer.get("run_id"),
                payload.get("run_id") is not None and producer.get("run_id") is not None,
                "payload run_id or producer run_id missing")
        producer_sha = producer.get("value_sha256")
        if event.get("value") is None or not producer_sha:
            incomparable.append({
                "request_id": request_id,
                "field": "value_sha256",
                "reason": "watcher value or producer value_sha256 missing",
            })
        else:
            digest = sha256_hex(event["value"])
            if digest != producer_sha:
                mismatches.append({
                    "request_id": request_id, "field": "value_sha256",
                    "watcher": digest, "producer": producer_sha,
                })

    missing_ids = sorted(set(expected) - set(seen))
    revisions_ordered = all(a <= b for a, b in zip(revisions, revisions[1:]))
    revisions_strict = all(a < b for a, b in zip(revisions, revisions[1:]))
    report: dict[str, Any] = {
        "requested": len(expected),
        "observed_watcher_events": observed,
        "matched": len(matched),
        "missing_ids": missing_ids,
        "missing_count": len(missing_ids),
        "duplicates": duplicates,
        "duplicate_count": len(duplicates),
        "unexpected": unexpected,
        "unexpected_count": len(unexpected),
        "mismatches": mismatches,
        "mismatch_count": len(mismatches),
        "incomparable": incomparable,
        "incomparable_count": len(incomparable),
        "producer_duplicate_count": len(producer_duplicates),
        "producer_anomalies": producer_anomalies,
        "revisions_ordered": revisions_ordered,
        "revisions_strictly_increasing": revisions_strict,
        "compared_fields": list(COMPARED_FIELDS),
    }
    report["ok"] = not (
        report["missing_count"] or report["duplicate_count"] or report["unexpected_count"]
        or report["mismatch_count"] or report["incomparable_count"]
        or report["producer_duplicate_count"] or report["producer_anomalies"]
        or not report["revisions_ordered"]
    )
    return report


def validate_reconnect_split(
    producers: list[dict[str, Any]],
    initial_records: list[dict[str, Any]],
    resumed_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate a reconnect scenario: event matching plus the L+1 boundary.

    The first event observed by the resumed Watch must be exactly
    (last revision fully observed by the initial Watch) + 1.
    """
    initial_events = [r for r in initial_records if r.get("type") == "watch_event"]
    resumed_events = [r for r in resumed_records if r.get("type") == "watch_event"]
    report = match_events(producers, initial_events + resumed_events)
    boundary: dict[str, Any] = {}
    anomalies: list[str] = []
    initial_revisions = [_as_int(r.get("event_revision")) for r in initial_events]
    initial_revisions = [v for v in initial_revisions if v is not None]
    resumed_revisions = [_as_int(r.get("event_revision")) for r in resumed_events]
    resumed_revisions = [v for v in resumed_revisions if v is not None]
    if initial_revisions:
        boundary["initial_last_revision"] = max(initial_revisions)
    if resumed_revisions:
        boundary["resumed_first_revision"] = min(resumed_revisions)
    if initial_revisions and resumed_revisions:
        expected_start = max(initial_revisions) + 1
        if min(resumed_revisions) != expected_start:
            anomalies.append(
                f"resumed Watch first revision {min(resumed_revisions)} != L+1 ({expected_start})")
    report["boundary"] = boundary
    report["boundary_anomalies"] = anomalies
    report["ok"] = bool(report["ok"]) and not anomalies
    return report


def validate_compaction_tail(
    producers: list[dict[str, Any]],
    recovery_records: list[dict[str, Any]],
    *,
    phase2_prefix: str,
    expected_snapshot_keys: int | None = None,
) -> dict[str, Any]:
    """Per-event validation of the compaction-recovery tail (phase 2 only).

    Checks the journal structure (one watch_canceled, one replacement snapshot,
    old and replacement watch_created), the snapshot boundary (snapshot revision
    follows compaction; replacement Watch starts at snapshot_revision + 1) and
    matches every phase-2 watcher event against producer acks by request_id,
    comparing key, revision, sequence, run_id and the SHA-256 of the raw value.
    Phase 1 history is represented by snapshot keys, not by events.
    """
    events_raw = [r for r in recovery_records if r.get("type") == "watch_event"]
    canceled = [r for r in recovery_records if r.get("type") == "watch_canceled"]
    snapshots = [r for r in recovery_records if r.get("type") == "snapshot"]
    created = [r for r in recovery_records if r.get("type") == "watch_created"]

    boundary: dict[str, Any] = {}
    anomalies: list[str] = []
    if len(canceled) != 1:
        anomalies.append(f"expected exactly one watch_canceled record, got {len(canceled)}")
    if len(snapshots) != 1:
        anomalies.append(f"expected exactly one snapshot record, got {len(snapshots)}")
    if len(created) < 2:
        anomalies.append(
            f"expected at least two watch_created records (old and replacement), got {len(created)}")

    snapshot_revision: int | None = None
    if len(snapshots) == 1:
        snapshot = snapshots[0]
        snapshot_revision = _as_int(snapshot.get("snapshot_revision"))
        key_count = _as_int(snapshot.get("key_count"))
        boundary["snapshot_revision"] = snapshot_revision
        boundary["snapshot_key_count"] = key_count
        if key_count is None or key_count < 0:
            anomalies.append(f"snapshot record has invalid key_count {snapshot.get('key_count')!r}")
        if expected_snapshot_keys is not None and key_count != expected_snapshot_keys:
            anomalies.append(
                f"snapshot key_count {key_count} != expected phase1 keys {expected_snapshot_keys}")
        if canceled:
            compact_revision = _as_int(canceled[0].get("compact_revision"))
            boundary["compact_revision"] = compact_revision
            if compact_revision is None or snapshot_revision is None or snapshot_revision < compact_revision:
                anomalies.append(
                    f"snapshot revision {snapshot_revision} does not follow compaction revision {compact_revision}")
            else:
                boundary["snapshot_at_compaction_revision"] = snapshot_revision == compact_revision
    if created and snapshot_revision is not None:
        start = _as_int(created[-1].get("start_revision"))
        boundary["new_watch_start_revision"] = start
        if start != snapshot_revision + 1:
            anomalies.append(
                f"replacement Watch start_revision {start} != snapshot_revision + 1 ({snapshot_revision + 1})")

    # Events at or before the snapshot revision cannot belong to the new Watch.
    eligible: list[dict[str, Any]] = []
    for event in events_raw:
        revision = _as_int(event.get("event_revision"))
        if snapshot_revision is not None and revision is not None and revision <= snapshot_revision:
            anomalies.append(
                f"watch event at revision {revision} at or before snapshot revision {snapshot_revision}")
            continue
        eligible.append(event)

    match = match_events(producers, eligible, phase_prefix=phase2_prefix)
    if eligible and not match["revisions_strictly_increasing"]:
        anomalies.append("phase2 watch event revisions are not strictly increasing")

    report = dict(match)
    report["boundary"] = boundary
    report["boundary_anomalies"] = anomalies
    report["ok"] = bool(report["ok"]) and not anomalies
    return report


def validate_mutation_sequence(
    records: list[dict[str, Any]],
    key: str,
    expected_types: tuple[str, ...] = ("PUT", "PUT", "DELETE"),
) -> dict[str, Any]:
    """Validate an event-type sequence on one key (same-key scenario).

    The keys are written with etcdctl, so no producer acks exist: request_id
    and SHA-256 comparisons are not possible here and are not claimed.
    """
    events = [r for r in records if r.get("type") == "watch_event" and r.get("key") == key]
    other = [r for r in records if r.get("type") == "watch_event" and r.get("key") != key]
    types = [r.get("event_type") for r in events]
    missing = 0
    unexpected = 0
    for index in range(max(len(expected_types), len(types))):
        expected_at = expected_types[index] if index < len(expected_types) else None
        observed_at = types[index] if index < len(types) else None
        if observed_at != expected_at:
            if expected_at is not None:
                missing += 1
            if observed_at is not None:
                unexpected += 1
    revisions = [_as_int(r.get("event_revision")) for r in events]
    revisions = [v for v in revisions if v is not None]
    strictly_increasing = all(a < b for a, b in zip(revisions, revisions[1:]))
    return {
        "key": key,
        "expected_types": list(expected_types),
        "observed_types": types,
        "requested": len(expected_types),
        "matched": len(expected_types) - missing,
        "missing_count": missing,
        "unexpected_count": unexpected + len(other),
        "extra_key_events": len(other),
        "revisions": revisions,
        "revisions_strictly_increasing": strictly_increasing,
        "validation_scope": (
            "event_type sequence and order only; keys are written with etcdctl, "
            "so no producer acks, request_ids or SHA-256 comparison are available"
        ),
        "ok": (
            missing == 0 and unexpected == 0 and len(other) == 0
            and strictly_increasing and len(types) == len(expected_types)
        ),
    }


def format_anomalies(report: dict[str, Any]) -> str:
    """Compact human-readable anomaly summary for error messages."""
    parts: list[str] = []
    if report.get("missing_count"):
        parts.append(f"missing={report['missing_count']} ids={report['missing_ids'][:5]}")
    if report.get("duplicate_count"):
        parts.append(f"duplicates={report['duplicate_count']} {report['duplicates'][:3]}")
    if report.get("unexpected_count"):
        parts.append(f"unexpected={report['unexpected_count']} {report['unexpected'][:3]}")
    if report.get("mismatch_count"):
        parts.append(f"mismatches={report['mismatch_count']} {report['mismatches'][:3]}")
    if report.get("incomparable_count"):
        parts.append(f"incomparable={report['incomparable_count']} {report['incomparable'][:3]}")
    if report.get("producer_duplicate_count"):
        parts.append(f"producer_duplicates={report['producer_duplicate_count']}")
    if report.get("producer_anomalies"):
        parts.append(f"producer_anomalies={report['producer_anomalies'][:3]}")
    if report.get("boundary_anomalies"):
        parts.append(f"boundary={report['boundary_anomalies'][:3]}")
    if report.get("revisions_ordered") is False:
        parts.append("revisions out of order")
    return "; ".join(parts) or "no anomaly details"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file; a truncated final line (interrupted run) is tolerated."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, raw in enumerate(lines, 1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            if number == len(lines):
                continue  # interrupted final write
            raise ValueError(f"{path}:{number}: invalid JSON: {exc}") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


def _phase2_prefix(producers: list[dict[str, Any]], recovery_records: list[dict[str, Any]]) -> str:
    for record in recovery_records:
        if record.get("type") == "snapshot" and record.get("prefix"):
            return str(record["prefix"]) + "phase2/"
    for record in producers:
        key = str(record.get("key", ""))
        if "/phase2/" in key:
            return key.split("/phase2/", 1)[0] + "/phase2/"
    raise ValueError("cannot determine the phase2 prefix from suite files")


def validate_suite(suite_dir: Path) -> dict[str, Any]:
    """Offline re-validation of one saved suite directory (no etcd access).

    Reconnect and compaction-tail are validated per event including SHA-256;
    the same-key scenario is validated as an event-type sequence (etcdctl
    writes carry no producer acks).
    """
    suite_id = suite_dir.name
    report: dict[str, Any] = {"suite_id": suite_id, "scenarios": {}}
    ok = True

    recon_producers = load_jsonl(suite_dir / "reconnect-producer-p1.jsonl") + \
        load_jsonl(suite_dir / "reconnect-producer-p2.jsonl")
    initial = load_jsonl(suite_dir / "reconnect-initial-watch.jsonl")
    resumed = load_jsonl(suite_dir / "reconnect-resumed-watch.jsonl")
    if recon_producers or initial or resumed:
        recon = validate_reconnect_split(recon_producers, initial, resumed)
        report["scenarios"]["reconnect"] = recon
        ok = ok and recon["ok"]

    cp2 = load_jsonl(suite_dir / "compaction-producer-p2.jsonl")
    rec_after = load_jsonl(suite_dir / "compaction-recover-after.jsonl")
    if cp2 or rec_after:
        tail = validate_compaction_tail(
            cp2, rec_after, phase2_prefix=_phase2_prefix(cp2, rec_after))
        report["scenarios"]["compaction_tail"] = tail
        ok = ok and tail["ok"]

    mutation_records = load_jsonl(suite_dir / "mutations-recover.jsonl")
    state_path = suite_dir / "mutations-state.json"
    if mutation_records:
        prefix = ""
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            prefix = str(state.get("prefix", ""))
        if prefix:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            mutation = validate_mutation_sequence(mutation_records, prefix + "shared-key")
            mutation["final_key_absent"] = (
                prefix + "shared-key" not in state.get("keys", {}))
            mutation["ok"] = bool(mutation["ok"] and mutation["final_key_absent"])
            if not mutation["final_key_absent"]:
                mutation["boundary_anomalies"] = ["deleted key remains in recovery state"]
            report["scenarios"]["same_key"] = mutation
            ok = ok and mutation["ok"]

    report["ok"] = ok
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline event-level validation of one saved WatchLab suite (no etcd access).")
    parser.add_argument("--suite", required=True, type=Path,
                        help="suite directory under results/automated/")
    args = parser.parse_args(argv)
    if not args.suite.is_dir():
        print(f"ERROR: suite directory not found: {args.suite}", file=sys.stderr)
        return 2
    try:
        report = validate_suite(args.suite)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
