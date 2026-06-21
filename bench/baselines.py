"""Comparison baselines. ``joblib`` is imported lazily and only when present."""

from __future__ import annotations

import importlib.util


def joblib_available() -> bool:
    return importlib.util.find_spec("joblib") is not None


def available_comparators() -> list[str]:
    """Comparators that can run in this environment (joblib only if installed)."""
    comparators = ["full", "naive"]
    if joblib_available():
        comparators.append("joblib")
    return comparators


def make_joblib_memory(cache_dir: str):  # type: ignore[no-untyped-def]
    """Build a ``joblib.Memory`` cache. Imported lazily so the dependency stays
    optional and is never pulled in by the shipped packages."""
    import joblib

    return joblib.Memory(location=cache_dir, verbose=0)
