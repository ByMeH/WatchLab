#!/usr/bin/env python3
"""Aggregate completed WatchLab summary.json files into CSV/JSON and optional PNG charts."""
from __future__ import annotations
import argparse
import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

SCENARIOS = ("reconnect", "compaction_recovery", "same_key_put_put_delete")
DEFAULT_SUITES = {
    "auto-20261119-233338", "auto-20261119-233343", "auto-20261119-233348",
    "auto-20261119-233353", "auto-20261119-233415", "auto-20261119-233438",
    "auto-20261119-233500", "auto-20261119-233544", "auto-20261119-233628",
}
METRICS = (
    "observed_events", "missing_events", "duplicate_events", "unexpected_events", "field_mismatches",
    "snapshot_keys", "snapshot_bytes", "snapshot_range_ms", "client_recovery_interval_ms",
    "snapshot_to_watch_ms", "phase1_keys_in_snapshot", "final_keys", "last_revision", "requested_events",
)
# Metric renames with backward compatibility: historical summary.json files use
# the old names, new reports use the corrected names. The first name in each
# tuple wins when both are present.
METRIC_ALIASES: dict[str, tuple[str, ...]] = {
    "client_recovery_interval_ms": ("client_recovery_interval_ms", "recovery_ms"),
    "phase1_keys_in_snapshot": ("phase1_keys_in_snapshot", "phase1_events_represented_by_snapshot"),
}


