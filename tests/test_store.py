from __future__ import annotations

import errno
import multiprocessing
import os
import struct
import subprocess
from pathlib import Path
from queue import Empty
from typing import Any, TypeAlias, cast

import pytest

from pyinc import (
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStoreKeyError,
    ArtifactStoreLockError,
    Database,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Input,
    PyIncError,
    deserialize_snapshot,
    freeze,
    query,
    serialize_snapshot,
)
from pyinc import (
    _safe_fs as safe_fs_module,
)
from pyinc._locking import FileLock
from pyinc._safe_fs import (
    _WIN_FILE_SHARE_DELETE,
    _WIN_STABLE_SHARE_MODE,
    _windows_path_prefixes,
    _windows_rename_information,
    _WindowsDirectoryHandles,
)
from pyinc.value import fingerprint_snapshot  # not re-exported from pyinc

_ExceptionDiagnostic: TypeAlias = tuple[str, str, int | None, int | None, str | None]
_WorkerResult: TypeAlias = tuple[str, tuple[_ExceptionDiagnostic, ...]]


def _filesystem_put_worker(
    root: str,
    digest: str,
    payload: bytes,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    try:
        store = FileSystemArtifactStore(root)
        ready.set()
        if not start.wait(timeout=15):
            raise TimeoutError("Parent did not release the artifact-store worker.")
        store.put(digest, payload)
    except Exception as error:  # noqa: BLE001 - cross-process result transport
        ready.set()
        diagnostics = _exception_diagnostics(error)
        results.put((diagnostics[0][0], diagnostics))
    else:
        results.put(("ok", ()))


def _exception_diagnostics(error: BaseException) -> tuple[_ExceptionDiagnostic, ...]:
    diagnostics: list[_ExceptionDiagnostic] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        filename = getattr(current, "filename", None)
        diagnostics.append(
            (
                type(current).__name__,
                str(current),
                getattr(current, "errno", None),
                getattr(current, "winerror", None),
                str(filename) if filename is not None else None,
            )
        )
        next_error = current.__cause__
        if next_error is None and not current.__suppress_context__:
            next_error = current.__context__
        current = next_error
    return tuple(diagnostics)


def _run_filesystem_put_workers(
    root: Path,
    digest: str,
    payloads: tuple[bytes, ...],
) -> list[_WorkerResult]:
    context: Any = multiprocessing.get_context("spawn" if os.name == "nt" else "fork")
    ready = [context.Event() for _ in payloads]
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_filesystem_put_worker,
            args=(str(root), digest, payload, worker_ready, start, results),
        )
        for payload, worker_ready in zip(payloads, ready, strict=True)
    ]
    started: list[Any] = []
    reports: list[_WorkerResult] = []
    try:
        for process in processes:
            process.start()
            started.append(process)
        for worker_ready in ready:
            assert worker_ready.wait(timeout=15), [process.exitcode for process in started]
        start.set()
        for _ in started:
            try:
                reports.append(results.get(timeout=35))
            except Empty as error:
                raise AssertionError(
                    "Artifact-store worker did not report; "
                    f"exit codes: {[process.exitcode for process in started]}"
                ) from error
        for process in started:
            process.join(timeout=5)
            assert process.exitcode == 0, reports
        return reports
    finally:
        start.set()
        for process in started:
            if process.is_alive():
                process.terminate()
        for process in started:
            process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
        results.close()
        results.join_thread()
        for process in started:
            if process.exitcode is not None:
                process.close()


# ---------------------------------------------------------------------------
# Group A: InMemoryArtifactStore protocol
# ---------------------------------------------------------------------------


def test_in_memory_store_round_trips_payload() -> None:
    store = InMemoryArtifactStore()
    payload = serialize_snapshot(freeze({"a": 1}))
    digest = fingerprint_snapshot(freeze({"a": 1}))

    store.put(digest, payload)

    assert store.get(digest) == payload
    assert store.contains(digest) is True


def test_in_memory_store_returns_none_for_missing_digest() -> None:
    store = InMemoryArtifactStore()
    assert store.get("0" * 64) is None
    assert store.contains("0" * 64) is False


def test_in_memory_store_idempotent_put_with_same_bytes() -> None:
    store = InMemoryArtifactStore()
    payload = b"K2;N;"
    digest = "abc"
    store.put(digest, payload)
    store.put(digest, payload)  # idempotent — must not raise
    assert store.get(digest) == payload


def test_in_memory_store_collision_raises_value_error() -> None:
    store = InMemoryArtifactStore()
    digest = "abc"
    store.put(digest, b"first")
    with pytest.raises(ValueError, match="collision"):
        store.put(digest, b"second")


