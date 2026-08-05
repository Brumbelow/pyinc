from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    InMemoryArtifactStore,
    Input,
    ResourceDependencyError,
    query,
)
from pyinc.value import fingerprint_snapshot

_HOOK_INPUT = Input[int]("resource-hook-dependency")


@query(key="resource-hook-dependency-leaf")
def _hook_query(db: Database) -> int:
    return _HOOK_INPUT.read(db)


class _NestedResource:
    def identity(self) -> tuple[str]:
        return ("resource-hook-nested",)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        return f"nested[{key}]"

    def probe(self, key: str) -> str:
        return key

    def load(self, db: Database, key: str) -> str:
        return key


_NESTED_RESOURCE = _NestedResource()


class _HookResource:
    def __init__(
        self,
        phase: str,
        read_kind: str,
        target: Database,
        *,
        swallow: bool = False,
    ) -> None:
        self.phase = phase
        self.read_kind = read_kind
        self.target = target
        self.swallow = swallow
        self.armed = False
        self.version = 0

    def identity(self) -> tuple[str, str, str, bool]:
        self._attempt("identity")
        return ("resource-hook-contract", self.phase, self.read_kind, self.swallow)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        self._attempt("label")
        return f"hook[{key}]"

    def probe(self, key: str) -> tuple[str, int]:
        self._attempt("probe")
        return key, self.version

    def load(self, db: Database, key: str) -> str:
        self._attempt("load")
        return f"{key}:{self.version}"

    def _attempt(self, phase: str) -> None:
        if not self.armed or self.phase != phase:
            return
        try:
            if self.read_kind == "input":
                _HOOK_INPUT.read(self.target)
            elif self.read_kind == "query":
                _hook_query(self.target)
            elif self.read_kind == "resource":
                _NESTED_RESOURCE.read(self.target, "nested")
            elif self.read_kind == "mode":
                _ = self.target.mode
            else:  # pragma: no cover - construction is controlled by these tests
                raise AssertionError(self.read_kind)
        except ResourceDependencyError:
            if not self.swallow:
                raise


class _AtomicHookResource(_HookResource):
    def probe_and_load(self, db: Database, key: str) -> tuple[tuple[str, int], str]:
        self._attempt("probe_and_load")
        return (key, self.version), f"{key}:{self.version}"


def _resource_for(
    phase: str,
    read_kind: str,
    target: Database,
    *,
    swallow: bool = False,
) -> _HookResource:
    cls = _AtomicHookResource if phase == "probe_and_load" else _HookResource
    return cls(phase, read_kind, target, swallow=swallow)


def _record_state(db: Database) -> tuple[Any, ...]:
    records = []
    for key, record in db._records.items():
        if key.kind != "resource":
            continue
        records.append(
            (
                key,
                record.digest,
                record.changed_at,
                record.verified_at,
                frozenset(record.dependencies),
                record.last_decision,
                record.last_recompute,
                record.reason,
                tuple(record.untracked_reasons),
                fingerprint_snapshot(record.probe),
                record.checked_in_request,
                record.failure,
                record.probe_unconfirmed,
                record.checkpointable,
            )
        )
    return tuple(sorted(records, key=lambda item: item[0].label))


def _registration_state(db: Database) -> tuple[Any, ...]:
    registrations = []
    for key, registration in db._resource_objects().items():
        registrations.append(
            (
                key,
                fingerprint_snapshot(registration.parameter_snapshot),
                registration.parameter_type_digest,
            )
        )
    return tuple(sorted(registrations, key=lambda item: item[0].label))


def _error_text(call: Callable[[], Any]) -> tuple[type[BaseException], str]:
    with pytest.raises(ResourceDependencyError) as raised:
        call()
    return type(raised.value), str(raised.value)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("phase", ["identity", "label", "probe", "load", "probe_and_load"])
