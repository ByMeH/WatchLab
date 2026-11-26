#!/usr/bin/env python3
"""Unit tests for scripts/demo.py helper logic (no etcd required).

Covered cases: JSONL reading with a truncated final line, free-port
selection with reserved exclusions, busy-port detection, binary staleness
detection, Go duration parsing, and final-state verification against
producer acknowledgements.
Run from the project root:
    python -m unittest discover -s scripts -p "test_*.py" -v
Only the Python standard library is used.
"""
from __future__ import annotations

import hashlib
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

import demo


class TestReadJsonl(unittest.TestCase):
    def test_valid_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                json.dumps({"type": "put_ack", "sequence": 1}) + "\n" +
                json.dumps({"type": "put_ack", "sequence": 2}) + "\n",
                encoding="utf-8",
            )
            records = demo.read_jsonl(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0]["sequence"], 1)

    def test_truncated_final_line_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                json.dumps({"type": "watch_event"}) + "\n" + '{"type": "wat',
                encoding="utf-8",
            )
            records = demo.read_jsonl(path)
            self.assertEqual(len(records), 1)

    def test_broken_middle_line_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "records.jsonl"
            path.write_text(
                '{"type": "a"}' + "\n" + "not json" + "\n" + '{"type": "b"}' + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(demo.DemoError):
                demo.read_jsonl(path)

    def test_missing_file_is_empty(self):
        records = demo.read_jsonl(Path("/nonexistent") / "nope.jsonl")
        self.assertEqual(records, [])


class TestPorts(unittest.TestCase):
    def test_find_free_port_avoids_reserved(self):
        port = demo.find_free_port(set(demo.RESERVED_PORTS))
        self.assertIsInstance(port, int)
        self.assertNotIn(port, demo.RESERVED_PORTS)

    def test_find_free_port_avoids_explicitly_busy(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            port = demo.find_free_port({busy})
            self.assertNotEqual(port, busy)
        finally:
            blocker.close()

    def test_assert_port_free_passes_on_free_port(self):
        port = demo.find_free_port(set())
        demo.assert_port_free(port)  # must not raise

    def test_assert_port_free_detects_busy_port(self):
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy = blocker.getsockname()[1]
        try:
            with self.assertRaises(demo.DemoError) as ctx:
                demo.assert_port_free(busy)
            self.assertIn("busy", str(ctx.exception))
        finally:
            blocker.close()


class TestBinaryStaleness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "bin").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def touch(self, relative: str):
        path = self.root / relative
        path.write_text("x", encoding="utf-8")
        # Ensure a strictly newer mtime than the binary regardless of fs precision.
        stamp = time.time() + 10
        import os
        os.utime(path, (stamp, stamp))

    def test_missing_binary(self):
        self.touch("main.go")
        self.assertIn("missing", demo.binary_staleness(self.root))

    def test_up_to_date_binary(self):
        binary = self.root / "bin" / "watchlab"
        binary.write_text("binary", encoding="utf-8")
        for source in ("main.go", "recover.go", "go.mod"):
            path = self.root / source
            path.write_text("src", encoding="utf-8")
            import os
            os.utime(path, (1, 1))  # older than the binary
        self.assertIsNone(demo.binary_staleness(self.root))

    def test_newer_source_detected(self):
        binary = self.root / "bin" / "watchlab"
        binary.write_text("binary", encoding="utf-8")
        self.touch("main.go")
        self.touch("recover.go")
        reason = demo.binary_staleness(self.root)
        self.assertIn("main.go", reason)
        self.assertIn("recover.go", reason)


class TestParseGoDuration(unittest.TestCase):
    def test_known_units(self):
        # parse_go_duration enforces the demo interval bounds (0..5s),
        # so only sub-5s units are valid inputs here.
        self.assertAlmostEqual(demo.parse_go_duration("100ms"), 0.1)
        self.assertAlmostEqual(demo.parse_go_duration("2s"), 2.0)

    def test_invalid_rejected(self):
        for bad in ("100", "ms", "1x", ""):
            with self.assertRaises(demo.DemoError):
                demo.parse_go_duration(bad)

    def test_out_of_range_rejected(self):
        for slow in ("10s", "1m"):
            with self.assertRaises(demo.DemoError):
                demo.parse_go_duration(slow)


class TestVerifyStateMatches(unittest.TestCase):
    @staticmethod
    def ack(key: str, value: str, revision: int) -> dict:
        return {
            "key": key,
            "revision": revision,
            "value_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
        }

    def test_consistent_state_passes(self):
        acks = [self.ack("/k/1", "v1", 5), self.ack("/k/2", "v2", 6)]
        state = {"keys": {
            "/k/1": {"value": "v1", "mod_revision": 5},
            "/k/2": {"value": "v2", "mod_revision": 6},
        }}
        report = demo.verify_state_matches(state, acks)
        self.assertTrue(report["ok"], report["problems"])
        self.assertEqual(report["checked_keys"], 2)

    def test_tampered_value_detected(self):
        acks = [self.ack("/k/1", "v1", 5)]
        state = {"keys": {"/k/1": {"value": "tampered", "mod_revision": 5}}}
        report = demo.verify_state_matches(state, acks)
        self.assertFalse(report["ok"])
        self.assertTrue(any("SHA-256" in p for p in report["problems"]))

    def test_missing_key_detected(self):
        acks = [self.ack("/k/1", "v1", 5)]
        report = demo.verify_state_matches({"keys": {}}, acks)
        self.assertFalse(report["ok"])
        self.assertTrue(any("missing" in p for p in report["problems"]))

    def test_mod_revision_mismatch_detected(self):
        acks = [self.ack("/k/1", "v1", 5)]
        state = {"keys": {"/k/1": {"value": "v1", "mod_revision": 4}}}
        report = demo.verify_state_matches(state, acks)
        self.assertFalse(report["ok"])
        self.assertTrue(any("mod_revision" in p for p in report["problems"]))

    def test_extra_key_detected(self):
        acks = [self.ack("/k/1", "v1", 5)]
        state = {"keys": {
            "/k/1": {"value": "v1", "mod_revision": 5},
            "/k/9": {"value": "ghost", "mod_revision": 9},
        }}
        report = demo.verify_state_matches(state, acks)
        self.assertFalse(report["ok"])
        self.assertTrue(any("unexpected keys" in p for p in report["problems"]))


class TestRequire(unittest.TestCase):
    def test_require_raises_with_message(self):
        with self.assertRaises(demo.DemoError) as ctx:
            demo.require(False, "boom")
        self.assertEqual(str(ctx.exception), "boom")

    def test_require_passes_silently(self):
        demo.require(True, "never")


if __name__ == "__main__":
    unittest.main()
