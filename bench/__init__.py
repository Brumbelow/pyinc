"""Benchmark and correctness harness for pyinc (excluded from the wheel).

Runs a canonical edit sequence against four targets: synthetic kernel query
graphs, the calc-with-includes fixture, JSON-Schema code generation, and action
reconciliation. Each target compares pyinc with full recomputation, an
intentional naive-cache control, and ``joblib.Memory``. Every scenario pairs its
informational timing with correctness and deterministic-work assertions.

The harness needs ``joblib`` (``pip install -e '.[bench]'``) and imports it
lazily. ``src/pyinc`` and ``src/pyinc_codegen`` never import it.
"""
