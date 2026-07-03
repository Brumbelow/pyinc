"""CLI entrypoint: run every target and write a reproducible report.

    python -m bench.run

Scratch workspaces are built in a temporary directory; only the CSV + markdown
report are written under ``bench/results/``. Exits non-zero if any pyinc row is
incorrect (incremental output != fresh recomputation).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .baselines import available_comparators
from .harness import run_scenarios, write_reports


def main() -> None:
    results_dir = Path(__file__).resolve().parent / "results"
    comparators = available_comparators()

    with tempfile.TemporaryDirectory() as scratch:
        results = run_scenarios(
            ["synthetic", "calc", "codegen", "action"],
            out_dir=scratch,
            comparators=comparators,
        )

    csv_path, md_path = write_reports(results, results_dir)
    pyinc_rows = [r for r in results if r.engine == "pyinc"]
    incorrect = [r for r in pyinc_rows if not r.correct]
    stale = [r for r in results if r.engine != "pyinc" and not r.correct]

    print(f"rows={len(results)} pyinc_rows={len(pyinc_rows)} comparators={comparators}")
    print("")
    print(f"{'target':<12} {'pyinc':>7} {'correct':>8} {'stale comparators':>18}")
    print("-" * 48)
    for name in ("synthetic", "calc", "codegen", "action"):
        target_pyinc = [r for r in pyinc_rows if r.target == name]
        if not target_pyinc:
            continue
        all_correct = all(r.correct for r in target_pyinc)
        stale_here = len([r for r in stale if r.target == name])
        print(
            f"{name:<12} {len(target_pyinc):>7} "
            f"{('all ok' if all_correct else 'FAILED'):>8} {stale_here:>18}"
        )
    print("-" * 48)
    print(f"report_csv={csv_path}")
    print(f"report_md={md_path}")
    if stale:
        pairs = sorted({(r.target, r.scenario, r.engine) for r in stale})
        print(f"note: {len(stale)} comparator run(s) were stale (fast but wrong): {pairs}")
    if incorrect:
        raise SystemExit(f"pyinc incorrectness detected: {[(r.target, r.scenario) for r in incorrect]}")


if __name__ == "__main__":
    main()
