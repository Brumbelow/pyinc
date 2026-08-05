from __future__ import annotations

from collections.abc import Callable

import pytest

from pyinc import Database, Query, UnsupportedValueError

_MODES = ("strict", "checked", "fast")


class _SneakyDict(dict[str, object]):
    def __init__(self, marker: object) -> None:
        super().__init__(marker=marker)
        self.current = marker

    def __getitem__(self, key: str) -> object:
        if key == "marker":
            return self.current
        return super().__getitem__(key)


class _SneakyTuple(tuple[object, ...]):
    def __new__(cls, marker: object) -> _SneakyTuple:
        return tuple.__new__(cls, (marker,))

    def __init__(self, marker: object) -> None:
        self.current = marker

    def __getitem__(self, index: int | slice) -> object:  # type: ignore[override]
        if index == 0:
            return self.current
        return tuple.__getitem__(self, index)


def _function_with_metadata(surface: str) -> Callable[[Database], int]:
    marker = tuple([1])

    def child(db: Database, positional: object = marker, *, keyword: object = marker) -> int:
        del db, positional, keyword
        return 1

    if surface == "defaults":
        child.__defaults__ = _SneakyTuple(marker)
    elif surface == "kwdefaults":
        child.__kwdefaults__ = _SneakyDict(marker)
    elif surface == "annotations":
        child.__annotations__ = _SneakyDict(marker)
    else:
        child.__dict__ = _SneakyDict(marker)
    return child


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("surface", ("defaults", "kwdefaults", "annotations", "dict"))
@pytest.mark.parametrize("path", ("top-level", "captured"))
def test_function_metadata_container_subclasses_are_rejected_before_caching(
    mode: str,
    surface: str,
    path: str,
) -> None:
    child = _function_with_metadata(surface)
    child_query = Query(child, key=f"metadata-container-child-{surface}-{mode}-{path}")

    if path == "top-level":
        requested = child_query
    else:

        def parent(db: Database) -> int:
            del db
            if surface == "defaults":
                return id(child.__defaults__[0])  # type: ignore[index]
            if surface == "kwdefaults":
                return id(child.__kwdefaults__["marker"])  # type: ignore[index]
            if surface == "annotations":
                return id(child.__annotations__["marker"])
            return id(child.__dict__["marker"])

        requested = Query(
            parent,
            key=f"metadata-container-parent-{surface}-{mode}-{path}",
        )

    warm = Database(mode=mode)
    with pytest.raises(UnsupportedValueError, match="exact (tuple|dict)"):
        warm.get(requested)
    with pytest.raises(UnsupportedValueError, match="exact (tuple|dict)"):
        Database(mode=mode).get(requested)
    assert warm.statistics().node_count == 0