def test_in_memory_store_satisfies_artifact_store_protocol() -> None:
    store: ArtifactStore = InMemoryArtifactStore()
    assert hasattr(store, "get")
    assert hasattr(store, "put")
    assert hasattr(store, "contains")


class _MinimalProtocolStore(ArtifactStore):
    """Explicit protocol subclass implementing only `get` and `put`.

    `contains` is deliberately left inherited so the protocol's documented
    default is what gets exercised, here and through a real database.
    """

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        existing = self._items.get(digest)
        if existing is not None:
            if existing != payload:
                raise ValueError(f"Digest collision for {digest!r}.")
            return
        self._items[digest] = payload


class _ContainsOnlyStore(ArtifactStore):
    """Explicit protocol subclass that overrides only `contains`.

    Stands in for an implementation whose author read the protocol as a set
    of optional hooks: it answers presence questions and never defines the
    two methods that would make an answer of `True` mean anything.
    """

    def contains(self, digest: str) -> bool:
        return False


def test_protocol_contains_default_matches_get() -> None:
    store = _MinimalProtocolStore()
    payload = serialize_snapshot(freeze({"a": 1}))
    digest = fingerprint_snapshot(freeze({"a": 1}))

    store.put(digest, payload)

    assert store.contains(digest) is True
    assert store.contains("0" * 64) is False


def test_protocol_stub_get_raises_instead_of_returning_none() -> None:
    store = _ContainsOnlyStore()  # type: ignore[abstract]

    # A subclass that skips `get`/`put` is broken, not empty: reading has to
    # fail where the omission is, not hand back a plausible `None`.
    with pytest.raises(NotImplementedError):
        store.get("0" * 64)
    with pytest.raises(NotImplementedError):
        store.put("0" * 64, b"x")


def test_in_memory_store_keys_view_is_read_only() -> None:
    store = InMemoryArtifactStore()
    payload = serialize_snapshot(freeze({"a": 1}))
    digest = fingerprint_snapshot(freeze({"a": 1}))
    store.put(digest, payload)

    assert store.keys() is not store._items
    with pytest.raises(TypeError):
        store.keys()["x"] = b"y"  # type: ignore[index]
    with pytest.raises(TypeError):
        del store.keys()[digest]  # type: ignore[attr-defined]

    # Neither attempt reached the backing map, so the collision guard still
    # compares against the original bytes instead of whatever was smuggled in.
    with pytest.raises(ValueError, match="collision"):
        store.put(digest, b"different")
    assert store.get(digest) == payload


# ---------------------------------------------------------------------------
# Group B: FileSystemArtifactStore
# ---------------------------------------------------------------------------


