import sys

import pytest

from pyinc import Database, InMemoryArtifactStore, UnsupportedValueError, query

_MODES = ("strict", "checked", "fast")


class _RuntimeAnnotated:
    marker: tuple([1])  # type: ignore[valid-type]


class _SneakyAnnotations(dict[str, object]):
    def __init__(self, marker: object) -> None:
        super().__init__(marker=marker)
        self.current = marker

    def __getitem__(self, key: str) -> object:
        if key == "marker":
            return self.current
        return super().__getitem__(key)


@pytest.mark.skipif(
    sys.version_info < (3, 14),
    reason="Python 3.14 deferred class annotations are required",
)
@pytest.mark.parametrize("mode", _MODES)
def test_deferred_class_annotations_pin_equal_replacements_across_checkpoint(
    mode: str,
) -> None:
    _RuntimeAnnotated.__annotations__["marker"] = tuple([1])

    @query(key=f"deferred-class-annotation-identity-{mode}")
    def annotation_identity(db: Database) -> int:
        del db
        return id(_RuntimeAnnotated.__annotations__["marker"])

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(annotation_identity) == id(_RuntimeAnnotated.__annotations__["marker"])
    checkpoint = writer.save_checkpoint()

    _RuntimeAnnotated.__annotations__["marker"] = tuple([1])
    expected = id(_RuntimeAnnotated.__annotations__["marker"])
    assert writer.get(annotation_identity) == expected
    assert Database(mode=mode).get(annotation_identity) == expected

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(annotation_identity) == expected
    assert reader.inspect(annotation_identity).last_recompute == "executed"


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("surface", ("function", "class"))
def test_annotation_mapping_subclasses_are_rejected_before_cache_or_checkpoint(
    mode: str,
    surface: str,
) -> None:
    marker = tuple([1])
    annotations = _SneakyAnnotations(marker)
    Holder = _RuntimeAnnotated
    original_annotations = Holder.__annotations__

    if surface == "function":

        def child(db: Database) -> int:
            del db
            return 1

        child.__annotations__ = annotations

        @query(key=f"subclassed-function-annotations-{mode}")
        def annotation_identity(db: Database) -> int:
            del db
            return id(child.__annotations__["marker"])

    else:
        Holder.__annotations__ = annotations

        @query(key=f"subclassed-class-annotations-{mode}")
        def annotation_identity(db: Database) -> int:
            del db
            return id(Holder.__annotations__["marker"])

    try:
        writer = Database(mode=mode, store=InMemoryArtifactStore())
        with pytest.raises(UnsupportedValueError, match="exact dict"):
            writer.get(annotation_identity)
        annotations.current = tuple([1])
        with pytest.raises(UnsupportedValueError, match="exact dict"):
            writer.get(annotation_identity)
        with pytest.raises(UnsupportedValueError, match="exact dict"):
            Database(mode=mode).get(annotation_identity)
        assert writer.statistics().node_count == 0
    finally:
        if surface == "class":
            Holder.__annotations__ = original_annotations
