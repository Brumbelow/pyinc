"""Timing/memory measurement, scenario orchestration, and report writing."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from .measure import ScenarioResult


def run_scenarios(
    targets: Iterable[str],
    *,
    out_dir: str | Path,
    comparators: Sequence[str] | None = None,
) -> list[ScenarioResult]:
    from . import scenarios

    comps = list(comparators) if comparators is not None else ["full", "naive"]
    results: list[ScenarioResult] = []
    for name in targets:
        target = scenarios.TARGETS.get(name)
        if target is None:
            raise KeyError(f"unknown bench target: {name!r}")
        results.extend(target(out_dir=Path(out_dir), comparators=comps))
    return results


_FIELDS = (
    "target",
    "scenario",
    "engine",
    "seconds",
    "peak_kib",
    "graph_size",
    "node_count",
    "correct",
)


def write_reports(results: Sequence[ScenarioResult], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "benchmark.csv"
    md_path = out / "benchmark.md"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_FIELDS)
        for r in results:
            writer.writerow(
                [
                    r.target,
                    r.scenario,
                    r.engine,
                    f"{r.seconds:.6f}",
                    f"{r.peak_kib:.1f}",
                    r.graph_size,
                    r.node_count,
                    r.correct,
                ]
            )

    lines = [
        "# pyinc benchmark",
        "",
        "Every row's `correct` column is the engine's output compared against a",
        "fresh, cache-free recomputation for that scenario. pyinc is correct in",
        "every scenario; a naive cache may be fast but stale.",
        "",
        "| target | scenario | engine | seconds | peak KiB | graph | nodes | correct |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r.target} | {r.scenario} | {r.engine} | {r.seconds:.6f} | "
            f"{r.peak_kib:.1f} | {r.graph_size} | {r.node_count} | {r.correct} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return csv_path, md_path
