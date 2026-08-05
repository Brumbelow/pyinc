# Benchmark and correctness harness

This harness exercises pyinc's incremental behavior against fresh recomputation and two
cache comparators. Correctness and deterministic work counts are release gates. Wall-clock
timings are environment-specific diagnostics and are never release thresholds.

## Run it

Install the locked benchmark toolchain and the project:

```console
python -m pip install --require-hashes --only-binary=:all: -r requirements/toolchain.lock
python -m pip install --no-build-isolation --no-deps -e .
python scripts/check_toolchain.py --verify-installed
```

Run the release configuration from the repository root:

```console
python -m bench.run --output bench/results --repetitions 5
```

The command launches five isolated Python processes with `PYTHONHASHSEED=0`. It fails if
`joblib` is unavailable, a worker emits anything other than the fixed 67-row matrix, work
counts differ between repetitions, or a correctness/work gate fails.

The benchmark workflow is manual and reusable; it is not run for ordinary pushes or pull
requests. Its 90-day workflow artifact supports CI investigation. For a release, automation
also packages these exact results with the command and layered SHA-256 manifests and attaches
that evidence bundle to the GitHub Release, where it is not tied to workflow-artifact expiry.

## Methodology

Each repetition creates new scratch directories, databases, comparator caches, and output
trees. The four targets are:

- `synthetic`: a six-branch query graph with localized and shared-input edits;
- `calc`: the include-aware calculator example and its declared outputs;
- `codegen`: JSON-Schema analysis, typed-Python generation, and reconciliation;
- `action`: creation, reuse, deletion, and tamper repair in isolation.

Every engine result is compared with a fresh, cache-free recomputation of the same state.
The fixed comparator set is full recomputation, an intentionally incomplete naive cache,
and `joblib.Memory`. Joblib applies to the synthetic function-cache comparison; the realistic
action-backed targets compare pyinc with fresh recomputation, and calc also carries the
naive output-cache control.

Checkpoint files are saved before timing starts. A checkpoint row measures only loading the
saved checkpoint and requesting/reconciling the warmed result. Wall timing uses
`time.perf_counter()` without `tracemalloc` or other memory instrumentation.

For pyinc rows, the harness records per-scenario query executions, reuses, backdates, and
resource loads. It also records resident memo nodes and real dependency edges (the sum of
every graph node's dependency labels), plus each operation's node and edge delta.

## Release gates

Each repetition must contain exactly 67 rows with the fixed target/scenario/engine matrix.
The following conditions are enforced:

- every pyinc, full-recompute, and joblib row matches fresh recomputation;
- exactly two naive-cache controls are stale: the synthetic shared-input edit and calc output
  tampering;
- unchanged and unreferenced edits execute zero queries;
- formatting-only edits backdate and perform zero downstream query executions;
- localized edits perform targeted work, while removals and tampering delete or repair the
  expected files;
- every pyinc row stays within its reviewed execution, backdate, resource-load, node, and edge
  envelope, so a deterministic regression to full-graph recomputation still fails;
- memo-node ceilings are 16 for synthetic, 24 for calc, 40 for codegen, and 8 for action;
- deterministic work counts match across all five isolated repetitions. The report also records
  the call-level `query_reuses` statistic and requires it to match across those same-path runs,
  but does not impose an absolute cross-path envelope: absolute path arguments can change
  dependency verification order and therefore the number of repeated already-checked calls
  without changing executions or graph work.

The release suite separately retains the 1,000-argument LRU and 1,000-module workspace
scalability tests.

## Artifacts

The output directory contains only generated artifacts and is ignored by Git:

- `samples.csv`: all 335 raw samples, including repetition number and work counts;
- `benchmark.csv`: 67 summarized rows with median and min/max wall time;
- `benchmark.md`: a concise human-readable correctness and timing summary;
- `metadata.json`: exact commit SHA, dirty-tree state, Python/build details, OS/runner and CPU
  information, comparator versions, targets, and repetition count.

Use `samples.csv` and `metadata.json` when investigating timing changes. A timing difference
without a correctness failure, work-count change, or node-ceiling breach is not a release
failure.
