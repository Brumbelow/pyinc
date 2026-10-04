"""``calc``, a minimal include-aware expression language.

It is pyinc's canonical end-to-end example. It exercises the three-layer query
pattern, cross-file dependency tracking through one shared ``FileResource``,
per-name incremental evaluation, and output reconciliation through the
``@action`` layer. A comment or whitespace edit backdates the parse because its
payload stays equal. The one exception is an edit on a line the parser rejects,
because the diagnostic quotes that line verbatim. See ``examples/calc/engine.py``.
"""
