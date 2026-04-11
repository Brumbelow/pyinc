# Benchmarks

Kernel microbench scenarios live in `benchmarks/run_microbench.py`.

Run from repo root:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py --samples 200 --warmup 50 --rounds 5 --payload-size 5000
```

The script prints JSON to stdout and can optionally write the same payload with `--output`.

The suite covers:

- diamond dependency reuse
- dynamic rewiring
- resource-backed file/directory reads
- large boundary values (`warm`, `identical_update`, `equal_update`, and `delta`)
- true query backdating (`backdate` versus `real_change`)

These numbers are non-gating regressions during kernel hardening.
