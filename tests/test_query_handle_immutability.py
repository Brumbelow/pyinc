from __future__ import annotations

from typing import Any, cast

import pytest

from pyinc import Database, InMemoryArtifactStore, Input, Query, query

_MODES = ("strict", "checked", "fast")


@pytest.mark.parametrize("mode", _MODES)
def test_public_handles_cannot_gain_subclass_state(mode: str) -> None:
    with pytest.raises(TypeError, match="Input handles cannot be subclassed"):

        class MutableInput(Input[int]):  # type: ignore[misc]
            __slots__ = ("marker",)

    with pytest.raises(TypeError, match="Query handles cannot be subclassed"):

        class MutableQuery(Query[[], int]):  # type: ignore[misc]
            __slots__ = ("marker",)

    source = Input[int](f"exact-handle-input-{mode}")

    @query(key=f"exact-handle-query-{mode}")
    def read_source(db: Database) -> int:
        return source.read(db)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(source, 1)
    fresh = Database(mode=mode)
    fresh.set(source, 1)
    assert writer.get(read_source) == fresh.get(read_source)
    checkpoint = writer.save_checkpoint()
    reader = Database(mode=mode, store=store)
    reader.set(source, 1)
    reader.load_checkpoint(checkpoint)
    assert reader.get(read_source) == 1


def _document_parent(child: Query[[], int]) -> Query[[], tuple[str | None, int]]:
    @query(key="query-handle-document-parent")
    def parent(db: Database) -> tuple[str | None, int]:
        return child.__doc__, child(db)

    return parent


@pytest.mark.parametrize("mode", _MODES)
def test_query_handle_rejects_all_state_changes_and_stays_warm_fresh_equal(
    mode: str,
) -> None:
    @query(key="query-handle-immutable-child")
    def child(db: Database) -> int:
        """Stable child documentation."""
        return 7

    parent = _document_parent(child)
    store = InMemoryArtifactStore()
    warm = Database(mode=mode, store=store)
    expected = ("Stable child documentation.", 7)
    assert warm.get(parent) == expected
    checkpoint = warm.save_checkpoint()

    replacements: dict[str, Any] = {
        "fn": lambda _db: 9,
        "eq": lambda left, right: left == right,
        "cutoff": abs,
        "key": "changed-key",
        "__name__": "changed_name",
        "__qualname__": "changed_qualname",
        "__module__": "changed_module",
        "__doc__": "Changed documentation.",
        "__wrapped__": lambda _db: 9,
        "attached": object(),
    }
    mutable = cast(Any, child)
    for name, replacement in replacements.items():
        with pytest.raises(AttributeError, match="Query handles are immutable"):
            setattr(mutable, name, replacement)
    with pytest.raises(AttributeError, match="Query handles are immutable"):
        del mutable.__doc__
    with pytest.raises(AttributeError):
        _ = mutable.__dict__
    with pytest.raises(AttributeError):
        object.__setattr__(child, "_wrapped", lambda _db: 9)

    assert warm.get(parent) == expected
    assert Database(mode=mode).get(parent) == expected

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(checkpoint)
    assert restored.get(parent) == expected


@pytest.mark.parametrize("mode", _MODES)
def test_reflective_public_metadata_change_moves_parent_identity(
    mode: str,
) -> None:
    @query(key="query-handle-reflective-child")
    def child(db: Database) -> int:
        """Original documentation."""
        return 7

    parent = _document_parent(child)
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(parent) == ("Original documentation.", 7)
    checkpoint = writer.save_checkpoint()

    object.__setattr__(child, "_doc", "Changed documentation.")
    expected = ("Changed documentation.", 7)
    assert writer.get(parent) == expected
    assert Database(mode=mode).get(parent) == expected

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(checkpoint)
    assert restored.get(parent) == expected


@pytest.mark.parametrize("mode", _MODES)
def test_captured_query_metadata_is_part_of_parent_identity_and_checkpoint_trust(
    mode: str,
) -> None:
    def raw(db: Database) -> int:
        """Old documentation."""
        return 1

    old_child = Query(raw, key="query-handle-metadata-child")
    old_parent = _document_parent(old_child)
    store = InMemoryArtifactStore()
    warm = Database(mode=mode, store=store)
    assert warm.get(old_parent) == ("Old documentation.", 1)
    checkpoint = warm.save_checkpoint()

    raw.__doc__ = "New documentation."
    new_child = Query(raw, key="query-handle-metadata-child")
    new_parent = _document_parent(new_child)
    expected = ("New documentation.", 1)

    assert warm.get(new_parent) == expected
    assert Database(mode=mode).get(new_parent) == expected

    collision = Database(mode=mode)
    assert collision.get(old_parent) == ("Old documentation.", 1)
    assert collision.get(new_parent) == expected

    restored = Database(mode=mode, store=store)
    restored.load_checkpoint(checkpoint)
    assert restored.get(new_parent) == expected

    post_mutation_store = InMemoryArtifactStore()
    post_mutation_writer = Database(mode=mode, store=post_mutation_store)
    assert post_mutation_writer.get(old_parent) == ("Old documentation.", 1)
    post_mutation_checkpoint = post_mutation_writer.save_checkpoint()
    post_mutation_reader = Database(mode=mode, store=post_mutation_store)
    post_mutation_reader.load_checkpoint(post_mutation_checkpoint)
    assert post_mutation_reader.get(new_parent) == expected


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("surface", ("annotation", "code"))
def test_captured_query_function_identity_surface_invalidates_warm_and_checkpoint(
    surface: str, mode: str
) -> None:
    marker = tuple([1])

    def raw(db: Database) -> int:
        return 1

    raw.__annotations__["marker"] = marker
    child = Query(raw, key=f"query-handle-function-surface-child:{surface}")
    if surface == "annotation":

        @query(key="query-handle-function-annotation-parent")
        def parent(db: Database) -> int:
            return id(child.fn.__annotations__["marker"])

        def expected() -> int:
            return id(child.fn.__annotations__["marker"])
    else:

        @query(key="query-handle-function-code-parent")
        def parent(db: Database) -> int:
            return id(child.fn.__code__)

        def expected() -> int:
            return id(child.fn.__code__)

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(parent) == expected()
    checkpoint = writer.save_checkpoint()

    if surface == "annotation":
        raw.__annotations__["marker"] = tuple([1])
    else:
        raw.__code__ = raw.__code__.replace()

    assert writer.get(parent) == Database(mode=mode).get(parent) == expected()
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(parent) == expected()
    assert reader.inspect(parent).last_recompute == "executed"
