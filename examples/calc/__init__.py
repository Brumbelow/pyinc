"""``calc`` — a minimal include-aware expression language, used as the canonical
end-to-end pyinc example.

It exercises the three-layer query pattern, cross-file dependency tracking via a
single shared ``FileResource``, backdating of the parse when a comment/whitespace
edit leaves its payload equal — which is every such edit except one on a line the
parser rejects and quotes verbatim in its diagnostic — per-name incremental
evaluation, and output reconciliation through the ``@action`` layer.
See ``examples/calc/engine.py``.
"""