def test_filesystem_store_writes_under_fanout_layout(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")

    expected_path = tmp_path / "objects" / "ab" / ("c" * 62)
    assert expected_path.exists()
    assert expected_path.read_bytes() == b"K2;N;"


def test_filesystem_store_get_returns_none_for_missing_digest(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    assert store.get("0" * 64) is None


def test_filesystem_store_round_trip(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    payload = serialize_snapshot(freeze([1, 2, 3]))
    digest = fingerprint_snapshot(freeze([1, 2, 3]))

    store.put(digest, payload)

    retrieved = store.get(digest)
    assert retrieved == payload
    assert deserialize_snapshot(retrieved) == freeze([1, 2, 3])


def test_filesystem_store_idempotent_put_same_bytes(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")
    store.put(digest, b"K2;N;")
    assert store.get(digest) == b"K2;N;"


def test_filesystem_store_collision_raises_value_error(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"first")
    with pytest.raises(ValueError, match="collision"):
        store.put(digest, b"different")


def test_filesystem_store_persists_across_instances(tmp_path: Path) -> None:
    digest = fingerprint_snapshot(freeze({"key": "value"}))
    payload = serialize_snapshot(freeze({"key": "value"}))

    writer = FileSystemArtifactStore(tmp_path)
    writer.put(digest, payload)

    reader = FileSystemArtifactStore(tmp_path)
    assert reader.get(digest) == payload


# ---------------------------------------------------------------------------
# Group C: Database integration
# ---------------------------------------------------------------------------


def test_database_writes_input_snapshot_to_store() -> None:
    payload = Input[dict[str, int]]("p")
    store = InMemoryArtifactStore()
    db = Database(store=store)

    db.set(payload, {"x": 1})

    digest = fingerprint_snapshot(freeze({"x": 1}))
    assert store.contains(digest)
    assert deserialize_snapshot(store.get(digest)) == freeze({"x": 1})  # type: ignore[arg-type]


def test_database_writes_query_result_snapshot_to_store() -> None:
    payload = Input[int]("seed")
    store = InMemoryArtifactStore()

    @query
    def double(db: Database) -> int:
        return payload.read(db) * 2

    db = Database(store=store)
    db.set(payload, 21)

    assert db.get(double) == 42

    # Both the input snapshot (21) and the result snapshot (42) are persisted.
    assert store.contains(fingerprint_snapshot(freeze(21)))
    assert store.contains(fingerprint_snapshot(freeze(42)))


def test_database_with_no_store_writes_nothing() -> None:
    payload = Input[int]("p")

    @query
    def echo(db: Database) -> int:
        return payload.read(db)

    db = Database()  # store=None default
    db.set(payload, 7)
    assert db.get(echo) == 7  # No errors despite no store.


def test_database_lru_eviction_does_not_remove_from_store() -> None:
    p1 = Input[int]("a")
    p2 = Input[int]("b")
    store = InMemoryArtifactStore()

    @query
    def q1(db: Database) -> int:
        return p1.read(db)

    @query
    def q2(db: Database) -> int:
        return p2.read(db)

    db = Database(store=store, max_query_nodes=1)
    db.set(p1, 100)
    db.set(p2, 200)

    assert db.get(q1) == 100
    assert db.get(q2) == 200  # evicts q1's memo

    # Both result snapshots remain in the store.
    assert store.contains(fingerprint_snapshot(freeze(100)))
    assert store.contains(fingerprint_snapshot(freeze(200)))


def test_database_filesystem_store_writes_through_raw_open_guard(
    tmp_path: Path,
) -> None:
    payload = Input[str]("p")
    store = FileSystemArtifactStore(tmp_path / "store")
    db = Database(store=store)

    db.set(payload, "hello")

    digest = fingerprint_snapshot(freeze("hello"))
    assert store.get(digest) is not None


# ---------------------------------------------------------------------------
# Group D: Phase 1 (mutable graphs) × Phase 2 (storage) composition
# ---------------------------------------------------------------------------


def test_filesystem_store_round_trips_cyclic_graph(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path)
    payload_obj: list[object] = []
    payload_obj.append(payload_obj)

    snapshot = freeze(payload_obj)
    bytes_payload = serialize_snapshot(snapshot)
    digest = fingerprint_snapshot(snapshot)

    store.put(digest, bytes_payload)
    retrieved = store.get(digest)
    assert retrieved == bytes_payload
    assert deserialize_snapshot(retrieved) == snapshot


def test_database_persists_shared_identity_input_to_store() -> None:
    payload = Input[tuple[dict[str, int], dict[str, int]]]("p")
    store = InMemoryArtifactStore()
    db = Database(store=store)

    shared = {"x": 1}
    db.set(payload, (shared, shared))

    digest = fingerprint_snapshot(freeze((shared, shared)))
    assert store.contains(digest)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_database_with_store_under_each_mode_round_trips(mode: str, tmp_path: Path) -> None:
    payload = Input[dict[str, int]]("p")
    store = FileSystemArtifactStore(tmp_path)

    @query
    def echo_x(db: Database) -> int:
        return payload.read(db)["x"]

    db = Database(mode=mode, store=store)
    db.set(payload, {"x": 7})
    assert db.get(echo_x) == 7

    digest = fingerprint_snapshot(freeze({"x": 7}))
    retrieved = store.get(digest)
    assert retrieved is not None
    assert deserialize_snapshot(retrieved) == freeze({"x": 7})


# ---------------------------------------------------------------------------
# Group E: Atomic write behavior
# ---------------------------------------------------------------------------


def test_filesystem_store_atomic_write_uses_temporary_file(tmp_path: Path) -> None:
    """The temporary file must be in the same directory as the target so that
    `os.replace` is guaranteed to be atomic across all common filesystems."""
    store = FileSystemArtifactStore(tmp_path)
    digest = "ab" + "c" * 62
    store.put(digest, b"K2;N;")

    target_dir = tmp_path / "objects" / "ab"
    # No temp files should remain after the put completes.
    leftover = [
        name
        for name in os.listdir(target_dir)
        if name.startswith(".tmp-") or name.startswith("tmp")
    ]
    assert leftover == []


@pytest.mark.parametrize(
    "key",
    (
        "",
        "abc",
        "0" * 63,
        "A" * 64,
        "../" + "0" * 64,
        "0/" + "0" * 62,
        "C:\\" + "0" * 64,
        "ck" + "0" * 63,
    ),
)
def test_filesystem_store_rejects_malformed_or_escaping_keys(tmp_path: Path, key: str) -> None:
    store = FileSystemArtifactStore(tmp_path)
    with pytest.raises(ArtifactStoreKeyError):
        store.get(key)
    with pytest.raises(ArtifactStoreKeyError):
        store.put(key, b"payload")
    with pytest.raises(ArtifactStoreKeyError):
        store.contains(key)


def test_filesystem_store_rejects_digest_string_subclasses(tmp_path: Path) -> None:
    class EvilDigest(str):
        def __getitem__(self, key: object) -> str:
            if isinstance(key, slice) and key.stop == 2:
                return ".."
            return "victim"

    store = FileSystemArtifactStore(tmp_path)
    digest = EvilDigest("a" * 64)

    with pytest.raises(ArtifactStoreKeyError):
        store.put(digest, b"payload")
    assert not (tmp_path / "victim").exists()
    assert not (tmp_path / "victim.lock").exists()


def test_filesystem_store_wraps_invalid_root_as_typed_error() -> None:
    with pytest.raises(ArtifactStoreError, match="root path is invalid"):
        FileSystemArtifactStore("bad\0root")


def test_a_resolve_that_fails_still_produces_a_typed_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Resolving a looping path raised on the interpreters this library still
    # supports and stopped raising on the newer ones, so the handler is driven
    # directly rather than through a shape only some interpreters produce.
    # Patching resolve is class-wide, so every witness is taken before it is
    # armed and the arming lasts exactly as long as the call under test.
    root = tmp_path / "store"
    before = sorted(entry.name for entry in tmp_path.iterdir())

    def raising_resolve(self: Path, strict: bool = False) -> Path:
        raise RuntimeError("Symlink loop from " + str(self))

    monkeypatch.setattr(Path, "resolve", raising_resolve)
    try:
        with pytest.raises(
            ArtifactStoreError, match="Artifact-store root path is invalid"
        ) as caught:
            FileSystemArtifactStore(root)
    finally:
        monkeypatch.undo()

    assert isinstance(caught.value, PyIncError)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert sorted(entry.name for entry in tmp_path.iterdir()) == before


def test_windows_trusted_path_plan_uses_extended_component_prefixes() -> None:
    assert _windows_path_prefixes(r"C:\store\objects\aa") == (
        "\\\\?\\C:\\",
        "\\\\?\\C:\\store",
        "\\\\?\\C:\\store\\objects",
        "\\\\?\\C:\\store\\objects\\aa",
    )
    assert _windows_path_prefixes(r"\\server\share\store") == (
        "\\\\?\\UNC\\server\\share\\",
        "\\\\?\\UNC\\server\\share\\store",
    )
    assert not _WIN_STABLE_SHARE_MODE & _WIN_FILE_SHARE_DELETE


@pytest.mark.parametrize(
    "path",
    (
        r"relative\file",
        r"C:\root\..\escape",
        r"\\.\C:\device",
        r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1\file",
        r"\\?\PIPE\service",
    ),
)
def test_windows_trusted_path_plan_rejects_unsafe_namespaces(path: str) -> None:
    with pytest.raises(OSError):
        _windows_path_prefixes(path)


def test_windows_rename_information_uses_full_utf16_target() -> None:
    payload, filename_offset = _windows_rename_information(
        "C:\\g\N{LATIN SMALL LETTER E WITH ACUTE}n\\model.py"
    )
    assert struct.unpack_from("<I", payload, 0) == (1,)
    assert payload[filename_offset:-2].decode("utf-16-le") == (
        "\\\\?\\C:\\g\N{LATIN SMALL LETTER E WITH ACUTE}n\\model.py"
    )
    assert payload[-2:] == b"\0\0"


def test_windows_directory_handles_stay_open_until_operation_finishes() -> None:
    class FakeWindowsApi:
        def __init__(self) -> None:
            self.opened: list[str] = []
            self.closed: list[int] = []

        def open_directory(self, path: str) -> int:
            self.opened.append(path)
            return len(self.opened)

        def require_directory(self, handle: int, path: str) -> None:
            assert handle == len(self.opened)
            assert path == self.opened[-1]

        def create_directory(self, path: str) -> None:
            raise AssertionError(f"unexpected create: {path}")

        def close(self, handle: int) -> None:
            self.closed.append(handle)

    api = FakeWindowsApi()
    handles = _WindowsDirectoryHandles.open(
        api,  # type: ignore[arg-type]
        r"C:\store\objects\aa",
        create=False,
    )
    assert api.closed == []
    assert len(handles.handles) == 4

    handles.close()

    assert api.closed == [4, 3, 2, 1]


def test_worker_exception_diagnostics_preserve_operating_system_details() -> None:
    cause = OSError(errno.EMFILE, "too many open files", "store.lock")
    error = ArtifactStoreError("outer failure")
    error.__cause__ = cause

    diagnostics = _exception_diagnostics(error)

    assert diagnostics[0] == ("ArtifactStoreError", "outer failure", None, None, None)
    assert diagnostics[1][0] == "OSError"
    assert diagnostics[1][2:] == (errno.EMFILE, None, "store.lock")


def test_filesystem_store_serializes_equal_cross_process_puts(tmp_path: Path) -> None:
    digest = "a" * 64
    reports = _run_filesystem_put_workers(tmp_path, digest, (b"same", b"same"))

    assert sorted(report[0] for report in reports) == ["ok", "ok"], reports
    assert FileSystemArtifactStore(tmp_path).get(digest) == b"same"


def test_filesystem_store_refuses_cross_process_conflicting_bytes(tmp_path: Path) -> None:
    digest = "b" * 64
    reports = _run_filesystem_put_workers(tmp_path, digest, (b"first", b"second"))

    assert sorted(report[0] for report in reports) == ["ValueError", "ok"], reports
    collision = next(report for report in reports if report[0] == "ValueError")
    assert collision[1][0][0] == "ValueError"
    assert "collision" in collision[1][0][1].lower()
    assert FileSystemArtifactStore(tmp_path).get(digest) in {b"first", b"second"}


def test_filesystem_store_lock_timeout_is_typed(tmp_path: Path) -> None:
    digest = "c" * 64
    store = FileSystemArtifactStore(tmp_path, lock_timeout=0)
    with FileLock(store._lock_path_for(digest), timeout=0), pytest.raises(ArtifactStoreLockError):
        store.put(digest, b"payload")


def test_filesystem_store_rejects_nonregular_lock_path_with_typed_error(
    tmp_path: Path,
) -> None:
    digest = "e" * 64
    store = FileSystemArtifactStore(tmp_path)
    lock_path = store._lock_path_for(digest)
    lock_path.parent.mkdir()
    try:
        lock_path.symlink_to(tmp_path / "outside-lock")
    except OSError:
        pytest.skip("symlink support is unavailable")

    with pytest.raises(ArtifactStoreError, match="artifact lock"):
        store.put(digest, b"payload")


def test_filesystem_store_interrupted_publish_leaves_no_partial_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = "d" * 64
    store = FileSystemArtifactStore(tmp_path)

    if os.name == "nt":
        api_type = type(safe_fs_module._windows_api())

        def fail_rename(_self: Any, _handle: int, _destination: str) -> None:
            raise OSError("interrupted")

        monkeypatch.setattr(api_type, "rename_handle", fail_rename)
    else:

        def fail_replace(source: str, destination: str, **kwargs: object) -> None:
            del source, destination, kwargs
            raise OSError("interrupted")

        monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted"):
        store.put(digest, b"payload")

    target = tmp_path / "objects" / "dd" / ("d" * 62)
    assert not target.exists()
    assert list(target.parent.glob(".tmp-*")) == []


@pytest.mark.parametrize("timeout", (float("nan"), float("inf"), -float("inf")))
def test_filesystem_store_rejects_nonfinite_lock_timeout(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        FileSystemArtifactStore(tmp_path, lock_timeout=timeout)


def test_filesystem_store_rejects_symlinked_object_fanout(tmp_path: Path) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    store = FileSystemArtifactStore(root)
    outside.mkdir()
    try:
        (root / "objects" / "aa").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links are not available")

    with pytest.raises(ArtifactStoreError, match="not a directory"):
        store.put("a" * 64, b"payload")
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="requires a Windows junction")
def test_windows_store_rejects_junctioned_object_fanout(tmp_path: Path) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    store = FileSystemArtifactStore(root)
    outside.mkdir()
    junction = root / "objects" / "77"
    created = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        check=False,
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"junction creation is unavailable: {created.stderr.strip()}")
    try:
        with pytest.raises(ArtifactStoreError):
            store.put("7" * 64, b"payload")
        assert list(outside.iterdir()) == []
    finally:
        junction.rmdir()


def test_filesystem_store_rejects_symlinked_object_target(tmp_path: Path) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside.bin"
    store = FileSystemArtifactStore(root)
    fanout = root / "objects" / "bb"
    fanout.mkdir()
    outside.write_bytes(b"outside")
    target = fanout / ("b" * 62)
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are not available")

    with pytest.raises(ArtifactStoreError, match="not a regular file"):
        store.get("b" * 64)
    with pytest.raises(ArtifactStoreError, match="not a regular file"):
        store.put("b" * 64, b"payload")
    assert outside.read_bytes() == b"outside"


def test_filesystem_store_parent_swap_cannot_redirect_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    moved = tmp_path / "moved-fanout"
    outside.mkdir()
    store = FileSystemArtifactStore(root)
    store_module = __import__("pyinc.store", fromlist=["atomic_write"])
    original_atomic_write = store_module.atomic_write

    def swap_then_write(target: Path, data: bytes) -> None:
        target.parent.rename(moved)
        try:
            target.parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symbolic links are not available")
        original_atomic_write(target, data)

    monkeypatch.setattr(store_module, "atomic_write", swap_then_write)
    with pytest.raises(ArtifactStoreError):
        store.put("e" * 64, b"blocked")
    assert list(outside.iterdir()) == []
    assert list(moved.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory-descriptor behavior")
def test_filesystem_store_rejects_opened_parent_rename_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store"
    outside = tmp_path / "outside"
    moved = outside / "moved-fanout"
    outside.mkdir()
    store = FileSystemArtifactStore(root)
    original = safe_fs_module._require_regular_or_missing
    raced = False

    def rename_after_validation(descriptor: int, name: str, path: Path) -> None:
        nonlocal raced
        original(descriptor, name, path)
        if len(name) == 62 and not raced:
            raced = True
            path.parent.rename(moved)

    monkeypatch.setattr(safe_fs_module, "_require_regular_or_missing", rename_after_validation)
    with pytest.raises(ArtifactStoreError, match="trusted path"):
        store.put("e" * 64, b"blocked")

    assert raced
    assert not (moved / ("e" * 62)).exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle sharing")
def test_windows_store_publish_replaces_a_racing_target_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyinc import _safe_fs as safe_fs_module

    root = tmp_path / "store"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    probe = tmp_path / "symlink-probe"
    try:
        probe.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symbolic-link creation is unavailable")
    probe.unlink()

    digest = "f" * 64
    store = FileSystemArtifactStore(root)
    target = root / "objects" / "ff" / ("f" * 62)
    api = safe_fs_module._windows_api()
    api_type = type(api)
    original_rename = api_type.rename_handle
    injected = False

    def race_target(self: Any, handle: int, destination: str) -> None:
        nonlocal injected
        if Path(destination) == target and not injected:
            target.symlink_to(outside)
            injected = True
        original_rename(self, handle, destination)

    monkeypatch.setattr(api_type, "rename_handle", race_target)
    store.put(digest, b"payload")

    assert injected
    assert not target.is_symlink()
    assert target.read_bytes() == b"payload"
    assert outside.read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle sharing")
def test_windows_store_holds_fanout_against_a_directory_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pyinc import _safe_fs as safe_fs_module

    root = tmp_path / "store"
    moved = tmp_path / "moved-fanout"
    store = FileSystemArtifactStore(root)
    digest = "9" * 64
    fanout = root / "objects" / "99"
    api = safe_fs_module._windows_api()
    api_type = type(api)
    original_rename = api_type.rename_handle
    swap_blocked = False

    def race_parent(self: Any, handle: int, destination: str) -> None:
        nonlocal swap_blocked
        if Path(destination).parent == fanout:
            try:
                fanout.rename(moved)
            except OSError:
                swap_blocked = True
        original_rename(self, handle, destination)

    monkeypatch.setattr(api_type, "rename_handle", race_parent)
    store.put(digest, b"payload")

    assert swap_blocked
    assert store.get(digest) == b"payload"
    assert not moved.exists()


@pytest.mark.skipif(os.name != "nt", reason="requires Win32 handle sharing")
def test_windows_lock_holds_its_parent_against_a_junction_swap(tmp_path: Path) -> None:
    store = FileSystemArtifactStore(tmp_path / "store")
    lock_path = store._lock_path_for("8" * 64)
    moved = tmp_path / "moved-lock-parent"
    lock = FileLock(lock_path, timeout=0)

    with lock, pytest.raises(OSError):
        lock_path.parent.rename(moved)

    assert not moved.exists()


# ---------------------------------------------------------------------------
# Group F: Scope-B checkpoint API (save_checkpoint / load_checkpoint)
# ---------------------------------------------------------------------------


def test_checkpoint_save_requires_store() -> None:
    db = Database()
    with pytest.raises(ValueError, match="ArtifactStore"):
        db.save_checkpoint()


def test_checkpoint_load_requires_store() -> None:
    db = Database()
    with pytest.raises(ValueError, match="ArtifactStore"):
        db.load_checkpoint("ck" + "0" * 64)


def test_checkpoint_load_missing_key_raises_key_error() -> None:
    store = InMemoryArtifactStore()
    db = Database(store=store)
    with pytest.raises(KeyError):
        db.load_checkpoint("ck" + "0" * 64)


def test_checkpoint_basic_round_trip() -> None:
    p = Input[int]("ckp_num")

    @query
    def ckp_doubled(db: Database) -> int:
        return p.read(db) * 2

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 21)
    assert db1.get(ckp_doubled) == 42

    ck_key = db1.save_checkpoint()
    assert ck_key.startswith("ck")

    db2 = Database(store=store)
    db2.set(p, 21)
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_doubled) == 42

    node = db2.inspect(ckp_doubled)
    assert node.last_recompute == "reused"


def test_checkpoint_invalidated_by_changed_input() -> None:
    p = Input[int]("ckp_seed")

    @query
    def ckp_tripled(db: Database) -> int:
        return p.read(db) * 3

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 7)
    assert db1.get(ckp_tripled) == 21
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 8)  # different input
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_tripled) == 24  # re-executed with new input

    node = db2.inspect(ckp_tripled)
    assert node.last_recompute == "executed"


