#!/usr/bin/env python3
"""Unit tests for the WatchLab Desktop GUI service layer (no display needed).

Covered cases: summary.json reading and validation, suite completeness
detection, JSONL event loading with normalization, event filtering, corrupted
file handling, demo summary discovery, analytics loaders, binary staleness
detection, recovery state.json parsing (snapshot/checkpoint/mod revisions,
filtering, missing/corrupted/empty files), and the managed ProcessRunner
(start/stream/stop/exit code).
Run from the project root:
    python -m unittest scripts.test_gui_services -v
Only the Python standard library is used; tkinter is not imported.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))

from gui import services as S  # noqa: E402
from gui import process as P  # noqa: E402


def _valid_summary(scenarios: list[str] | None = None) -> dict:
    names = scenarios if scenarios is not None else list(S.SCENARIOS)
    return {
        "events_per_phase": 10,
        "results": [
            {"scenario": name, "observed_events": 10, "missing_events": 0}
            for name in names
        ],
    }


class TestSuiteSummary(unittest.TestCase):
    def test_load_valid_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auto-x").mkdir()
            (root / "auto-x" / "summary.json").write_text(
                json.dumps(_valid_summary()), encoding="utf-8")
            data = S.load_suite_summary("auto-x", root)
            self.assertEqual(data["events_per_phase"], 10)
            self.assertEqual(len(data["results"]), len(S.SCENARIOS))

    def test_missing_summary_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                S.load_suite_summary("auto-x", Path(tmp))

    def test_corrupted_summary_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auto-x").mkdir()
            (root / "auto-x" / "summary.json").write_text(
                '{"results": [', encoding="utf-8")
            with self.assertRaises(ValueError):
                S.load_suite_summary("auto-x", root)

    def test_unexpected_structure_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auto-x").mkdir()
            (root / "auto-x" / "summary.json").write_text(
                json.dumps({"no_results": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                S.load_suite_summary("auto-x", root)

    def test_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auto-ok").mkdir()
            (root / "auto-ok" / "summary.json").write_text(
                json.dumps(_valid_summary()), encoding="utf-8")
            (root / "auto-bad").mkdir()
            (root / "auto-bad" / "summary.json").write_text(
                json.dumps(_valid_summary(["reconnect"])), encoding="utf-8")
            ok, _ = S.suite_is_complete("auto-ok", root)
            bad, reason = S.suite_is_complete("auto-bad", root)
            self.assertTrue(ok)
            self.assertFalse(bad)
            self.assertIn("missing scenarios", reason)
            self.assertEqual(S.completed_series(root), ["auto-ok"])
            self.assertEqual([s for s, _ in S.excluded_series(root)], ["auto-bad"])

    def test_missing_summary_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "auto-20261119-230935").mkdir()  # incomplete suite
            self.assertEqual([s for s, _ in S.excluded_series(root)],
                             ["auto-20261119-230935"])
            self.assertEqual(S.completed_series(root), [])


class TestEventLoading(unittest.TestCase):
    def _make_suite(self, root: Path) -> None:
        suite = root / "auto-test"
        suite.mkdir(parents=True)
        producer = [
            {"type": "put_ack", "sequence": 1, "revision": 2, "key": "phase1/k1",
             "run_id": "run-1", "acknowledged_at": "2026-11-19T23:33:38Z"},
            {"type": "put_ack", "sequence": 2, "revision": 3, "key": "phase1/k2",
             "run_id": "run-1", "acknowledged_at": "2026-11-19T23:33:38Z"},
        ]
        watch = [
            {"type": "watch_event", "event_type": "PUT", "event_revision": 2,
             "key": "phase1/k1", "run_id": "run-1", "received_at": "2026-11-19T23:33:38Z",
             "value": json.dumps({"sequence": 1, "request_id": "req-1"})},
            {"type": "watch_event", "event_type": "DELETE", "event_revision": 7,
             "key": "phase1/k1", "run_id": "run-2", "received_at": "2026-11-19T23:33:39Z"},
        ]
        recover = [
            {"type": "watch_event", "event_type": "PUT", "event_revision": 9,
             "key": "phase3/k1", "run_id": "run-2", "timestamp": "2026-11-19T23:33:40Z"},
            {"type": "snapshot_record", "snapshot_revision": 8,
             "timestamp": "2026-11-19T23:33:40Z"},
        ]
        names = {
            "reconnect-producer-p1.jsonl": producer,
            "reconnect-initial-watch.jsonl": watch,
            "compaction-recover-after.jsonl": recover,
        }
        for name, records in names.items():
            text = "".join(json.dumps(r) + "\n" for r in records)
            (suite / name).write_text(text, encoding="utf-8")

    def test_producer_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_suite(root)
            records = list(S.iter_events(root, "auto-test", "producer"))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[0].type, "put_ack")
            self.assertEqual(records[0].sequence, "1")
            self.assertEqual(records[0].revision, "2")
            self.assertEqual(records[0].timestamp, "2026-11-19T23:33:38Z")

    def test_watch_type_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_suite(root)
            records = list(S.iter_events(root, "auto-test", "watch"))
            self.assertEqual([r.type for r in records], ["PUT", "DELETE"])
            self.assertEqual(records[0].sequence, "1")
            self.assertEqual(records[0].request_id, "req-1")

    def test_recover_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_suite(root)
            records = list(S.iter_events(root, "auto-test", "recover"))
            self.assertEqual(len(records), 2)
            self.assertEqual(records[1].type, "snapshot_record")
            self.assertEqual(records[1].revision, "8")

    def test_filtering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_suite(root)
            only_delete = S.EventFilter(event_type="DELETE")
            got = list(S.iter_events(root, "auto-test", "watch", only_delete))
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].revision, "7")

            by_rev = S.EventFilter(revision="2")
            self.assertEqual(len(list(S.iter_events(root, "auto-test", "watch", by_rev))), 1)

            by_key = S.EventFilter(key_substring="K1")
            self.assertEqual(len(list(S.iter_events(root, "auto-test", "watch", by_key))), 2)

            by_run = S.EventFilter(run_id="RUN-2")
            self.assertEqual(len(list(S.iter_events(root, "auto-test", "watch", by_run))), 1)

            none = S.EventFilter(revision="999")
            self.assertEqual(list(S.iter_events(root, "auto-test", "watch", none)), [])

    def test_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_suite(root)
            page, total, pages = S.load_events_page(root, "auto-test", "watch",
                                                    page=0, page_size=1)
            self.assertEqual(total, 2)
            self.assertEqual(pages, 2)
            self.assertEqual(len(page), 1)
            page2, total2, pages2 = S.load_events_page(root, "auto-test", "watch",
                                                       page=1, page_size=1)
            self.assertEqual(total2, 2)
            self.assertEqual(pages2, 2)
            self.assertEqual(len(page2), 1)

    def test_truncated_lines_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = root / "auto-test"
            suite.mkdir()
            path = suite / "reconnect-producer-p1.jsonl"
            path.write_text(
                json.dumps({"type": "put_ack", "sequence": 1, "revision": 2}) + "\n" +
                '{"type": "put_ack", "seq',  # truncated final write
                encoding="utf-8")
            records = list(S.iter_events(root, "auto-test", "producer"))
            self.assertEqual(len(records), 1)

    def test_corrupted_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            suite = root / "auto-test"
            suite.mkdir()
            (suite / "reconnect-producer-p1.jsonl").write_text(
                "not json at all\n", encoding="utf-8")
            self.assertEqual(list(S.iter_events(root, "auto-test", "producer")), [])

    def test_missing_suite_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(list(S.iter_events(root, "auto-nope", "producer")), [])


class TestDemoSummary(unittest.TestCase):
    def _write_demo(self, tmp: Path, name: str, overall: str) -> Path:
        directory = tmp / name
        directory.mkdir()
        path = directory / "demo-summary.json"
        path.write_text(json.dumps({
            "demo_id": name, "started_at": "2026-11-21T10:00:00Z",
            "overall": overall, "total_verified_events": 27,
            "scenarios": [{"scenario": "reconnect", "status": "pass"}],
        }), encoding="utf-8")
        return path

    def test_latest_summary_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._write_demo(tmp, "watchlab-demo-a", "pass")
            time.sleep(0.02)
            self._write_demo(tmp, "watchlab-demo-b", "pass")
            summary = S.last_demo_summary(tmp)
            self.assertIsNotNone(summary)
            self.assertEqual(summary.demo_id, "watchlab-demo-b")
            self.assertEqual(summary.verified_events, 27)
            self.assertEqual(summary.status_word(), "pass")

    def test_no_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(S.last_demo_summary(Path(tmp)))

    def test_corrupted_summary_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            directory = tmp / "watchlab-demo-x"
            directory.mkdir()
            (directory / "demo-summary.json").write_text("{broken", encoding="utf-8")
            self.assertIsNone(S.last_demo_summary(tmp))


class TestAnalyticsLoaders(unittest.TestCase):
    def test_group_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "group_statistics.csv").write_text(
                "metric,suite_id,sample_size,mean\n"
                "snapshot_range_ms,auto-1,3,1.690\n",
                encoding="utf-8")
            rows = S.load_group_stats(root)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["metric"], "snapshot_range_ms")

    def test_missing_or_corrupted_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(S.load_group_stats(root), [])
            (root / "group_statistics.csv").write_bytes(
                b"metric,suite_id\n\xff\xfe bad utf8\n")  # invalid UTF-8
            self.assertEqual(S.load_group_stats(root), [])

    def test_charts_and_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(S.analysis_charts(root), [])
            (root / "snapshot_range_ms.png").write_bytes(b"\x89PNG")
            (root / "notes.txt").write_text("x", encoding="utf-8")
            charts = S.analysis_charts(root)
            self.assertEqual([c.name for c in charts], ["snapshot_range_ms.png"])
            (root / "README.md").write_text("# Analysis\n", encoding="utf-8")
            self.assertIn("# Analysis", S.read_analysis_readme(root))


class TestProjectState(unittest.TestCase):
    """Checks against the real project (read-only; presence assertions only)."""

    def test_real_project_state(self):
        completed = S.completed_series()
        self.assertGreaterEqual(len(completed), 9)
        for suite_id in S.SELECTED_SUITES:
            self.assertIn(suite_id, completed,
                          f"selected suite {suite_id} is not complete")
        excluded = dict(S.excluded_series())
        self.assertIn("auto-20261119-230935", excluded)
        rows = S.build_suite_rows()
        statuses = {row.status for row in rows}
        self.assertIn("ok", statuses)
        self.assertIn("excluded (incomplete)", statuses)
        self.assertEqual(S.selected_series(), 9)
        validation = S.verified_events()
        self.assertIsNotNone(validation)
        self.assertGreater(validation, 0)

    def test_binary_status_and_environment(self):
        status, path, _detail = S.binary_status()
        self.assertIn(status, ("ok", "stale", "missing", "go unavailable"))
        env = S.check_environment()
        self.assertTrue(env.python_version)
        self.assertEqual(env.binary_path, path)
        self.assertTrue(env.demo_script.endswith("demo.py"))

    def test_real_analysis_files(self):
        self.assertTrue(S.load_group_stats(), "group_statistics.csv should exist")
        charts = S.analysis_charts()
        self.assertTrue(charts, "PNG charts should exist in reports/analysis")
        self.assertIn("included", S.analysis_manifest_summary())


class TestBinaryStaleness(unittest.TestCase):
    def test_stale_binary_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bin").mkdir()
            binary = root / "bin" / "watchlab"
            binary.write_text("fake", encoding="utf-8")
            old = time.time() - 3600
            for name in ("main.go", "recover.go", "go.mod"):
                (root / name).write_text("// source", encoding="utf-8")
                import os
                os.utime(root / name, (old, old))
            status, _, _ = S.binary_status(root)
            self.assertEqual(status, "ok" if time.time() - old > 1 else "stale")

    def test_missing_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            status, path, _ = S.binary_status(Path(tmp))
            self.assertEqual(status, "missing")
            self.assertTrue(path.endswith("watchlab"))


class TestProcessRunner(unittest.TestCase):
    def test_stream_and_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            runner = P.ProcessRunner("echo-test", [sys.executable, "-c",
                                     "print('line1'); print('line2')"], log_dir)
            runner.start()
            runner.wait(10)
            self.assertEqual(runner.exit_code, 0)
            lines = runner.read_log(10)
            self.assertEqual([line for _, line in lines], ["line1", "line2"])
            self.assertTrue(runner.log_path.exists())

    def test_exit_event_carries_code_in_ts_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = P.ProcessRunner("exit-contract", [sys.executable, "-c", "pass"],
                                     Path(tmp))
            runner.start()
            runner.wait(10)
            events = runner.drain()
            self.assertIn(("exit", 0, ""), events)

    def test_stop_running_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = P.ProcessRunner("sleeper", [sys.executable, "-c",
                                     "import time; time.sleep(60)"], Path(tmp))
            runner.start()
            runner.stop()
            runner.wait(10)
            self.assertIsNotNone(runner.exit_code)
            self.assertNotEqual(runner.exit_code, 0)

    def test_missing_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = P.ProcessRunner("nope", ["/no/such/binary"], Path(tmp))
            runner.start()
            runner.wait(10)
            self.assertIsNotNone(runner.exit_code)
            self.assertNotEqual(runner.exit_code, 0)
            self.assertTrue(runner.log_path.exists())

    def test_format_helpers(self):
        self.assertEqual(P.format_duration(100.0, 105.5), "5.5s")
        self.assertEqual(P.format_duration(None, None), "-")
        import re as _re
        self.assertRegex(P.format_ts(0.0), r"^\d{2}:\d{2}:\d{2}$")


class TestRecoveryState(unittest.TestCase):
    """Recovery State Inspector service layer: state.json parsing/filtering."""

    @staticmethod
    def _payload() -> dict:
        prefix = "/watchlab/demo/x/compaction/events/"
        # Mirrors the real recover.go recoveryState shape: fresh snapshot at
        # R=21 covers phases 1+2, then phase 3 (revisions 22..26) was applied
        # by Watch events, so L=26 and three keys have mod_revision > R.
        return {
            "schema_version": 1,
            "prefix": prefix,
            "snapshot_revision": 21,
            "last_revision": 26,
            "updated_at": "2026-11-21T12:16:19.739684494Z",
            "snapshot_duration_ms": 3.9,
            "data_bytes": 1000,
            "keys": {
                prefix + "phase1/000001": {"value": "{\"sequence\":1}",
                                           "create_revision": 12,
                                           "mod_revision": 12, "version": 1},
                prefix + "phase1/000002": {"value": "no-mod-rev"},
                prefix + "phase2/000001": {"value": "plain", "create_revision": 20,
                                           "mod_revision": 20, "version": 1},
                prefix + "phase3/000001": {"value": "tail-1", "create_revision": 22,
                                           "mod_revision": 22, "version": 1},
                prefix + "phase3/000002": {"value": "tail-2", "create_revision": 23,
                                           "mod_revision": 23, "version": 1},
                prefix + "phase3/000003": {"value": "tail-3", "create_revision": 26,
                                           "mod_revision": 26, "version": 1},
            },
        }

    def _write(self, tmp: Path, payload) -> Path:
        path = tmp / "state.json"
        if isinstance(payload, str):
            path.write_text(payload, encoding="utf-8")
        else:
            path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_load_valid_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), self._payload())
            state = S.load_recovery_state(path)
            self.assertEqual(state.schema_version, 1)
            self.assertEqual(state.prefix, "/watchlab/demo/x/compaction/events/")
            self.assertEqual(state.snapshot_revision, 21)
            self.assertEqual(state.last_revision, 26)
            self.assertEqual(state.data_bytes, 1000)
            self.assertEqual(state.key_count, 6)
            self.assertGreater(state.file_size_bytes, 0)
            self.assertEqual([kv.key for kv in state.keys],
                             sorted(kv.key for kv in state.keys))
            self.assertTrue(state.keys[0].key.endswith("phase1/000001"))
            self.assertEqual(state.keys[0].value, "{\"sequence\":1}")

    def test_snapshot_and_checkpoint_split(self):
        # 2: snapshot_revision (R) and last_revision (L) stay separate values
        #    and the L-R delta / changed-after-snapshot count are derived.
        with tempfile.TemporaryDirectory() as tmp:
            state = S.load_recovery_state(self._write(Path(tmp), self._payload()))
            self.assertEqual(state.snapshot_revision, 21)
            self.assertEqual(state.last_revision, 26)
            self.assertIsNot(state.snapshot_revision, state.last_revision)
            self.assertEqual(state.checkpoint_delta, 5)
            changed = state.changed_after_snapshot
            self.assertEqual(len(changed), 3)
            self.assertTrue(all(kv.mod_revision > state.snapshot_revision
                                for kv in changed))

    def test_mod_revision_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = S.load_recovery_state(self._write(Path(tmp), self._payload()))
            by_rel = {kv.key[len(state.prefix):]: kv for kv in state.keys}
            self.assertEqual(by_rel["phase3/000003"].mod_revision, 26)
            self.assertEqual(by_rel["phase1/000001"].mod_revision, 12)
            # 3: omitempty field absent in JSON -> None, not 0
            self.assertIsNone(by_rel["phase1/000002"].mod_revision)
            self.assertIsNone(by_rel["phase1/000002"].create_revision)

    def test_corrupted_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), '{"snapshot_revision": 21, "keys": {')
            with self.assertRaises(S.RecoveryStateError) as ctx:
                S.load_recovery_state(path)
            self.assertEqual(ctx.exception.reason, "corrupted")

    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.RecoveryStateError) as ctx:
                S.load_recovery_state(Path(tmp) / "absent" / "state.json")
            self.assertEqual(ctx.exception.reason, "missing")
        with self.assertRaises(S.RecoveryStateError):
            S.load_recovery_state("/tmp/watchlab-does-not-exist/state.json")

    def test_empty_state(self):
        payload = self._payload()
        payload["keys"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            state = S.load_recovery_state(self._write(Path(tmp), payload))
            self.assertEqual(state.key_count, 0)
            self.assertEqual(state.changed_after_snapshot, ())
            self.assertEqual(S.filter_state_keys(state), ())
            self.assertTrue(all(cell == 0 or cell is None for cell in
                                (len(state.keys),)))
        # 6: valid JSON but wrong structure must not crash the Inspector
        payload2 = {"keys": "not-an-object"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(S.RecoveryStateError) as ctx:
                S.load_recovery_state(self._write(Path(tmp), payload2))
            self.assertEqual(ctx.exception.reason, "unexpected")

    def test_filter_state_keys(self):
        # 7: case-insensitive substring + revision scope vs snapshot R
        with tempfile.TemporaryDirectory() as tmp:
            state = S.load_recovery_state(self._write(Path(tmp), self._payload()))
            self.assertEqual(len(S.filter_state_keys(state)), 6)
            self.assertEqual(len(S.filter_state_keys(state, "phase3")), 3)
            self.assertEqual(len(S.filter_state_keys(state, "PHASE1")), 2)
            self.assertEqual(len(S.filter_state_keys(state, "000003")), 1)
            changed = S.filter_state_keys(state, scope=S.SCOPE_CHANGED_AFTER_SNAPSHOT)
            self.assertEqual(len(changed), 3)
            snapshot = S.filter_state_keys(state, scope=S.SCOPE_FROM_SNAPSHOT)
            self.assertEqual(len(snapshot), 3)
            both = S.filter_state_keys(state, "000002", S.SCOPE_CHANGED_AFTER_SNAPSHOT)
            self.assertEqual([kv.key.rsplit("/", 1)[-1] for kv in both], ["000002"])
            self.assertEqual(S.filter_state_keys(state, "absent"), ())

    def test_state_path_discovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self.assertIsNone(S.latest_recovery_state_path(tmp))
            first = tmp / "watchlab-demo-a" / "compaction" / "state.json"
            first.parent.mkdir(parents=True)
            first.write_text("{}", encoding="utf-8")
            time.sleep(0.02)
            second = tmp / "watchlab-demo-b" / "compaction" / "state.json"
            second.parent.mkdir(parents=True)
            second.write_text("{}", encoding="utf-8")
            self.assertEqual(S.latest_recovery_state_path(tmp), second)
            # a demo-summary with paths.state_file is honoured
            summary = tmp / "watchlab-demo-a" / "demo-summary.json"
            summary.write_text(json.dumps(
                {"paths": {"state_file": str(first)}}), encoding="utf-8")
            self.assertEqual(S.demo_state_path(summary), str(first))
            self.assertIsNone(S.demo_state_path(tmp / "absent.json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
