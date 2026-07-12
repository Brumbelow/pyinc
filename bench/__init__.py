"""Benchmark + correctness harness for pyinc (not shipped in the wheel).

Exercises four targets — synthetic kernel query graphs, the calc-with-includes
fixture, JSON-Schema code generation, and action reconciliation — across a
canonical edit sequence, comparing pyinc against full recomputation, an
intentional naive-cache control, and ``joblib.Memory``. Every scenario pairs its
informational timing with correctness and deterministic-work assertions.

``joblib`` is required when running this bench-only harness
(``pip install -e '.[bench]'``), but is imported lazily and never imported by
``src/pyinc`` or ``src/pyinc_codegen``.
"""
