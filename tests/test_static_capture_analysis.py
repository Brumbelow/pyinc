from __future__ import annotations

import pytest

from pyinc import Database, InMemoryArtifactStore, explain_query_captures, query

Mode = str

_DYNAMIC_NAMESPACE_STATE: dict[str, int] = {"value": 0}


@query(key="contract.dynamic-namespace-read")
def _dynamic_namespace_read(db: Database) -> int:
    state = globals()["_DYNAMIC_NAMESPACE_STATE"]
    if not isinstance(state, dict):
        raise TypeError("dynamic state must be a dictionary")
    value = state.get("value")
    if not isinstance(value, int):
        raise TypeError("dynamic state value must be an integer")
    return value


@query(key="contract.declared-dynamic-namespace-read")
def _declared_dynamic_namespace_read(db: Database) -> int:
    db.report_untracked_read("dynamic namespace lookup")
    state = globals()["_DYNAMIC_NAMESPACE_STATE"]
    if not isinstance(state, dict):
        raise TypeError("dynamic state must be a dictionary")
    value = state.get("value")
    if not isinstance(value, int):
        raise TypeError("dynamic state value must be an integer")
    return value


@pytest.mark.parametrize("mode", ("strict", "checked", "fast"))
def test_dynamic_globals_lookup_is_outside_static_capture_analysis_and_checkpoint_trust(
    mode: Mode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(globals(), "_DYNAMIC_NAMESPACE_STATE", {"value": 1})
    capture_names = {item.name for item in explain_query_captures(_dynamic_namespace_read)}
    assert "_DYNAMIC_NAMESPACE_STATE" not in capture_names

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    assert writer.get(_dynamic_namespace_read) == 1
    assert writer.get(_declared_dynamic_namespace_read) == 1
    checkpoint = writer.save_checkpoint()

    monkeypatch.setitem(globals(), "_DYNAMIC_NAMESPACE_STATE", {"value": 2})
    assert writer.get(_dynamic_namespace_read) == 1
    assert writer.inspect(_dynamic_namespace_read).last_decision == "reused"
    assert writer.get(_declared_dynamic_namespace_read) == 2

    fresh = Database(mode=mode)
    assert fresh.get(_dynamic_namespace_read) == 2

    warmed = Database(mode=mode, store=store)
    warmed.load_checkpoint(checkpoint)
    assert warmed.get(_dynamic_namespace_read) == 1
    assert warmed.inspect(_dynamic_namespace_read).last_decision == "reused"
    assert warmed.get(_declared_dynamic_namespace_read) == 2
