from __future__ import annotations

import _thread
import concurrent.futures
import functools
import multiprocessing
import multiprocessing.pool
import os
import shlex
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

import pytest

from pyinc import (
    Database,
    InMemoryArtifactStore,
    Input,
    QueryConcurrencyError,
    query,
)

_THREAD_INPUT = Input[bool]("query-concurrency-enabled")


def _write_marker(path: str) -> str:
    Path(path).write_text("launched", encoding="utf-8")
    return "launched"


def _process_context() -> multiprocessing.context.BaseContext:
    return multiprocessing.get_context("fork" if hasattr(os, "fork") else "spawn")


def _error(call: Callable[[], Any]) -> tuple[type[BaseException], str]:
    with pytest.raises(QueryConcurrencyError) as raised:
        call()
    return type(raised.value), str(raised.value)


@query(key="query-concurrency-thread-transition")
def _thread_transition(db: Database, marker: str) -> str:
    if not _THREAD_INPUT.read(db):
        return "safe"
    worker = threading.Thread(target=_write_marker, args=(marker,))
    with suppress(QueryConcurrencyError):
        worker.start()
    return "caught"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_thread_launch_input_transition_matches_fresh_without_starting_worker(
    mode: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"thread-{mode}"
    warm = Database(mode=mode)
    warm.set(_THREAD_INPUT, False)
    assert warm.get(_thread_transition, str(marker)) == "safe"

    warm.set(_THREAD_INPUT, True)
    warm_error = _error(lambda: warm.get(_thread_transition, str(marker)))
    assert not marker.exists()

    fresh = Database(mode=mode)
    fresh.set(_THREAD_INPUT, True)
    fresh_error = _error(lambda: fresh.get(_thread_transition, str(marker)))
    assert warm_error == fresh_error
    assert not marker.exists()

    warm.set(_THREAD_INPUT, False)
    assert warm.get(_thread_transition, str(marker)) == "safe"


@query(key="query-concurrency-low-level-thread")
def _low_level_thread(db: Database, alias_name: str, marker: str) -> str:
    del db
    launch = cast(Callable[..., Any], getattr(_thread, alias_name))
    with suppress(QueryConcurrencyError):
        launch(_write_marker, (marker,))
    return "caught"


_LOW_LEVEL_THREAD_ALIASES = tuple(
    name
    for name in ("start_new_thread", "start_new", "start_joinable_thread")
    if hasattr(_thread, name)
)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("alias_name", _LOW_LEVEL_THREAD_ALIASES)
def test_low_level_thread_aliases_are_sticky_and_do_not_launch(
    mode: str,
    alias_name: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"low-{mode}-{alias_name}"
    error_type, message = _error(
        lambda: Database(mode=mode).get(_low_level_thread, alias_name, str(marker))
    )
    assert error_type is QueryConcurrencyError
    assert alias_name in message
    assert not marker.exists()


@query(key="query-concurrency-prewarmed-executor")
def _prewarmed_executor_query(db: Database, marker: str) -> str:
    del db
    submit = cast(Callable[..., Any], globals()["_ACTIVE_EXECUTOR_SUBMIT"])
    try:
        future = submit(_write_marker, marker)
    except QueryConcurrencyError:
        return "caught"
    return cast(str, future.result(timeout=5))


@pytest.mark.parametrize(
    "executor_type",
    [concurrent.futures.ThreadPoolExecutor, concurrent.futures.ProcessPoolExecutor],
)
def test_prewarmed_executor_submit_is_rejected_in_every_mode(
    executor_type: Callable[..., concurrent.futures.Executor],
    tmp_path: Path,
) -> None:
    executor_kwargs: dict[str, Any] = {"max_workers": 1}
    if executor_type is concurrent.futures.ProcessPoolExecutor:
        executor_kwargs["mp_context"] = _process_context()
    with executor_type(**executor_kwargs) as executor:
        assert executor.submit(abs, -1).result(timeout=10) == 1
        globals()["_ACTIVE_EXECUTOR_SUBMIT"] = executor.submit
        try:
            for mode in ("strict", "checked", "fast"):
                marker = tmp_path / f"{executor_type.__name__}-{mode}"
                database = Database(mode=mode)
                error_type, message = _error(
                    functools.partial(database.get, _prewarmed_executor_query, str(marker))
                )
                assert error_type is QueryConcurrencyError
                assert "Executor.submit" in message
                assert not marker.exists()
        finally:
            globals()["_ACTIVE_EXECUTOR_SUBMIT"] = None


def _pool_call(pool: multiprocessing.pool.Pool, method_name: str, marker: str) -> Any:
    method = getattr(pool, method_name)
    if method_name == "apply":
        return method(_write_marker, (marker,))
    if method_name == "apply_async":
        return method(_write_marker, (marker,)).get(timeout=5)
    if method_name in {"map", "imap", "imap_unordered"}:
        return tuple(method(_write_marker, [marker]))
    if method_name in {"map_async"}:
        return tuple(method(_write_marker, [marker]).get(timeout=5))
    if method_name in {"starmap"}:
        return tuple(method(_write_marker, [(marker,)]))
    if method_name in {"starmap_async"}:
        return tuple(method(_write_marker, [(marker,)]).get(timeout=5))
    raise AssertionError(method_name)


@query(key="query-concurrency-preexisting-pool")
def _preexisting_pool_query(db: Database) -> str:
    del db
    call = cast(Callable[[], Any], globals()["_ACTIVE_POOL_CALL"])
    try:
        call()
    except QueryConcurrencyError:
        return "caught"
    return "submitted"


@pytest.mark.parametrize(
    "method_name",
    [
        "apply",
        "apply_async",
        "map",
        "map_async",
        "starmap",
        "starmap_async",
        "imap",
        "imap_unordered",
    ],
)
def test_preexisting_pool_submission_apis_are_rejected(
    method_name: str,
    tmp_path: Path,
) -> None:
    context = _process_context()
    pool = context.Pool(1)
    try:
        assert pool.apply(abs, (-1,)) == 1
        for mode in ("strict", "checked", "fast"):
            marker = tmp_path / f"pool-{method_name}-{mode}"
            globals()["_ACTIVE_POOL_CALL"] = functools.partial(
                _pool_call, pool, method_name, str(marker)
            )
            database = Database(mode=mode)
            error_type, message = _error(functools.partial(database.get, _preexisting_pool_query))
            assert error_type is QueryConcurrencyError
            assert "Pool" in message
            assert not marker.exists()
    finally:
        globals()["_ACTIVE_POOL_CALL"] = None
        pool.close()
        pool.join()


@query(key="query-concurrency-preexisting-process")
def _preexisting_process_query(db: Database) -> str:
    del db
    process = cast(multiprocessing.process.BaseProcess, globals()["_ACTIVE_PROCESS"])
    try:
        process.start()
    except QueryConcurrencyError:
        return "caught"
    return "started"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_preexisting_multiprocessing_process_cannot_start(
    mode: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"process-{mode}"
    process = cast(Any, _process_context()).Process(
        target=_write_marker,
        args=(str(marker),),
    )
    globals()["_ACTIVE_PROCESS"] = process
    try:
        error_type, message = _error(lambda: Database(mode=mode).get(_preexisting_process_query))
        assert error_type is QueryConcurrencyError
        assert "Process.start" in message
        assert process.pid is None
        assert not marker.exists()
    finally:
        globals()["_ACTIVE_PROCESS"] = None
        process.close()


@query(key="query-concurrency-subprocess-marker")
def _subprocess_marker_query(db: Database, marker: str) -> str:
    del db
    try:
        subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x')",
                marker,
            ],
            check=True,
        )
    except QueryConcurrencyError:
        return "caught"
    return "launched"


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_subprocess_popen_is_rejected_before_marker_side_effect(
    mode: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"subprocess-{mode}"
    error_type, message = _error(
        lambda: Database(mode=mode).get(_subprocess_marker_query, str(marker))
    )
    assert error_type is QueryConcurrencyError
    assert "subprocess.Popen" in message
    assert not marker.exists()


def _external_command(marker: str) -> str:
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x')",
        marker,
    ]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@query(key="query-concurrency-os-external-launch")
def _os_external_launch_query(db: Database, operation: str, marker: str) -> str:
    del db
    argv = [
        sys.executable,
        "-c",
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('x')",
        marker,
    ]
    try:
        if operation == "system":
            os.system(_external_command(marker))
        elif operation == "popen":
            os.popen(_external_command(marker)).read()
        elif operation in {"posix_spawn", "posix_spawnp"}:
            pid = getattr(os, operation)(sys.executable, argv, {})
            os.waitpid(pid, 0)
        elif operation in {"spawnv", "spawnvp"}:
            getattr(os, operation)(os.P_WAIT, sys.executable, argv)
        elif operation in {"spawnve", "spawnvpe"}:
            getattr(os, operation)(os.P_WAIT, sys.executable, argv, {})
        elif operation in {"spawnl", "spawnlp"}:
            getattr(os, operation)(os.P_WAIT, sys.executable, *argv)
        elif operation in {"spawnle", "spawnlpe"}:
            getattr(os, operation)(os.P_WAIT, sys.executable, *argv, {})
        else:
            raise AssertionError(operation)
    except QueryConcurrencyError:
        return "caught"
    return "launched"


_OS_EXTERNAL_LAUNCHES = tuple(
    name
    for name in (
        "posix_spawn",
        "posix_spawnp",
        "spawnl",
        "spawnle",
        "spawnlp",
        "spawnlpe",
        "spawnv",
        "spawnve",
        "spawnvp",
        "spawnvpe",
        "system",
        "popen",
    )
    if hasattr(os, name)
)


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
@pytest.mark.parametrize("operation", _OS_EXTERNAL_LAUNCHES)
def test_os_external_process_entrypoints_are_rejected_in_queries(
    mode: str,
    operation: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"os-{operation}-{mode}"
    error_type, message = _error(
        lambda: Database(mode=mode).get(
            _os_external_launch_query,
            operation,
            str(marker),
        )
    )
    assert error_type is QueryConcurrencyError
    assert operation in message
    assert not marker.exists()


@query(key="query-concurrency-os-fork")
def _os_fork_query(db: Database) -> str:
    del db
    try:
        pid = os.fork()
    except QueryConcurrencyError:
        return "caught"
    if pid == 0:  # pragma: no cover - reached only if the guard regresses
        os._exit(0)
    os.waitpid(pid, 0)
    return "forked"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is unavailable")
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_os_fork_is_rejected_before_process_creation(mode: str) -> None:
    error_type, message = _error(lambda: Database(mode=mode).get(_os_fork_query))
    assert error_type is QueryConcurrencyError
    assert "os.fork" in message


class _SubprocessResource:
    def identity(self) -> tuple[str]:
        return ("query-concurrency-subprocess-resource",)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        return f"command[{key}]"

    def probe(self, key: str) -> tuple[str]:
        return (key,)

    def load(self, db: Database, key: str) -> str:
        del db, key
        return subprocess.run(
            [sys.executable, "-c", "print('resource-ok')"],
            check=True,
            capture_output=True,
            text=True,
            close_fds=False,
        ).stdout.strip()


_SUBPROCESS_RESOURCE = _SubprocessResource()


@query(key="query-concurrency-subprocess-resource-root")
def _subprocess_resource_root(db: Database) -> str:
    return _SUBPROCESS_RESOURCE.read(db, "command")


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_subprocess_is_allowed_as_tracked_resource_io(mode: str) -> None:
    assert Database(mode=mode).get(_subprocess_resource_root) == "resource-ok"


@pytest.mark.skipif(
    os.name != "posix" or not getattr(subprocess, "_USE_POSIX_SPAWN", False),
    reason="subprocess posix_spawn path is unavailable",
)
def test_resource_subprocess_posix_spawn_path_remains_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    popen_type = cast(Any, subprocess.Popen)
    original = popen_type._posix_spawn

    def tracking_posix_spawn(self: Any, *args: Any, **kwargs: Any) -> None:
        calls.append(True)
        original(self, *args, **kwargs)

    monkeypatch.setattr(popen_type, "_posix_spawn", tracking_posix_spawn)
    assert Database().get(_subprocess_resource_root) == "resource-ok"
    assert calls


class _CaughtThreadResource:
    def __init__(self, marker: str) -> None:
        self.marker = marker
        self.armed = False
        self.version = 0

    def identity(self) -> tuple[str]:
        return ("query-concurrency-caught-thread-resource",)

    def read(self, db: Database, key: str) -> str:
        return cast(str, db.read_resource(self, key))

    def label(self, key: str) -> str:
        return f"thread-resource[{key}]"

    def probe(self, key: str) -> tuple[str, int]:
        return key, self.version

    def load(self, db: Database, key: str) -> str:
        del db, key
        if not self.armed:
            return "safe"
        worker = threading.Thread(target=_write_marker, args=(self.marker,))
        with suppress(QueryConcurrencyError):
            worker.start()
        return "caught"


def _resource_state(db: Database) -> tuple[Any, ...]:
    return tuple(
        sorted(
            (
                (
                    key,
                    record.digest,
                    record.changed_at,
                    record.verified_at,
                    record.last_decision,
                    record.reason,
                    record.checked_in_request,
                    record.failure,
                    record.probe_unconfirmed,
                )
                for key, record in db._records.items()
                if key.kind == "resource"
            ),
            key=lambda item: item[0].label,
        )
    )


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_caught_resource_thread_violation_leaves_warm_state_and_store_clean(
    mode: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"resource-thread-{mode}"
    store = InMemoryArtifactStore()
    db = Database(mode=mode, store=store)
    resource = _CaughtThreadResource(str(marker))
    assert resource.read(db, "value") == "safe"

    before_revision = db.revision
    before_statistics = db.statistics()
    before_records = _resource_state(db)
    before_registry = tuple(sorted(db._resource_objects(), key=lambda key: key.label))
    before_store = tuple(sorted(store.keys()))
    resource.armed = True
    resource.version = 1

    error_type, message = _error(lambda: resource.read(db, "value"))
    assert error_type is QueryConcurrencyError
    assert "Resource hook" in message
    assert db.revision == before_revision
    assert _resource_state(db) == before_records
    assert tuple(sorted(db._resource_objects(), key=lambda key: key.label)) == before_registry
    assert tuple(sorted(store.keys())) == before_store
    after_statistics = db.statistics()
    assert after_statistics.resource_loads == before_statistics.resource_loads
    assert after_statistics.resource_probe_hits == before_statistics.resource_probe_hits
    assert not marker.exists()

    fresh = Database(mode=mode)
    fresh_resource = _CaughtThreadResource(str(marker))
    fresh_resource.armed = True
    fresh_resource.version = 1
    fresh_error = _error(lambda: fresh_resource.read(fresh, "value"))
    assert fresh_error == (error_type, message)
    assert not fresh._resource_objects()
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_same_mode_checkpoint_cannot_restore_past_thread_launch(
    mode: str,
    tmp_path: Path,
) -> None:
    marker = tmp_path / f"checkpoint-thread-{mode}"
    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)
    writer.set(_THREAD_INPUT, False)
    assert writer.get(_thread_transition, str(marker)) == "safe"
    checkpoint = writer.save_checkpoint()

    loaded = Database(mode=mode, store=store)
    loaded.set(_THREAD_INPUT, True)
    loaded.load_checkpoint(checkpoint)
    loaded_error = _error(lambda: loaded.get(_thread_transition, str(marker)))

    fresh = Database(mode=mode)
    fresh.set(_THREAD_INPUT, True)
    fresh_error = _error(lambda: fresh.get(_thread_transition, str(marker)))
    assert loaded_error == fresh_error
    assert not marker.exists()


@pytest.mark.parametrize("target", ["same", "cross"])
@pytest.mark.parametrize("mode", ["strict", "checked", "fast"])
def test_thread_join_deadlock_shapes_are_rejected_in_subprocess(
    mode: str,
    target: str,
) -> None:
    script = textwrap.dedent(
        f"""
        import threading
        from pyinc import Database, Input, QueryConcurrencyError, query

        FLAG = Input[int]("deadlock-flag")
        PARENT = None
        TARGET = None

        @query(key="deadlock-cross-leaf")
        def cross_leaf(db):
            return globals()["FLAG"].read(globals()["PARENT"])

        @query(key="deadlock-root")
        def root(db):
            def work():
                if {target!r} == "same":
                    globals()["FLAG"].read(db)
                else:
                    globals()["TARGET"].get(cross_leaf)
            worker = threading.Thread(target=work)
            try:
                worker.start()
            except QueryConcurrencyError:
                pass
            else:
                worker.join()
            return 1

        parent = Database(mode={mode!r})
        target_db = parent if {target!r} == "same" else Database(mode={mode!r})
        parent.set(FLAG, 1)
        globals()["PARENT"] = parent
        globals()["TARGET"] = target_db
        try:
            parent.get(root)
        except QueryConcurrencyError:
            print("REJECTED")
        else:
            raise SystemExit("launch was not rejected")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REJECTED"


def test_cached_preimport_thread_and_executor_references_are_rejected() -> None:
    script = textwrap.dedent(
        """
        import concurrent.futures
        import threading

        CACHED_START = threading.Thread.start
        EXECUTOR = concurrent.futures.ThreadPoolExecutor(1)
        EXECUTOR.submit(abs, -1).result()
        CACHED_SUBMIT = EXECUTOR.submit

        from pyinc import Database, QueryConcurrencyError, query

        @query(key="cached-preimport-thread")
        def thread_query(db):
            worker = threading.Thread(target=lambda: None)
            try:
                globals()["CACHED_START"](worker)
            except QueryConcurrencyError:
                pass
            return 1

        @query(key="cached-preimport-executor")
        def executor_query(db):
            try:
                globals()["CACHED_SUBMIT"](abs, -2).result()
            except QueryConcurrencyError:
                pass
            return 1

        try:
            for item in (thread_query, executor_query):
                try:
                    Database().get(item)
                except QueryConcurrencyError:
                    continue
                raise SystemExit("cached launch escaped")
        finally:
            EXECUTOR.shutdown()
        print("REJECTED")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "REJECTED"


def test_top_level_concurrency_and_subprocesses_remain_available(tmp_path: Path) -> None:
    thread_marker = tmp_path / "top-thread"
    worker = threading.Thread(target=_write_marker, args=(str(thread_marker),))
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert thread_marker.read_text(encoding="utf-8") == "launched"

    with concurrent.futures.ThreadPoolExecutor(1) as thread_pool:
        assert thread_pool.submit(abs, -2).result(timeout=5) == 2
    with concurrent.futures.ProcessPoolExecutor(1, mp_context=_process_context()) as process_pool:
        assert process_pool.submit(abs, -3).result(timeout=10) == 3

    completed = subprocess.run(
        [sys.executable, "-c", "print('top-subprocess')"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "top-subprocess"
