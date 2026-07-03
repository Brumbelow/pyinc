"""Timing/memory measurement, scenario orchestration, and report writing."""

from __future__ import annotations

import csv
from collections.abc import Iterable, Sequence
from pathlib import Path

from . import labels
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


# CSV header: identity columns followed by one column per metric. The metric
# fields (and their pyinc-only semantics) live in ``labels`` so the CSV and the
# markdown report stay in sync.
_ID_FIELDS = ("target", "scenario", "engine")
_FIELDS = _ID_FIELDS + tuple(m.csv_field for m in labels.METRICS)


def _metric_value(result: ScenarioResult, metric: labels.Metric) -> object:
    """Raw cell value for ``metric``; ``None`` when it does not apply (rendered
    as a blank cell rather than a misleading ``0``)."""
    is_pyinc = result.engine == "pyinc"
    if metric is labels.WALL:
        return f"{result.seconds:.6f}"
    if metric is labels.PEAK:
        return f"{result.peak_kib:.1f}"
    if metric is labels.GRAPH:
        return result.graph_size if is_pyinc else None
    if metric is labels.NODES:
        return result.node_count if is_pyinc else None
    if metric is labels.CORRECT:
        return result.correct
    raise AssertionError(f"unhandled metric: {metric.csv_field}")


def _write_csv(results: Sequence[ScenarioResult], csv_path: Path) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(_FIELDS)
        for r in results:
            row = [r.target, r.scenario, r.engine]
            for metric in labels.METRICS:
                value = _metric_value(r, metric)
                row.append("" if value is None else value)
            writer.writerow(row)


def _grouped(
    results: Sequence[ScenarioResult],
) -> list[tuple[tuple[str, str], list[ScenarioResult]]]:
    """Group results by (target, scenario), preserving first-seen order."""
    order: list[tuple[str, str]] = []
    buckets: dict[tuple[str, str], list[ScenarioResult]] = {}
    for r in results:
        key = (r.target, r.scenario)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(r)
    return [(key, buckets[key]) for key in order]


def _speedup(baseline_seconds: float | None, seconds: float) -> str:
    """Human phrasing of ``seconds`` relative to the ``full`` baseline."""
    if baseline_seconds is None:
        return "—"
    if seconds <= 0 or baseline_seconds <= 0:
        return "—"
    factor = baseline_seconds / seconds
    if abs(factor - 1.0) < 0.05:
        return "≈ baseline"
    if factor >= 1.0:
        return f"{factor:.1f}× faster"
    return f"{1.0 / factor:.1f}× slower"


def _correct_cell(correct: bool, engine: str) -> str:
    if correct:
        return "✅ yes"
    return "⚠️ **STALE**" if engine != "pyinc" else "❌ **WRONG**"


def _write_markdown(results: Sequence[ScenarioResult], md_path: Path) -> None:
    engines = [e for e in labels.ENGINE_LABELS if any(r.engine == e for r in results)]
    scenarios_present = [
        s for s in labels.SCENARIO_LABELS if any(r.scenario == s for r in results)
    ]
    stale = [r for r in results if r.engine != "pyinc" and not r.correct]

    lines: list[str] = [
        "# pyinc benchmark",
        "",
        "Each scenario applies one canonical edit and times every engine on it. "
        "The **correct?** column compares that engine's output against a fresh, "
        "cache-free recomputation of the same scenario.",
        "",
        f"**pyinc is correct in every scenario below.** The comparators "
        f"(`{labels.BASELINE_ENGINE}` recompute, naive per-key cache, "
        "`joblib.Memory`) are included to show the trade-off: a naive cache can "
        "be faster than pyinc yet serve a **stale** result where a real "
        "dependency changed.",
        "",
    ]

    if stale:
        lines.append("## ⚠️ Stale results (fast but wrong)")
        lines.append("")
        lines.append(
            "These comparator runs finished quickly but returned output that does "
            "**not** match a fresh recomputation — the exact failure pyinc "
            "prevents:"
        )
        lines.append("")
        for r in stale:
            lines.append(
                f"- **{labels.engine_label(r.engine)}** on "
                f"*{labels.target_label(r.target)} → "
                f"{labels.scenario_title(r.scenario)}* "
                f"({labels.scenario_description(r.scenario)})"
            )
        lines.append("")

    # Legends.
    lines.append("## Scenarios")
    lines.append("")
    lines.append("| scenario | what it does |")
    lines.append("|---|---|")
    for s in scenarios_present:
        lines.append(f"| **{labels.scenario_title(s)}** | {labels.scenario_description(s)} |")
    lines.append("")

    lines.append("## Metrics")
    lines.append("")
    lines.append("| column | meaning |")
    lines.append("|---|---|")
    for metric in labels.METRICS:
        lines.append(f"| {metric.header} | {metric.description} |")
    lines.append(
        f"| speedup | wall time relative to the `{labels.BASELINE_ENGINE}` "
        "recompute for that scenario |"
    )
    lines.append("")
    lines.append(
        "Engines: "
        + ", ".join(f"`{e}` — {labels.engine_label(e)}" for e in engines)
        + "."
    )
    lines.append("")

    # Per-target, per-scenario comparison tables.
    current_target: str | None = None
    for (target, scenario), rows in _grouped(results):
        if target != current_target:
            lines.append(f"## {labels.target_label(target)}")
            lines.append("")
            note = labels.TARGET_NOTES.get(target)
            if note:
                lines.append(note)
                lines.append("")
            current_target = target
        lines.append(f"### {labels.scenario_title(scenario)}")
        lines.append("")
        lines.append(f"_{labels.scenario_description(scenario)}_")
        lines.append("")
        baseline = next(
            (r.seconds for r in rows if r.engine == labels.BASELINE_ENGINE), None
        )
        lines.append("| engine | wall (ms) | peak (KiB) | correct? | speedup |")
        lines.append("|---|---|---|---|---|")
        for r in rows:
            speedup = (
                "baseline" if r.engine == labels.BASELINE_ENGINE
                else _speedup(baseline, r.seconds)
            )
            lines.append(
                f"| {labels.engine_label(r.engine)} | {r.seconds * 1000:.2f} | "
                f"{r.peak_kib:.1f} | {_correct_cell(r.correct, r.engine)} | {speedup} |"
            )
        lines.append("")

    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_reports(results: Sequence[ScenarioResult], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "benchmark.csv"
    md_path = out / "benchmark.md"
    _write_csv(results, csv_path)
    _write_markdown(results, md_path)
    return csv_path, md_path
