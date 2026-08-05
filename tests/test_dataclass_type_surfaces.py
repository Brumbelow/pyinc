from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from pyinc import Database, InMemoryArtifactStore, query

_MODES = ("strict", "checked", "fast")
_METADATA = {"tag": tuple([1])}


@dataclass
class _SurfaceDataclass:
    value: object = field(default=tuple([1]), metadata=_METADATA)
    plain: object = 1


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("surface", ("default", "metadata"))
def test_dataclass_field_surfaces_pin_equal_replacements_across_checkpoint(
    mode: str,
    surface: str,
) -> None:
    dataclass_field = _SurfaceDataclass.__dataclass_fields__["value"]
    dataclass_field.default = tuple([1])
    _METADATA["tag"] = tuple([1])

    @query(key=f"dataclass-field-surface-{surface}-{mode}")
    def field_identity(db: Database) -> int:
        del db
        current = _SurfaceDataclass.__dataclass_fields__["value"]
        if surface == "default":
            return id(current.default)
        return id(current.metadata["tag"])

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(field_identity) == Database(mode=mode).get(field_identity)
    checkpoint = writer.save_checkpoint()

    if surface == "default":
        dataclass_field.default = tuple([1])
        expected = id(dataclass_field.default)
    else:
        _METADATA["tag"] = tuple([1])
        expected = id(dataclass_field.metadata["tag"])

    assert writer.get(field_identity) == expected
    assert Database(mode=mode).get(field_identity) == expected

    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
    assert reader.get(field_identity) == expected
    assert reader.inspect(field_identity).last_recompute == "executed"
