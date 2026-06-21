"""Benchmark + correctness harness for pyinc (not shipped in the wheel).

Exercises four targets — synthetic kernel query graphs, the calc-with-includes
fixture, JSON-Schema code generation, and action reconciliation — across a
canonical edit sequence, comparing pyinc against full recomputation, a naive
per-key cache, and (optionally) ``joblib.Memory``. Every scenario pairs its
timing with a correctness assertion that the incremental output equals a fresh,
cache-free run.

``joblib`` is a bench-only optional dependency (``pip install -e '.[bench]'``)
and is imported lazily; it is never imported by ``src/pyinc`` or
``src/pyinc_codegen``.
"""