def load_runs(root: Path, sizes: set[int], exclude: set[str], include: set[str] | None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    rows: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for suite_dir in sorted(root.glob("auto-*")):
        if not suite_dir.is_dir():
            continue
        suite = suite_dir.name
        path = suite_dir / "summary.json"
        if not path.exists():
            skipped.append({"suite_id": suite, "reason": "incomplete run: summary.json is missing"})
            continue
        if suite in exclude:
            skipped.append({"suite_id": suite, "reason": "explicitly excluded"})
            continue
        if include is not None and suite not in include:
            skipped.append({"suite_id": suite, "reason": "not in selected suite ID set"})
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            count = int(data["events_per_phase"])
            if count not in sizes:
                skipped.append({"suite_id": suite, "reason": f"events_per_phase={count} not selected"})
                continue
            result_map = {r["scenario"]: r for r in data["results"]}
            missing_scenarios = sorted(set(SCENARIOS) - set(result_map))
            if missing_scenarios:
                skipped.append({"suite_id": suite, "reason": "missing scenarios: " + ",".join(missing_scenarios)})
                continue
            for scenario in SCENARIOS:
                result = result_map[scenario]
                row: dict[str, Any] = {
                    "suite_id": suite,
                    "events_per_phase": count,
                    "interval": data.get("interval", ""),
                    "scenario": scenario,
                    "endpoint": data.get("endpoint", ""),
                }
                for metric in METRICS:
                    names = METRIC_ALIASES.get(metric, (metric,))
                    value = ""
                    for name in names:
                        if name in result and result[name] is not None and result[name] != "":
                            value = result[name]
                            break
                    row[metric] = value
                if int(result.get("missing_events", 0) or 0) != 0:
                    # Suites with computed nonzero missing_events are excluded
                    # from aggregation, never silently mixed in.
                    raise ValueError(f"{scenario}: missing_events={result.get('missing_events')}")
                rows.append(row)
        except Exception as exc:
            skipped.append({"suite_id": suite, "reason": f"invalid summary: {exc}"})
    return rows, skipped


def numeric(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def aggregate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((int(row["events_per_phase"]), str(row["scenario"])), []).append(row)
    out: list[dict[str, Any]] = []
    for (size, scenario), items in sorted(grouped.items()):
        for metric in METRICS:
            values = [v for v in (numeric(item.get(metric)) for item in items) if v is not None]
            if not values:
                continue
            out.append({
                "events_per_phase": size,
                "scenario": scenario,
                "metric": metric,
                "n": len(values),
                "mean": statistics.mean(values),
                "median": statistics.median(values),
                "stdev_sample": statistics.stdev(values) if len(values) >= 2 else "",
                "min": min(values),
                "max": max(values),
            })
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def make_charts(stats: list[dict[str, Any]], output: Path) -> str:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return "matplotlib unavailable; CSV/JSON created, charts skipped. Install matplotlib in project venv."

    def metric_points(name: str, scenario: str = "compaction_recovery"):
        return sorted(
            [r for r in stats if r["metric"] == name and r["scenario"] == scenario],
            key=lambda r: int(r["events_per_phase"]),
        )

    plots = [
        ("snapshot_range_ms", "Range snapshot duration", "snapshot duration (ms)", "snapshot_range.png"),
        ("client_recovery_interval_ms", "Client recovery interval (watch_canceled -> watch_created)",
         "client interval (ms), not an etcd property", "recovery_interval.png"),
        ("snapshot_bytes", "Snapshot payload size", "bytes (key + value UTF-8 lengths)", "snapshot_payload_size.png"),
    ]
    made = 0
    for metric, title, ylabel, filename in plots:
        points = metric_points(metric)
        if not points:
            continue
        x = [int(p["events_per_phase"]) for p in points]
        y = [float(p["mean"]) for p in points]
        err = [float(p["stdev_sample"]) if p["stdev_sample"] != "" else 0 for p in points]
        fig, ax = plt.subplots(figsize=(7, 4.5))
        ax.errorbar(x, y, yerr=err, marker="o", capsize=4)
        ax.set_title(title)
        ax.set_xlabel("Events per phase")
        ax.set_ylabel(ylabel)
        ax.grid(True, alpha=0.25)
        note = ("n=3 per size; single-node local etcd (127.0.0.1:2379); scenario compaction_recovery; "
                "error bars: sample SD (n-1); 9 fixed suite IDs, excluded: auto-20261119-230935; "
                "descriptive comparison of this testbed, not a benchmark")
        fig.text(0.5, 0.015, note, ha="center", fontsize=6.5, color="dimgray", wrap=True)
        fig.tight_layout()
        fig.savefig(output / filename, dpi=180)
        plt.close(fig)
        made += 1
    return f"created {made} charts" if made else "no chartable compaction metrics found"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="results/automated", help="directory containing auto-*/summary.json")
    parser.add_argument("--out", default="reports/analysis", help="output directory")
    parser.add_argument("--sizes", default="10,50,100", help="comma-separated events_per_phase values")
    parser.add_argument("--exclude", default="auto-20261119-230935", help="comma-separated suite IDs to exclude")
    parser.add_argument("--suite-ids", default=",".join(sorted(DEFAULT_SUITES)), help="exact comma-separated suite IDs; defaults to the nine verified runs")
    parser.add_argument("--all-matching", action="store_true", help="include every completed run matching --sizes instead of the default nine suite IDs")
    args = parser.parse_args()
    root = Path(args.root)
    output = Path(args.out)
    sizes = {int(x.strip()) for x in args.sizes.split(",") if x.strip()}
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    include = None if args.all_matching else {x.strip() for x in args.suite_ids.split(",") if x.strip()}
    if not root.is_dir():
        raise SystemExit(f"Results directory not found: {root}")
    output.mkdir(parents=True, exist_ok=True)
    rows, skipped = load_runs(root, sizes, exclude, include)
    stats = aggregate(rows)
    suites = sorted({row["suite_id"] for row in rows})
    expected_scenarios = len(SCENARIOS)
    if len(rows) != len(suites) * expected_scenarios:
        raise SystemExit("Internal validation failed: incomplete scenario rows")
    write_csv(output / "master_runs.csv", rows)
    write_csv(output / "group_statistics.csv", stats)
    write_csv(output / "excluded_runs.csv", skipped)
    report = {
        "source_root": str(root),
        "selected_sizes": sorted(sizes),
        "included_suite_ids": suites,
        "requested_suite_ids": sorted(include) if include is not None else "all matching sizes",
        "excluded_or_skipped": skipped,
        "run_count": len(suites),
        "scenario_rows": len(rows),
        "note": "Descriptive statistics from completed summaries. One local single-member etcd environment; do not generalize as a universal benchmark.",
        "metric_naming": {
            "client_recovery_interval_ms": "client-side interval from the watch_canceled record to the next watch_created record of this implementation; historical summaries store it as recovery_ms",
            "phase1_keys_in_snapshot": "number of current keys captured by the Range snapshot (phase 1 keys), not a count of historical events; historical summaries store it as phase1_events_represented_by_snapshot",
        },
    }
    (output / "analysis_manifest.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    chart_note = make_charts(stats, output)
    print(f"Included suites: {len(suites)}")
    print("Suite IDs:")
    for suite in suites:
        print(" ", suite)
    print(f"Run-scenario rows: {len(rows)}")
    print(f"Skipped/excluded directories: {len(skipped)}")
    print(f"Output: {output}")
    print(f"Charts: {chart_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
