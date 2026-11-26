#!/usr/bin/env python3
"""Unit tests for scripts/validation.py (event-level matching rules).

Covered cases: full match, missing event, duplicate event, revision mismatch,
key mismatch, SHA-256 mismatch, unexpected event without a producer ack,
incomparable fields (must not silently pass), ordering violations, phase
prefix violations, compaction-tail boundary checks and mutation sequences.
Run from the project root:
    python -m unittest discover -s scripts -p "test_*.py" -v
Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import unittest

import validation


def payload_text(rid: str, seq: int, run_id: str = "run-a", message: str = "watchlab test event") -> str:
    return json.dumps({
        "run_id": run_id, "sequence": seq, "request_id": rid,
        "created_at": "2026-01-01T00:00:00Z", "message": message,
    })


def producer(rid: str, seq: int, *, run_id: str = "run-a", key: str | None = None,
             revision: int | None = None, sha: str | None = None, drop_sha: bool = False):
    value = payload_text(rid, seq, run_id)
    record = {
        "type": "put_ack", "run_id": run_id, "sequence": seq, "request_id": rid,
        "key": key or f"/watchlab/test/{seq:06d}",
        "revision": revision if revision is not None else 100 + seq,
    }
    if not drop_sha:
        record["value_sha256"] = sha or hashlib.sha256(value.encode("utf-8")).hexdigest()
    return record


def watcher(rid: str, seq: int, *, run_id: str = "run-a", key: str | None = None,
            revision: int | None = None, value: str | None = None,
            event_type: str = "PUT", rev_override: int | None = None):
    return {
        "type": "watch_event", "run_id": "watch-a",
        "event_revision": rev_override if rev_override is not None else (
            revision if revision is not None else 100 + seq),
        "event_type": event_type,
        "key": key or f"/watchlab/test/{seq:06d}",
        "value": value if value is not None else payload_text(rid, seq, run_id),
    }


class TestMatchEvents(unittest.TestCase):
    def test_full_match_passes(self):
        producers = [producer(f"r-{i:03d}", i) for i in range(1, 4)]
        watchers = [watcher(f"r-{i:03d}", i) for i in range(1, 4)]
        report = validation.match_events(producers, watchers)
        self.assertTrue(report["ok"])
        self.assertEqual(report["requested"], 3)
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["missing_count"], 0)
        self.assertEqual(report["duplicate_count"], 0)
        self.assertEqual(report["unexpected_count"], 0)
        self.assertEqual(report["mismatch_count"], 0)
        self.assertEqual(report["incomparable_count"], 0)
        self.assertTrue(report["revisions_strictly_increasing"])

    def test_missing_event_detected(self):
        producers = [producer(f"r-{i:03d}", i) for i in range(1, 4)]
        watchers = [watcher("r-001", 1), watcher("r-003", 3)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_count"], 1)
        self.assertIn("r-002", report["missing_ids"])
        self.assertEqual(report["matched"], 2)

    def test_duplicate_event_detected(self):
        producers = [producer(f"r-{i:03d}", i) for i in range(1, 3)]
        watchers = [watcher("r-001", 1), watcher("r-002", 2), watcher("r-002", 2)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["duplicate_count"], 1)
        self.assertEqual(report["matched"], 2)
        self.assertEqual(report["missing_count"], 0)

    def test_revision_mismatch_detected(self):
        producers = [producer("r-001", 1, revision=101)]
        watchers = [watcher("r-001", 1, rev_override=999)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        fields = {m["field"] for m in report["mismatches"]}
        self.assertIn("revision", fields)

    def test_key_mismatch_detected(self):
        producers = [producer("r-001", 1)]
        watchers = [watcher("r-001", 1, key="/watchlab/other/000001")]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        fields = {m["field"] for m in report["mismatches"]}
        self.assertIn("key", fields)

    def test_sha256_mismatch_detected(self):
        producers = [producer("r-001", 1)]
        watchers = [watcher("r-001", 1, value=payload_text("r-001", 1, message="tampered"))]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        fields = {m["field"] for m in report["mismatches"]}
        self.assertIn("value_sha256", fields)

    def test_unexpected_event_without_producer(self):
        producers = [producer("r-001", 1)]
        watchers = [watcher("r-001", 1), watcher("r-099", 99)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unexpected_count"], 1)
        self.assertIn("no matching producer acknowledgement", report["unexpected"][0]["reason"])

    def test_incomparable_sha_is_not_a_pass(self):
        # A missing producer value_sha256 must be reported as incomparable,
        # never treated as an automatic pass.
        producers = [producer("r-001", 1, drop_sha=True)]
        watchers = [watcher("r-001", 1)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["incomparable_count"], 1)
        self.assertEqual(report["incomparable"][0]["field"], "value_sha256")
        self.assertEqual(report["mismatch_count"], 0)

    def test_revisions_out_of_order_detected(self):
        producers = [producer(f"r-{i:03d}", i) for i in range(1, 3)]
        watchers = [watcher("r-002", 2), watcher("r-001", 1)]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertFalse(report["revisions_ordered"])

    def test_phase_prefix_violation_is_unexpected(self):
        producers = [producer("r-001", 1, key="/watchlab/test/phase2/000001")]
        watchers = [
            watcher("r-001", 1, key="/watchlab/test/phase2/000001"),
            watcher("r-099", 1, key="/watchlab/test/phase1/000001"),
        ]
        report = validation.match_events(producers, watchers, phase_prefix="/watchlab/test/phase2/")
        self.assertFalse(report["ok"])
        self.assertEqual(report["matched"], 1)
        self.assertEqual(report["unexpected_count"], 1)
        self.assertIn("outside phase prefix", report["unexpected"][0]["reason"])

    def test_non_put_event_is_unexpected(self):
        producers = [producer("r-001", 1)]
        watchers = [watcher("r-001", 1, event_type="DELETE")]
        report = validation.match_events(producers, watchers)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unexpected_count"], 1)
        self.assertIn("non-PUT", report["unexpected"][0]["reason"])


def recovery_journal(events, *, start_old=101, compact_revision=110,
                     snapshot_revision=110, start_new=None, snapshot_key_count=2):
    if start_new is None:
        start_new = snapshot_revision + 1
    return [
        {"type": "watch_created", "run_id": "comp-b", "start_revision": start_old},
        {"type": "watch_canceled", "run_id": "comp-b", "compact_revision": compact_revision,
         "error": "etcdserver: mvcc: required revision has been compacted"},
        {"type": "snapshot", "run_id": "comp-b", "prefix": "/watchlab/test/",
         "snapshot_revision": snapshot_revision, "key_count": snapshot_key_count,
         "data_bytes": 500},
        {"type": "watch_created", "run_id": "comp-b", "start_revision": start_new},
    ] + events


class TestCompactionTail(unittest.TestCase):
    def prefix(self) -> str:
        return "/watchlab/test/phase2/"

    def test_tail_success(self):
        producers = [
            producer("r-001", 1, key=self.prefix() + "000001", revision=111),
            producer("r-002", 2, key=self.prefix() + "000002", revision=112),
        ]
        events = [watcher("r-001", 1, key=self.prefix() + "000001", revision=111),
                  watcher("r-002", 2, key=self.prefix() + "000002", revision=112)]
        report = validation.validate_compaction_tail(
            producers, recovery_journal(events), phase2_prefix=self.prefix(),
            expected_snapshot_keys=2)
        self.assertTrue(report["ok"], validation.format_anomalies(report))
        self.assertEqual(report["matched"], 2)
        self.assertEqual(report["missing_count"], 0)
        self.assertTrue(report["boundary"]["snapshot_at_compaction_revision"])
        self.assertEqual(report["boundary"]["new_watch_start_revision"], 111)
        self.assertEqual(report["boundary_anomalies"], [])

    def test_tail_wrong_start_revision_detected(self):
        producers = [producer("r-001", 1, key=self.prefix() + "000001", revision=111)]
        events = [watcher("r-001", 1, key=self.prefix() + "000001", revision=111)]
        report = validation.validate_compaction_tail(
            producers, recovery_journal(events, start_new=112),
            phase2_prefix=self.prefix(), expected_snapshot_keys=1)
        self.assertFalse(report["ok"])
        self.assertTrue(any("start_revision" in a for a in report["boundary_anomalies"]))

    def test_tail_phase1_event_contamination_detected(self):
        producers = [producer("r-001", 1, key=self.prefix() + "000001", revision=111)]
        events = [
            watcher("r-001", 1, key=self.prefix() + "000001", revision=111),
            watcher("r-099", 1, key="/watchlab/test/phase1/000001", revision=113),
        ]
        report = validation.validate_compaction_tail(
            producers, recovery_journal(events), phase2_prefix=self.prefix(),
            expected_snapshot_keys=1)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unexpected_count"], 1)

    def test_tail_missing_event_detected(self):
        producers = [
            producer("r-001", 1, key=self.prefix() + "000001", revision=111),
            producer("r-002", 2, key=self.prefix() + "000002", revision=112),
        ]
        events = [watcher("r-001", 1, key=self.prefix() + "000001", revision=111)]
        report = validation.validate_compaction_tail(
            producers, recovery_journal(events), phase2_prefix=self.prefix(),
            expected_snapshot_keys=2)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_count"], 1)
        self.assertIn("r-002", report["missing_ids"])

    def test_tail_event_at_or_before_snapshot_detected(self):
        producers = [producer("r-001", 1, key=self.prefix() + "000001", revision=111)]
        events = [
            watcher("r-099", 1, key=self.prefix() + "000099", revision=109),
            watcher("r-001", 1, key=self.prefix() + "000001", revision=111),
        ]
        report = validation.validate_compaction_tail(
            producers, recovery_journal(events), phase2_prefix=self.prefix(),
            expected_snapshot_keys=1)
        self.assertFalse(report["ok"])
        self.assertTrue(any("at or before snapshot" in a for a in report["boundary_anomalies"]))


class TestReconnectSplit(unittest.TestCase):
    def test_reconnect_match_and_boundary(self):
        producers = [producer("r-001", 1, revision=100), producer("r-002", 2, revision=101),
                     producer("r-003", 3, revision=102), producer("r-004", 4, revision=103)]
        initial = [watcher("r-001", 1, revision=100), watcher("r-002", 2, revision=101),
                   {"type": "watch_created", "run_id": "recon-a"}]
        resumed = [{"type": "watch_created", "run_id": "recon-b"},
                   watcher("r-003", 3, revision=102), watcher("r-004", 4, revision=103)]
        report = validation.validate_reconnect_split(producers, initial, resumed)
        self.assertTrue(report["ok"], validation.format_anomalies(report))
        self.assertEqual(report["matched"], 4)
        self.assertEqual(report["boundary"]["initial_last_revision"], 101)
        self.assertEqual(report["boundary"]["resumed_first_revision"], 102)

    def test_reconnect_wrong_resume_revision_detected(self):
        producers = [producer("r-001", 1, revision=100), producer("r-002", 2, revision=101),
                     producer("r-003", 3, revision=103)]
        initial = [watcher("r-001", 1, revision=100), watcher("r-002", 2, revision=101)]
        resumed = [watcher("r-003", 3, revision=103)]
        report = validation.validate_reconnect_split(producers, initial, resumed)
        self.assertFalse(report["ok"])
        self.assertTrue(any("L+1" in a for a in report["boundary_anomalies"]))


class TestMutationSequence(unittest.TestCase):
    KEY = "/watchlab/mut/shared-key"

    def records(self, types, revisions=None):
        revisions = revisions or list(range(1, len(types) + 1))
        return [{"type": "watch_event", "event_revision": rev, "event_type": t, "key": self.KEY}
                for t, rev in zip(types, revisions)]

    def test_mutation_sequence_success(self):
        report = validation.validate_mutation_sequence(self.records(["PUT", "PUT", "DELETE"]), self.KEY)
        self.assertTrue(report["ok"], validation.format_anomalies(report))
        self.assertEqual(report["matched"], 3)
        self.assertEqual(report["missing_count"], 0)
        self.assertEqual(report["unexpected_count"], 0)
        self.assertIn("no producer acks", report["validation_scope"])

    def test_mutation_sequence_missing_delete(self):
        report = validation.validate_mutation_sequence(self.records(["PUT", "PUT"]), self.KEY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["missing_count"], 1)

    def test_mutation_sequence_unexpected_extra(self):
        report = validation.validate_mutation_sequence(
            self.records(["PUT", "PUT", "DELETE", "PUT"]), self.KEY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["unexpected_count"], 1)

    def test_mutation_sequence_extra_key_events_detected(self):
        records = self.records(["PUT", "PUT", "DELETE"])
        records.append({"type": "watch_event", "event_revision": 9,
                        "event_type": "PUT", "key": "/watchlab/mut/other"})
        report = validation.validate_mutation_sequence(records, self.KEY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["extra_key_events"], 1)


if __name__ == "__main__":
    unittest.main()