def test_checkpoint_key_is_content_addressed() -> None:
    p = Input[int]("ckp_x")

    @query
    def ckp_identity(db: Database) -> int:
        return p.read(db)

    store = InMemoryArtifactStore()

    db1 = Database(store=store)
    db1.set(p, 5)
    db1.get(ckp_identity)
    key1 = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 5)
    db2.get(ckp_identity)
    key2 = db2.save_checkpoint()

    assert key1 == key2


def test_checkpoint_filesystem_store_cross_instance(tmp_path: Path) -> None:
    p = Input[str]("ckp_text")

    @query
    def ckp_upper(db: Database) -> str:
        return p.read(db).upper()

    store = FileSystemArtifactStore(tmp_path)
    db1 = Database(store=store)
    db1.set(p, "hello")
    assert db1.get(ckp_upper) == "HELLO"
    ck_key = db1.save_checkpoint()

    store2 = FileSystemArtifactStore(tmp_path)
    db2 = Database(store=store2)
    db2.set(p, "hello")
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_upper) == "HELLO"
    assert db2.inspect(ckp_upper).last_recompute == "reused"


def test_checkpoint_chain_of_queries() -> None:
    p = Input[int]("ckp_base")

    @query
    def ckp_step1(db: Database) -> int:
        return p.read(db) + 1

    @query
    def ckp_step2(db: Database) -> int:
        return ckp_step1(db) * 10

    store = InMemoryArtifactStore()
    db1 = Database(store=store)
    db1.set(p, 4)
    assert db1.get(ckp_step2) == 50  # (4+1)*10
    ck_key = db1.save_checkpoint()

    db2 = Database(store=store)
    db2.set(p, 4)
    db2.load_checkpoint(ck_key)
    assert db2.get(ckp_step2) == 50

    assert db2.inspect(ckp_step2).last_recompute == "reused"
    assert db2.inspect(ckp_step1).last_recompute == "reused"


