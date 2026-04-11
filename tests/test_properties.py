from __future__ import annotations

from pathlib import Path
import tempfile

import pytest

pytest.importorskip("hypothesis")

from hypothesis import given, settings, strategies as st

from pyfoundinc import Database, FileResource, Input, query


Operation = tuple[str, int | str]


def operation_sequences() -> st.SearchStrategy[list[Operation]]:
    choose_side = st.tuples(st.just("chooser"), st.sampled_from(["left", "right"]))
    update_value = st.one_of(
        st.tuples(st.just("left"), st.integers(min_value=-20, max_value=20)),
        st.tuples(st.just("right"), st.integers(min_value=-20, max_value=20)),
        st.tuples(st.just("offset"), st.integers(min_value=-20, max_value=20)),
    )
    return st.lists(st.one_of(choose_side, update_value), min_size=1, max_size=30)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=50, deadline=None)
@given(steps=operation_sequences())
def test_incremental_results_match_fresh_recomputation(mode: str, steps: list[Operation]) -> None:
    chooser = Input[str]("chooser")
    left = Input[int]("left")
    right = Input[int]("right")
    offset = Input[int]("offset")
    state: dict[str, int | str] = {"chooser": "left", "left": 0, "right": 0, "offset": 0}
    inputs = {
        "chooser": chooser,
        "left": left,
        "right": right,
        "offset": offset,
    }

    @query
    def selected(db: Database) -> int:
        if chooser.read(db) == "left":
            return left.read(db)
        return right.read(db)

    @query
    def parity(db: Database) -> str:
        return "even" if selected(db) % 2 == 0 else "odd"

    @query
    def describe(db: Database) -> tuple[str, int]:
        return parity(db), selected(db) + offset.read(db)

    incremental = Database(mode=mode)
    for name, value in state.items():
        incremental.set(inputs[name], value)

    for name, value in steps:
        state[name] = value
        incremental.set(inputs[name], value)

        fresh = Database(mode=mode)
        for current_name, current_value in state.items():
            fresh.set(inputs[current_name], current_value)

        assert incremental.get(describe) == fresh.get(describe)


def file_contents() -> st.SearchStrategy[list[str]]:
    return st.lists(
        st.sampled_from(
            [
                "",
                "import os\n",
                "# comment\nimport os\n",
                "import sys\n",
                "value = 1\n",
            ]
        ),
        min_size=1,
        max_size=20,
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@settings(max_examples=30, deadline=None)
@given(contents=file_contents())
def test_resource_backed_queries_match_fresh_recomputation(mode: str, contents: list[str]) -> None:
    files = FileResource()

    @query
    def source(db: Database, filename: str) -> str:
        return files.read(db, filename)

    @query
    def diagnostics(db: Database, filename: str) -> tuple[bool, int]:
        current = source(db, filename)
        return "import os" in current, len(current.splitlines())

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "sample.py"
        incremental = Database(mode=mode)

        for content in contents:
            path.write_text(content, encoding="utf-8")

            fresh = Database(mode=mode)
            assert incremental.get(diagnostics, str(path)) == fresh.get(diagnostics, str(path))