@pytest.mark.parametrize("read_kind", ["input", "query", "resource"])
@pytest.mark.parametrize("cross_database", [False, True])
def test_resource_hook_database_reads_fail_warm_and_fresh_without_state_changes(
    mode: str,
    phase: str,
    read_kind: str,
    cross_database: bool,
) -> None:
    warm = Database(mode=mode)
    warm_target = Database(mode=mode) if cross_database else warm
    warm_target.set(_HOOK_INPUT, 1)
    resource = _resource_for(phase, read_kind, warm_target)

    assert resource.read(warm, "value") == "value:0"
    before_revision = warm.revision
    before_records = _record_state(warm)
    before_registrations = _registration_state(warm)
    before_statistics = warm.statistics()

    resource.armed = True
    resource.version = 1
    warm_error = _error_text(lambda: resource.read(warm, "value"))

    assert warm.revision == before_revision
    assert _record_state(warm) == before_records
    assert _registration_state(warm) == before_registrations
    after_statistics = warm.statistics()
    assert after_statistics.resource_loads == before_statistics.resource_loads
    assert after_statistics.resource_probe_hits == before_statistics.resource_probe_hits

    fresh = Database(mode=mode)
    fresh_target = Database(mode=mode) if cross_database else fresh
    fresh_target.set(_HOOK_INPUT, 1)
    fresh_resource = _resource_for(phase, read_kind, fresh_target)
    fresh_resource.armed = True
    fresh_resource.version = 1
    fresh_error = _error_text(lambda: fresh_resource.read(fresh, "value"))

    assert warm_error == fresh_error
    assert not any(record.key.kind == "resource" for record in fresh._records.values())
    assert not fresh._resource_objects()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("phase", ["identity", "label", "probe", "load", "probe_and_load"])
def test_resource_hooks_cannot_catch_the_dependency_contract_error(
    mode: str,
    phase: str,
) -> None:
    db = Database(mode=mode)
    db.set(_HOOK_INPUT, 1)
    resource = _resource_for(phase, "input", db, swallow=True)
    resource.armed = True

    error_type, message = _error_text(lambda: resource.read(db, "value"))

    assert error_type is ResourceDependencyError
    assert f".{phase}() cannot call Database.read_input()" in message
    assert not any(record.key.kind == "resource" for record in db._records.values())


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_database_configuration_reads_are_forbidden_in_resource_hooks(mode: str) -> None:
    db = Database(mode=mode)
    resource = _resource_for("load", "mode", db)
    resource.armed = True

    error_type, message = _error_text(lambda: resource.read(db, "value"))

    assert error_type is ResourceDependencyError
    assert ".load() cannot call Database.mode" in message


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_caught_resource_dependency_marks_query_untracked_and_uncheckpointable(
    mode: str,
) -> None:
    db = Database(mode=mode)
    db.set(_HOOK_INPUT, 1)
    resource = _resource_for("load", "input", db)
    resource.armed = True

    @query(key=f"caught-resource-hook-dependency-{mode}")
    def root(current: Database) -> str:
        try:
            return resource.read(current, "value")
        except ResourceDependencyError:
            return "fallback"

    assert db.get(root) == "fallback"
    key, _ = db._query_key(root, (), {})
    record = db._records[key]
    assert record.is_untracked
    assert not record.checkpointable
    assert any(
        "forbidden read Database.read_input()" in reason for reason in record.untracked_reasons
    )

    store = InMemoryArtifactStore()
    checkpoint = db.save_checkpoint(store)
    manifest_bytes = store.get(checkpoint)
    assert manifest_bytes is not None
    manifest = json.loads(manifest_bytes)
    assert all(entry.get("query_id") != root.key for entry in manifest["records"])

    resource.armed = False
    assert db.get(root) == "value:0"

    fresh = Database(mode=mode)
    fresh.set(_HOOK_INPUT, 1)
    resource.target = fresh
    assert fresh.get(root) == "value:0"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("phase", ["probe", "load", "probe_and_load"])
@pytest.mark.parametrize("read_kind", ["input", "query", "resource"])
def test_checkpoint_resource_verification_preserves_the_hook_contract(
    mode: str,
    phase: str,
    read_kind: str,
) -> None:
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(_HOOK_INPUT, 1)
    resource = _resource_for(phase, read_kind, writer)

    @query(key=f"checkpoint-resource-hook-{mode}-{phase}-{read_kind}")
    def root(db: Database) -> str:
        return resource.read(db, "value")

    assert writer.get(root) == "value:0"
    checkpoint = writer.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.set(_HOOK_INPUT, 1)
    loaded.load_checkpoint(checkpoint)
    resource.target = loaded
    resource.armed = True
    resource.version = 1
    loaded_error = _error_text(lambda: loaded.get(root))

    assert not any(record.is_failed for record in loaded._records.values())

    fresh = Database(mode=mode)
    fresh.set(_HOOK_INPUT, 1)
    resource.target = fresh
    fresh_error = _error_text(lambda: fresh.get(root))

    assert loaded_error == fresh_error


