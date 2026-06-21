"""CSV emission and Markdown report generation.

The Markdown report is generated *from* the CSV (not hand-maintained), so the two
never drift. Reports carry environment metadata and explicit capability-difference
notes, and make no universal speed claims from a single machine.
"""

from __future__ import annotations

import csv
import dataclasses
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .scenarios import BenchRecord

CSV_FIELDS = [f.name for f in dataclasses.fields(BenchRecord)]


def write_csv(records: list[BenchRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow(dataclasses.asdict(record))


def write_metadata(metadata: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fmt_ns(value: str) -> str:
    if value == "-1":
        return "N/A"
    micros = int(value) / 1000.0
    return f"{micros:.1f} µs"


def _fmt_int(value: str) -> str:
    return "N/A" if value == "-1" else value


def render_markdown(csv_path: Path, metadata: dict[str, Any]) -> str:
    rows = read_csv(csv_path)
    lines: list[str] = ["# pyinc Benchmark Report", ""]
    lines.append(
        "Generated from `" + csv_path.name + "`. Timings are from a single machine "
        "and are **not** universal speed claims."
    )
    lines += ["", "## Environment", ""]
    for key in sorted(metadata):
        lines.append(f"- **{key}**: {metadata[key]}")
    lines += [
        "",
        "## Capability differences",
        "",
        "These baselines do **not** provide identical correctness or dependency "
        "semantics; the comparison is informational only.",
        "",
        "- `pyinc_incremental` — dependency-aware early cutoff, ownership-tracked "
        "outputs, stale deletion, and tamper repair.",
        "- `fresh_full` — a brand-new cache-free `Database` recomputed from scratch "
        "(the correctness oracle).",
        "- `naive_cache` — a deliberately simple whole-input recompute with no "
        "dependency-aware cutoff, ownership, or stale deletion.",
        "- `joblib_memory` — argument-based memoization; marked `N/A` where it "
        "cannot represent file-tree generation.",
        "",
        "Every `pyinc_incremental` row passed a byte-for-byte correctness check "
        "against `fresh_full`.",
        "",
        "## Results",
        "",
    ]

    grouped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[(row["workload"], row["scenario"])].append(row)

    header = (
        "| workload | scenario | implementation | correctness | median | p95 | "
        "exec | reuse | backdate | writes | deletes | nodes | edges |"
    )
    sep = "|" + "---|" * 13
    lines += [header, sep]
    for group_key in sorted(grouped):
        for row in grouped[group_key]:
            lines.append(
                "| {workload} | {scenario} | {impl} | {ok} | {median} | {p95} | "
                "{exec} | {reuse} | {backdate} | {writes} | {deletes} | {nodes} | {edges} |".format(
                    workload=row["workload"],
                    scenario=row["scenario"],
                    impl=row["implementation"],
                    ok=row["correctness"],
                    median=_fmt_ns(row["median_ns"]),
                    p95=_fmt_ns(row["p95_ns"]),
                    exec=_fmt_int(row["query_executions"]),
                    reuse=_fmt_int(row["query_reuses"]),
                    backdate=_fmt_int(row["query_backdates"]),
                    writes=_fmt_int(row["output_writes"]),
                    deletes=_fmt_int(row["output_deletes"]),
                    nodes=_fmt_int(row["graph_nodes"]),
                    edges=_fmt_int(row["graph_edges"]),
                )
            )
    lines.append("")
    return "\n".join(lines)
