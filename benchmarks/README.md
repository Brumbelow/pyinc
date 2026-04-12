# Benchmarks

`benchmarks/run_microbench.py` now drives both the benchmark suite and the reporting layer.

The harness is intentionally stdlib-only and focuses on internal baselines:

- incremental work on one long-lived `Database`
- fresh recomputation on a new `Database` with the same edits
- semantic markers such as reuse, execution, and backdating

## CLI

Run from repo root:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py --suite all --format table
```

Common flags:

- `--suite {micro,workload,all}`: choose the benchmark family
- `--bench NAME`: run one scenario or `all`
- `--format {table,json,markdown}`: choose stdout format; default is `table`
- `--output-json PATH`: write a JSON artifact
- `--output-markdown PATH`: write a Markdown artifact
- `--samples N`, `--warmup N`, `--rounds N`: measurement controls
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

Legacy aliases still work for the original scenarios: `diamond`, `rewiring`, `files`, `large`, and `backdating`.

## Output

Default terminal output is a compact table with:

- scenario and phase
- mode and sample count
- mean and p95 latency
- ops/s
- `vs_fresh` speedup when a fresh baseline exists
- compact semantic markers

Markdown output adds grouped scenario sections plus a short interpretation line for each scenario. JSON output is the machine-readable schema with environment metadata, parameters, phases, comparisons, and invariant summaries.

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
