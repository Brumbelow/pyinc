"""Reproducible benchmark + correctness harness for pyinc.

Exercises the kernel directly, the GraphQL generator, and the detection-content
compiler across a fixed set of scenarios. Every timed incremental result is
compared byte-for-byte against a fresh, cache-disabled recomputation; a mismatch
fails loudly and no timing is emitted without a passing correctness assertion.

Run with::

    python -m bench.run --output-dir bench/results

Benchmark-only dependencies (``joblib``) live in the ``bench`` optional-dependency
group; nothing under ``src/pyinc`` imports them.
"""