class _FailureProbeDependencyResource:
    def __init__(self, target: Database) -> None:
        self.target = target

    def identity(self) -> tuple[str]:
        return ("resource-hook-failure-probe",)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        return f"failure-probe[{key}]"

    def probe(self, key: str) -> tuple[str]:
        _HOOK_INPUT.read(self.target)
        return (key,)

    def load(self, db: Database, key: str) -> str:
        raise AssertionError("the atomic hook must be used")

    def probe_and_load(self, db: Database, key: str) -> tuple[tuple[str], str]:
        raise ValueError("load failed before its failure probe")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_forbidden_failure_probe_read_wins_over_the_load_error(mode: str) -> None:
    db = Database(mode=mode)
    db.set(_HOOK_INPUT, 1)
    resource = _FailureProbeDependencyResource(db)

    error_type, message = _error_text(lambda: resource.read(db, "value"))

    assert error_type is ResourceDependencyError
    assert ".probe() cannot call Database.read_input()" in message
    assert not any(record.key.kind == "resource" for record in db._records.values())


class _RawIOResource:
    def identity(self) -> tuple[str]:
        return ("resource-hook-raw-io",)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        return f"raw-io[{key}]"

    def probe(self, key: str) -> str:
        return Path(key).read_text(encoding="utf-8")

    def load(self, db: Database, key: str) -> str:
        return Path(key).read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_hooks_may_observe_raw_external_state(mode: str, tmp_path: Path) -> None:
    path = tmp_path / "value.txt"
    path.write_text("first", encoding="utf-8")
    db = Database(mode=mode)
    resource = _RawIOResource()

    assert resource.read(db, str(path)) == "first"
    path.write_text("second", encoding="utf-8")
    assert resource.read(db, str(path)) == "second"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_queries_may_compose_inputs_queries_and_resources(mode: str) -> None:
    db = Database(mode=mode)
    db.set(_HOOK_INPUT, 2)
    resource = _resource_for("load", "input", db)

    @query(key=f"resource-hook-positive-composition-{mode}")
    def root(current: Database) -> tuple[int, int, str]:
        return (
            _HOOK_INPUT.read(current),
            _hook_query(current),
            resource.read(current, "value"),
        )

    assert db.get(root) == (2, 2, "value:0")


class _CaughtDeferredMapping(Mapping[str, int]):
    def __init__(self, target: Database) -> None:
        self.target = target

    def __getitem__(self, key: str) -> int:
        if key != "value":
            raise KeyError(key)
        return 1

    def __iter__(self) -> Iterator[str]:
        return iter(("value",))

    def __len__(self) -> int:
        return 1

    def items(self) -> Any:
        with suppress(ResourceDependencyError):
            _HOOK_INPUT.read(self.target)
        return {"value": 1}.items()


class _DeferredLoadResource:
    def __init__(self, target: Database) -> None:
        self.target = target
        self.armed = False
        self.version = 0

    def identity(self) -> tuple[str]:
        return ("resource-hook-deferred-load",)

    def read(self, db: Database, key: str) -> Any:
        return db.read_resource(self, key)

    def label(self, key: str) -> str:
        return f"deferred-load[{key}]"

    def probe(self, key: str) -> tuple[str, int]:
        return key, self.version

    def load(self, db: Database, key: str) -> Any:
        if self.armed:
            return _CaughtDeferredMapping(self.target)
        return {"value": 0}


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_resource_result_materialization_cannot_defer_a_caught_database_read(
    mode: str,
) -> None:
    warm = Database(mode=mode)
    warm.set(_HOOK_INPUT, 1)
    resource = _DeferredLoadResource(warm)
    assert resource.read(warm, "value") is not None
    resource.armed = True
    resource.version = 1
    warm_error = _error_text(lambda: resource.read(warm, "value"))

    fresh = Database(mode=mode)
    fresh.set(_HOOK_INPUT, 1)
    fresh_resource = _DeferredLoadResource(fresh)
    fresh_resource.armed = True
    fresh_resource.version = 1
    fresh_error = _error_text(lambda: fresh_resource.read(fresh, "value"))

    assert warm_error == fresh_error
    assert ".load() cannot call Database.read_input()" in warm_error[1]