class _PutCountingStore(InMemoryArtifactStore):
    """In-memory store that records the digest of every `put` it is handed."""

    def __init__(self) -> None:
        super().__init__()
        self.put_log: list[str] = []

    def put(self, digest: str, payload: bytes) -> None:
        self.put_log.append(digest)
        super().put(digest, payload)


def test_write_through_store_raises_on_preseeded_wrong_bytes() -> None:
    @query
    def write_through_constant(db: Database) -> int:
        return 4242

    digest = fingerprint_snapshot(freeze(4242))
    store = InMemoryArtifactStore()
    store.put(digest, b"wrong bytes")

    # The write-through path persists a query's result as it is produced, so
    # the store's refusal has to surface out of `get` rather than being
    # swallowed by a presence check on bytes that would never decode.
    db = Database(store=store)
    with pytest.raises(ValueError, match="Digest collision"):
        db.get(write_through_constant)

    assert store.get(digest) == b"wrong bytes"


def test_correct_preseed_is_accepted_without_a_new_put() -> None:
    p = Input[int]("preseed_correct")

    @query
    def preseed_correct_query(db: Database) -> int:
        return p.read(db) + 77

    victim = fingerprint_snapshot(freeze(77))
    store = _PutCountingStore()
    store.put(victim, serialize_snapshot(freeze(77)))
    store.put_log.clear()

    db = Database(store=store)
    db.set(p, 0)
    assert db.get(preseed_correct_query) == 77
    ck_key = db.save_checkpoint()

    # Bytes that already match are left alone: the digest is never re-`put`.
    assert victim not in store.put_log

    # A second identical save republishes no snapshot. The manifest key is
    # written unconditionally and is idempotent on equal bytes, so it is not
    # part of the count.
    snapshot_puts = [d for d in store.put_log if not d.startswith("ck")]
    assert db.save_checkpoint() == ck_key
    assert [d for d in store.put_log if not d.startswith("ck")] == snapshot_puts


