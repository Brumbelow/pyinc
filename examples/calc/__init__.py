"""``calc`` — a minimal include-aware expression language, used as the canonical
end-to-end pyinc example.

It exercises the three-layer query pattern, cross-file dependency tracking via a
single shared ``FileResource``, backdating on comment/whitespace edits, per-name
incremental evaluation, and output reconciliation through the ``@action`` layer.
See ``examples/calc/engine.py``.
"""
