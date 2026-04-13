# Benchmarks

`benchmarks/run_microbench.py` now drives both the benchmark suite and the reporting layer.

The harness is intentionally stdlib-only and focuses on internal baselines:

- incremental work on one long-lived `Database`
- fresh recomputation on a new `Database` with the same edits
- semantic markers such as reuse, execution, and backdating
- workload comparison against a plain stdlib baseline with no incremental engine

## CLI

Run from repo root:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py --suite all --format table
```

Common flags:

- `--suite {micro,workload,all}`: choose the benchmark family
- `--bench NAME`: run one scenario or `all`
- `--implementation {incremental,plain,compare}`: choose the incremental engine, the plain workload baseline, or the workload comparison report
- `--format {table,json,markdown}`: choose stdout format; default is `table`
- `--output-json PATH`: write a JSON artifact
- `--output-markdown PATH`: write a Markdown artifact
- `--samples N`, `--warmup N`, `--rounds N`: measurement controls
- `--bootstrap-resamples N`: bootstrap resamples for paired speedup confidence intervals
- `--confidence-level FLOAT`: confidence level for paired speedup intervals (default `0.95`)
- `--seed N`: deterministic seed for paired bootstrap resampling
- `--pair-order {alternating,candidate_first,baseline_first}`: pair alignment strategy
- `--payload-size N`: generic scale factor for larger scenarios
- `--mode {strict,checked,fast}`: optional mode override

`--output` remains as a backward-compatible alias for `--output-json`.

## Suites

Micro scenarios:

- `diamond_reuse`
- `dynamic_rewiring`
- `resource_reads`
- `large_boundary`
- `query_backdating`
- `backdating_chain`
- `rewiring_torture`
- `cutoff_economics`
- `resource_granularity`
- `lru_pressure`

Workload scenarios:

- `source_analysis`
- `workspace_import_graph`

Legacy aliases still work for the original scenarios: `diamond`, `rewiring`, `files`, `large`, and `backdating`.

`--implementation plain` and `--implementation compare` are workload-only. They support `source_analysis` and `workspace_import_graph`.

## Output

Default terminal output is a compact table with:

- scenario and phase
- mode and sample count
- mean and p95 latency
- ops/s
- `vs_fresh_x` and paired confidence interval when a fresh baseline exists
- compact semantic markers

Compare mode renders workload tables with:

- incremental mean/p95
- plain mean/p95
- `speedup_x` (paired geometric speedup ratio) and `speedup_ci_x`
- `latency_reduction_pct` and `latency_reduction_ci_pct`
- `speedup_pct` for backward compatibility (`100% == 1.0x`, this is ratio-as-percent, not percent-faster)
- incremental semantic markers
- paired sample count (`paired_n`)

Formulas:

- `speedup_x = exp(mean(log(baseline_i) - log(candidate_i)))`
- `latency_reduction_pct = 100 * (1 - 1 / speedup_x)`
- confidence intervals are paired bootstrap percentile intervals computed from the paired sample units

Markdown output adds grouped scenario sections with raw metric tables only. JSON output is the machine-readable schema with environment metadata, parameters, phases or operations, paired comparisons, and invariant summaries.

## Recommended Commands

Quick smoke:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py \
  --suite all \
  --samples 1 \
  --warmup 0 \
  --rounds 1 \
  --payload-size 8 \
  --format table \
  --output-json /tmp/pyfoundinc-bench.json \
  --output-markdown /tmp/pyfoundinc-bench.md
```

Deeper local run:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py \
  --suite all \
  --samples 30 \
  --warmup 5 \
  --rounds 3 \
  --payload-size 512 \
  --format table \
  --output-json /tmp/pyfoundinc-bench.json \
  --output-markdown /tmp/pyfoundinc-bench.md
```

Workload-only report:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py \
  --suite workload \
  --format markdown
```

Workload comparison report:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py \
  --suite workload \
  --bench workspace_import_graph \
  --implementation compare \
  --format markdown \
  --output-json /tmp/pyfoundinc-workload-compare.json \
  --output-markdown /tmp/pyfoundinc-workload-compare.md
```
