from __future__ import annotations

import sys
from types import FunctionType
from typing import Any, TypeVar, cast

import pytest

from pyinc import Database, InMemoryArtifactStore, Query

_MODES = ("strict", "checked", "fast")


def _generic_function() -> FunctionType:
    namespace: dict[str, Any] = {"__name__": __name__}
    exec(
        "def reflected[T](db):\n    del db\n    return id(reflected.__type_params__[0])\n",
        namespace,
    )
    return cast(FunctionType, namespace["reflected"])


def _annotated_function() -> FunctionType:
    namespace: dict[str, Any] = {"__name__": __name__}
    source = (
        "def reflected(db: object) -> int:\n"
        "    del db\n"
        "    return id(reflected.__annotate__.__code__)\n"
    )
    exec(compile(source, "<deferred-annotation-test>", "exec", dont_inherit=True), namespace)
    return cast(FunctionType, namespace["reflected"])


@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="Python 3.14 function reflection surfaces are required",
)
@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("surface", ("type-params", "annotate"))
def test_function_reflective_surfaces_pin_equal_replacements_across_checkpoint(
    mode: str,
    surface: str,
) -> None:
    reflected = _generic_function() if surface == "type-params" else _annotated_function()
    reflective_surface = cast(Any, reflected)
    if surface == "annotate":
        _ = reflected.__annotations__
    requested = Query(reflected, key=f"function-reflective-{surface}-{mode}")

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(requested) == Database(mode=mode).get(requested)
    checkpoint = writer.save_checkpoint()

    if surface == "type-params":
        reflective_surface.__type_params__ = (TypeVar("T"),)
        expected = id(reflective_surface.__type_params__[0])
    else:
        evaluator = reflective_surface.__annotate__
        assert evaluator is not None
        evaluator.__code__ = evaluator.__code__.replace()
        expected = id(evaluator.__code__)

    assert writer.get(requested) == expected
    assert Database(mode=mode).get(requested) == expected

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(requested) == expected
    assert reader.inspect(requested).last_recompute == "executed"
