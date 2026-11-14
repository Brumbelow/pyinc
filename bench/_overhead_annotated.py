"""The annotated half of the evaluated-annotation line in ``bench.overhead``.

This module has no ``from __future__ import annotations``, so ``db: Database``
is a real expression that names the class. The query's identity then folds
``pyinc/runtime.py``, the file that defines it. ``bench.overhead`` holds the
same query under the future import, where the annotation is a string.
"""

from pyinc import Database, Input, query

NUMBER = Input[int]("bench-overhead-annotated-number")


@query
def annotated_double(db: Database) -> int:
    return NUMBER.read(db) * 2
