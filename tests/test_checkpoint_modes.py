from __future__ import annotations

import pytest

from pyinc import (
    CheckpointModeError,
    Database,
    InMemoryArtifactStore,
    Input,
    query,
)

_MODES = ("strict", "checked", "fast")
_EXPECTED_TYPE = {"strict": "FrozenList", "checked": "list", "fast": "list"}


class _MutableMode(str):
    pass


@pytest.mark.parametrize("save_mode", _MODES)
@pytest.mark.parametrize("load_mode", _MODES)
def test_checkpoint_mode_matrix_refuses_cross_mode_reuse(
    save_mode: str, load_mode: str
) -> None:
    source = Input[list[int]]("checkpoint-mode-source")

    @query(key="checkpoint-mode-observer")
    def observed_type(db: Database) -> str:
        return type(source.read(db)).__name__

    store = InMemoryArtifactStore()
    writer = Database(mode=save_mode, store=store)
    writer.set(source, [1])
    assert writer.get(observed_type) == _EXPECTED_TYPE[save_mode]
    checkpoint = writer.save_checkpoint()

    reader = Database(mode=load_mode, store=store)
    reader.set(source, [1])
    if load_mode == save_mode:
        reader.load_checkpoint(checkpoint)
        assert reader.get(observed_type) == _EXPECTED_TYPE[load_mode]
        fresh = Database(mode=load_mode)
        fresh.set(source, [1])
        assert fresh.get(observed_type) == _EXPECTED_TYPE[load_mode]
    else:
        with pytest.raises(CheckpointModeError, match="cannot be loaded"):
            reader.load_checkpoint(checkpoint)
        assert reader.get(observed_type) == _EXPECTED_TYPE[load_mode]


@pytest.mark.parametrize("mode", _MODES)
def test_database_rejects_mode_subclasses_before_checkpoint_state_exists(mode: str) -> None:
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    checkpoint = writer.save_checkpoint()

    with pytest.raises(ValueError, match="mode must be one of"):
        Database(mode=_MutableMode(mode), store=store)

    # A normal exact-string reader still accepts the valid checkpoint.
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)
