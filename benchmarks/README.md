# Benchmarks

Kernel microbench scenarios live in `benchmarks/run_microbench.py`.

Run from repo root:

```bash
PYTHONPATH=src python benchmarks/run_microbench.py --samples 200 --payload-size 5000
```

The script reports cold/warm/delta timings for:

- diamond dependency reuse
- dynamic rewiring
- resource-backed file/directory reads
- large boundary values (warm, equal-update, and delta paths)

These numbers are non-gating regressions during kernel hardening.
