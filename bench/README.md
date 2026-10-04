# Benchmark and correctness harness

This harness checks pyinc's incremental behavior against fresh recomputation and two
cache comparators. Correctness and deterministic work counts are release gates. Wall-clock
timings are environment-specific diagnostics and stay outside the release gates.

## Run it

Install the benchmark comparator with the project:

```console
python -m pip install -e '.[bench]'
```

Run the release configuration from the repository root:

```console
python -m bench.run --output bench/results --repetitions 5
```

The command launches five isolated Python processes with `PYTHONHASHSEED=0`. It fails if
`joblib` is missing, a worker's rows differ from the fixed 67-row matrix, work counts differ
between repetitions, or a correctness/work gate fails.

The benchmark workflow is manual and reusable. It runs on demand, separate from the checks
on ordinary pushes and pull requests. Its uploaded artifacts are the authoritative record
for a particular run.

## Methodology

Each repetition creates new scratch directories, databases, comparator caches, and output
trees. The four targets are:

- `synthetic`: a six-branch query graph with localized and shared-input edits;
- `calc`: the include-aware calculator example and its declared outputs;
- `codegen`: JSON-Schema analysis, typed-Python generation, and reconciliation;
- `action`: creation, reuse, deletion, and tamper repair in isolation.

Every engine result is compared with a fresh, cache-free recomputation of the same state.
The fixed comparator set is full recomputation, an intentionally incomplete naive cache,
and `joblib.Memory`. Joblib applies to the synthetic function-cache comparison. The realistic
action-backed targets compare pyinc with fresh recomputation, and calc also carries the
naive output-cache control.

Checkpoint files are saved before timing starts. A checkpoint row measures only loading the
saved checkpoint and requesting/reconciling the warmed result. Wall timing uses
`time.perf_counter()` without `tracemalloc` or other memory instrumentation.

For pyinc rows, the harness records per-scenario query executions, reuses, backdates, and
resource loads. It also records resident memo nodes and real dependency edges (the sum of
every graph node's dependency labels), plus each operation's node and edge delta.

## Release gates

Each repetition must contain the 67 rows of the fixed target/scenario/engine matrix, and
no others. The gates enforce:

- every pyinc, full-recompute, and joblib row matches fresh recomputation;
- the only stale rows are two naive-cache controls: the synthetic shared-input edit and calc
  output tampering;
- unchanged and unreferenced edits execute zero queries;
- formatting-only edits backdate and perform zero downstream query executions;
- localized edits perform targeted work, and removals and tampering delete or repair the
  expected files;
- every pyinc row stays within its reviewed execution, reuse, backdate, resource-load, node,
  and edge bounds, so a deterministic regression to full-graph recomputation fails;
- memo-node ceilings are 16 for synthetic, 24 for calc, 40 for codegen, and 8 for action;
- deterministic work counts match across all five isolated repetitions.

The 1,000-argument LRU and 1,000-module workspace scalability tests stay in the release
suite, separate from this harness.

## Artifacts

The output directory contains only generated artifacts and is ignored by Git:

- `samples.csv`: all 335 raw samples, including repetition number and work counts;
- `benchmark.csv`: 67 summarized rows with median and min/max wall time;
- `benchmark.md`: a concise human-readable correctness and timing summary;
- `metadata.json`: exact commit SHA, dirty-tree state, Python/build details, OS/runner and CPU
  information, comparator versions, targets, and repetition count.

Use `samples.csv` and `metadata.json` to investigate timing changes. A timing difference
with no correctness failure, work-count change, or node-ceiling breach still passes the
release.