def test_checkpoint_store_passed_to_save_and_load_directly() -> None:
    p = Input[int]("ckp_direct")

    @query
    def ckp_sq(db: Database) -> int:
        return p.read(db) ** 2

    store = InMemoryArtifactStore()
    db1 = Database()  # no store configured
    db1.set(p, 6)
    db1.get(ckp_sq)
    ck_key = db1.save_checkpoint(store=store)

    db2 = Database()  # no store configured
    db2.set(p, 6)
    db2.load_checkpoint(ck_key, store=store)
    assert db2.get(ckp_sq) == 36
    assert db2.inspect(ckp_sq).last_recompute == "reused"


class _DuckStore:
    """Store-shaped object missing `contains` entirely.

    Not a protocol subclass and not structurally complete, so it is the case
    the shape check has to catch before the kernel reaches for the method
    that is not there.
    """

    def __init__(self) -> None:
        self._items: dict[str, bytes] = {}

    def get(self, digest: str) -> bytes | None:
        return self._items.get(digest)

    def put(self, digest: str, payload: bytes) -> None:
        self._items[digest] = payload


@pytest.mark.parametrize("door", ["constructor", "save", "load"])
def test_database_rejects_a_store_missing_required_methods(door: str) -> None:
    assert not isinstance(_DuckStore(), ArtifactStore)

    if door == "constructor":
        with pytest.raises(TypeError, match="must implement the ArtifactStore protocol"):
            Database(store=cast(Any, _DuckStore()))
        return

    db = Database()
    if door == "save":
        with pytest.raises(TypeError, match="must implement the ArtifactStore protocol"):
            db.save_checkpoint(store=cast(Any, _DuckStore()))
    else:
        # A well-formed key that is simply absent: the shape check has to fire
        # ahead of the lookup, or the caller learns about the missing key
        # instead of the unusable store.
        with pytest.raises(TypeError, match="must implement the ArtifactStore protocol"):
            db.load_checkpoint("ck" + "0" * 64, store=cast(Any, _DuckStore()))


