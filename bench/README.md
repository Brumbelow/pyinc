# pyinc benchmark + correctness harness

Reproducible benchmarks that **never report a timing without first proving
correctness**. Every timed incremental result is compared byte-for-byte against a
fresh, cache-disabled `Database` recomputation; a mismatch fails loudly.

## Run

```bash
python -m pip install -e '.[bench]'        # benchmark-only deps (joblib)
python -m bench.run --output-dir bench/results [--warmup 1] [--repetitions 5]
```

Outputs (under `--output-dir`):

- `benchmark.csv` — raw records, one row per (workload, scenario, implementation).
- `benchmark.md` — report **generated from the CSV** (never hand-edited).
- `metadata.json` — environment metadata (timestamp, git commit, Python
  impl/version, platform, CPU count, pyinc + benchmark dependency versions,
  warmup/repetition config).

## What it measures

Workloads: the kernel directly (synthetic high-fan-out graph), the GraphQL
generator, and the detection compiler. Scenarios: cold, warm, presentation-only
edit, localized semantic edit, high-fan-out shared-dependency edit, output
tampering + repair, checkpoint restore in a fresh `Database`, and full
recomputation.

Comparison implementations: `pyinc_incremental`, `fresh_full` (the correctness
oracle), a deliberately simple `naive_cache`, and `joblib_memory` (where its
argument-based memoization fits; `N/A` otherwise). These baselines do **not**
provide identical correctness or dependency semantics — see the capability-
differences section the report prints.

## Notes

- Timings are single-machine and are **not** universal speed claims.
- `joblib` is the only extra dependency and lives in the `bench` optional group;
  nothing under `src/pyinc` imports it.
- The normal test suite (`tests/test_bench.py`) covers the adapters and report
  generation with **no** brittle timing assertions.