def test_database_rejects_a_store_inheriting_the_protocol_stubs() -> None:
    store = _ContainsOnlyStore()  # type: ignore[abstract]

    # It has all three attributes, so the shape check alone waves it through:
    # the two halves of the gate catch different failures.
    assert isinstance(store, ArtifactStore)

    with pytest.raises(TypeError, match="without implementing it"):
        Database(store=store)


class _LoggingMinimalStore(_MinimalProtocolStore):
    """Minimal protocol store that records the digest of every `put`."""

    def __init__(self) -> None:
        super().__init__()
        self.put_log: list[str] = []

    def put(self, digest: str, payload: bytes) -> None:
        self.put_log.append(digest)
        super().put(digest, payload)


def test_minimal_protocol_store_puts_once_per_distinct_digest() -> None:
    seed = Input[int]("minimal_store_seed")

    @query
    def minimal_store_child(db: Database) -> int:
        return seed.read(db) + 1

    @query
    def minimal_store_parent(db: Database) -> int:
        return minimal_store_child(db) * 10

    # Only `get` and `put` are defined; `contains` is the inherited default,
    # which the boundary check has to keep admitting.
    store = _LoggingMinimalStore()
    db = Database(store=store)
    db.set(seed, 4)
    assert db.get(minimal_store_parent) == 50

    ck_key = db.save_checkpoint()
    assert db.save_checkpoint() == ck_key
    assert db.save_checkpoint() == ck_key

    manifest_puts = [d for d in store.put_log if d.startswith("ck")]
    snapshot_puts = [d for d in store.put_log if not d.startswith("ck")]

    # The manifest is written unconditionally and is idempotent on equal
    # bytes, so it repeats once per save under a single key.
    assert manifest_puts == [ck_key, ck_key, ck_key]

    # Every other digest is content-addressed: three identical saves write
    # each of them exactly once, not once per save.
    assert snapshot_puts, "no snapshot was persisted; the count below would be vacuous"
    assert sorted(snapshot_puts) == sorted(set(snapshot_puts))

    warm = Database(store=store)
    warm.set(seed, 4)
    warm.load_checkpoint(ck_key)
    before = warm.statistics()
    assert warm.get(minimal_store_parent) == 50
    after = warm.statistics()

    # Witness: the warm request read the checkpoint through the same minimal
    # store and executed nothing.
    executions_during_warm = after.query_executions - before.query_executions
    assert executions_during_warm == 0
    assert after.query_reuses - before.query_reuses >= 1


def test_filesystem_store_reports_exhausted_open_retries_as_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_contended(path: Path) -> object:
        error = OSError(13, "transient contention")
        error.winerror = 32  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr("pyinc._locking.open_lock_file", always_contended)
    store = FileSystemArtifactStore(tmp_path, lock_timeout=0)
    with pytest.raises(ArtifactStoreLockError):
        store.put("f" * 64, b"payload")
