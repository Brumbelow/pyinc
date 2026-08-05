from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from pyinc import (
    ActionPathError,
    ArtifactStoreError,
    Database,
    DirectoryResource,
    FileResource,
    FileStatResource,
    FileSystemArtifactStore,
    InMemoryArtifactStore,
    Output,
    ResolvedPathResource,
    action,
    query,
)
from pyinc.integrations import (
    deep_requirements_analysis,
    requirements_analysis,
    scope_tree,
    workspace_requirements_analysis,
)
from pyinc.integrations.requirements_txt import requirements_public_payload
from pyinc.integrations.scope_resolution import scope_tree_payload

_MODES = ("strict", "checked", "fast")
_RESOLVED_PATHS = ResolvedPathResource()
_DIRECTORIES = DirectoryResource()
_FILE_STATS = FileStatResource()


@query(key="tests.path-failures.resolved")
def _resolved_value(db: Database, path: str) -> str | None:
    return _RESOLVED_PATHS.read(db, path)


@query(key="tests.path-failures.directory")
def _directory_entries(db: Database, path: str) -> tuple[str, ...]:
    return _DIRECTORIES.read(db, path)


@query(key="tests.path-failures.exists")
def _file_exists(db: Database, path: str) -> bool:
    return _FILE_STATS.read(db, path).exists


def _symlink_loop(tmp_path: Path, name: str) -> tuple[Path, Path]:
    first = tmp_path / f"{name}-first"
    second = tmp_path / f"{name}-second"
    try:
        first.symlink_to(second)
        second.symlink_to(first)
    except (NotImplementedError, OSError):
        pytest.skip("symlink support is unavailable in this environment")
    return first, second


def _remove_loop(first: Path, second: Path) -> None:
    first.unlink()
    second.unlink()


@pytest.mark.parametrize("mode", _MODES)
@pytest.mark.parametrize("loop_at", ("root", "state"))
def test_action_symlink_loop_is_typed_and_side_effect_free(
    mode: str,
    loop_at: str,
    tmp_path: Path,
) -> None:
    loop, peer = _symlink_loop(tmp_path, loop_at)
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    sentinel = ordinary / "sentinel.txt"
    sentinel.write_bytes(b"unchanged")
    calls = 0

    @action(tool=f"path-loop-{mode}-{loop_at}")
    def build(db: Database) -> list[Output]:
        nonlocal calls
        calls += 1
        return [Output("result.txt", b"new")]

    root = loop if loop_at == "root" else ordinary
    state_dir = loop if loop_at == "state" else ordinary
    with pytest.raises(ActionPathError):
        build.reconcile(Database(mode=mode), root=root, state_dir=state_dir)

    assert calls == 0
    assert loop.is_symlink()
    assert peer.is_symlink()
    assert sentinel.read_bytes() == b"unchanged"
    assert sorted(path.name for path in ordinary.iterdir()) == ["sentinel.txt"]


def test_filesystem_store_symlink_loop_is_a_typed_error(tmp_path: Path) -> None:
    loop, peer = _symlink_loop(tmp_path, "store")

    with pytest.raises(ArtifactStoreError):
        FileSystemArtifactStore(loop)

    assert loop.is_symlink()
    assert peer.is_symlink()


def test_historical_resolve_runtime_errors_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action_root = tmp_path / "action"
    action_root.mkdir()
    store_root = tmp_path / "store"
    original_resolve = Path.resolve

    @action(tool="historical-resolve-loop")
    def build(db: Database) -> list[Output]:
        return [Output("result.txt", b"new")]

    def reject_named_paths(path: Path, strict: bool = False) -> Path:
        if path in {action_root, store_root}:
            raise RuntimeError("Symlink loop from pathlib")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", reject_named_paths)
    with pytest.raises(ActionPathError, match="root or state directory"):
        build.reconcile(Database(), root=action_root)
    with pytest.raises(ArtifactStoreError, match="root path is invalid"):
        FileSystemArtifactStore(store_root)
    assert tuple(action_root.iterdir()) == ()
    assert not store_root.exists()


def test_nested_resolution_runtime_errors_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    store = FileSystemArtifactStore(tmp_path / "store")
    action_parent = root / "pkg"
    store_objects = store.root / "objects"
    original_resolve = Path.resolve

    @action(tool="nested-resolve-loop")
    def build(db: Database) -> list[Output]:
        return [Output("pkg/result.txt", b"new")]

    def reject_named_paths(path: Path, strict: bool = False) -> Path:
        if path in {action_parent, store_objects}:
            raise RuntimeError("Symlink loop from pathlib")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", reject_named_paths)
    with pytest.raises(ActionPathError, match="resolve owned output"):
        build.reconcile(Database(), root=root)
    with pytest.raises(ArtifactStoreError, match="resolve artifact-store directory"):
        store.contains("a" * 64)
    assert tuple(root.iterdir()) == ()


def test_windows_unresolvable_link_error_uses_conservative_resource_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "loop"
    original_stat = Path.stat
    original_iterdir = Path.iterdir
    original_open = os.open

    def windows_loop_error() -> OSError:
        error = OSError("The name of the file cannot be resolved by the system")
        error.winerror = 1921  # type: ignore[attr-defined]
        return error

    def fail_stat(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == target:
            raise windows_loop_error()
        return original_stat(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", fail_stat)
        assert _RESOLVED_PATHS.probe(str(target)) == (None,)
        assert _FILE_STATS.probe(str(target)) == (False, None, None)

    def fail_iterdir(path: Path) -> Any:
        if path == target:
            raise windows_loop_error()
        return original_iterdir(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "iterdir", fail_iterdir)
        assert _DIRECTORIES.probe(str(target)) == (False, ())

    def fail_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if os.fspath(path) == str(target):
            raise windows_loop_error()
        return original_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", fail_open)
        assert FileResource().probe(str(target)) == ("missing",)


@pytest.mark.parametrize("mode", _MODES)
def test_conservative_path_states_match_fresh_after_checkpoint(
    mode: str,
    tmp_path: Path,
) -> None:
    resolved_loop, resolved_peer = _symlink_loop(tmp_path, "resolved")
    scope_loop, scope_peer = _symlink_loop(tmp_path, "scope")
    requirements_loop, requirements_peer = _symlink_loop(tmp_path, "requirements")
    workspace_loop, workspace_peer = _symlink_loop(tmp_path, "workspace")
    include_loop, include_peer = _symlink_loop(tmp_path, "include")
    requirements_root = tmp_path / "root-requirements.txt"
    requirements_root.write_text(
        f"-r {include_loop.name}\n-r invalid\0path\n",
        encoding="utf-8",
    )
    nul_path = "invalid\0path"

    store = InMemoryArtifactStore()
    writer = Database(mode=mode, store=store)

    assert writer.get(_resolved_value, str(resolved_loop)) is None
    assert writer.get(_resolved_value, nul_path) is None
    assert writer.get(_file_exists, str(resolved_loop)) is False
    assert writer.get(_file_exists, nul_path) is False
    assert _RESOLVED_PATHS.probe(str(resolved_loop)) == (None,)
    assert _RESOLVED_PATHS.probe(nul_path) == (None,)
    assert _RESOLVED_PATHS.load(writer, nul_path) is None
    assert _RESOLVED_PATHS.probe_and_load(writer, nul_path) == ((None,), None)

    initial_scope = writer.get(scope_tree_payload, str(scope_loop))
    assert initial_scope[0] == str(scope_loop)
    assert initial_scope[2:] == ((), ())
    assert scope_tree(writer, scope_loop).bindings == ()
    initial_nul_scope = scope_tree(writer, nul_path)
    assert initial_nul_scope.bindings == ()

    initial_requirements = writer.get(requirements_public_payload, str(requirements_loop))
    assert initial_requirements[0][1:] == ((), (), (), ())
    assert requirements_analysis(writer, requirements_loop).diagnostics[0][0] == (
        "invalid-requirements-path"
    )
    assert deep_requirements_analysis(writer, requirements_loop).diagnostics[0][0] == (
        "invalid-requirements-path"
    )
    initial_nul_requirements = requirements_analysis(writer, nul_path)
    assert initial_nul_requirements.diagnostics[0][0] == ("invalid-requirements-path")
    initial_deep = deep_requirements_analysis(writer, requirements_root)
    initial_diagnostic_codes = tuple(code for code, _message in initial_deep.diagnostics)
    assert initial_diagnostic_codes.count("invalid-requirements-path") == 1
    assert initial_diagnostic_codes.count("unparseable-line") == 1
    assert workspace_requirements_analysis(writer, workspace_loop) is None
    assert workspace_requirements_analysis(writer, nul_path) is None
    assert writer.get(_directory_entries, str(workspace_loop)) == ()
    assert writer.get(_directory_entries, nul_path) == ()

    fresh_invalid = Database(mode=mode)
    assert writer.get(_resolved_value, str(resolved_loop)) == fresh_invalid.get(
        _resolved_value, str(resolved_loop)
    )
    assert writer.get(_file_exists, str(resolved_loop)) == fresh_invalid.get(
        _file_exists, str(resolved_loop)
    )
    assert writer.get(scope_tree_payload, str(scope_loop)) == fresh_invalid.get(
        scope_tree_payload, str(scope_loop)
    )
    assert requirements_analysis(writer, requirements_loop) == requirements_analysis(
        fresh_invalid, requirements_loop
    )
    assert workspace_requirements_analysis(writer, workspace_loop) == (
        workspace_requirements_analysis(fresh_invalid, workspace_loop)
    )
    assert writer.get(_directory_entries, str(workspace_loop)) == fresh_invalid.get(
        _directory_entries, str(workspace_loop)
    )

    checkpoint = writer.save_checkpoint()
    reader = Database(mode=mode, store=store)
    reader.load_checkpoint(checkpoint)

    assert reader.get(_resolved_value, str(resolved_loop)) is None
    assert reader.get(_resolved_value, nul_path) is None
    assert reader.get(_file_exists, str(resolved_loop)) is False
    assert reader.get(_file_exists, nul_path) is False
    assert reader.get(scope_tree_payload, str(scope_loop)) == initial_scope
    assert scope_tree(reader, nul_path) == initial_nul_scope
    assert reader.get(requirements_public_payload, str(requirements_loop)) == initial_requirements
    assert requirements_analysis(reader, nul_path) == initial_nul_requirements
    assert reader.get(_directory_entries, str(workspace_loop)) == ()
    assert reader.get(_directory_entries, nul_path) == ()

    _remove_loop(resolved_loop, resolved_peer)
    resolved_loop.write_bytes(b"resolved")
    _remove_loop(scope_loop, scope_peer)
    scope_loop.write_text("def value():\n    return 1\n", encoding="utf-8")
    _remove_loop(requirements_loop, requirements_peer)
    requirements_loop.write_text("requests>=2\n", encoding="utf-8")
    _remove_loop(workspace_loop, workspace_peer)
    workspace_loop.mkdir()
    (workspace_loop / "requirements.txt").write_text("click>=8\n", encoding="utf-8")
    _remove_loop(include_loop, include_peer)
    include_loop.write_text("flask>=3\n", encoding="utf-8")

    fresh = Database(mode=mode)
    assert (
        reader.get(_resolved_value, str(resolved_loop))
        == fresh.get(_resolved_value, str(resolved_loop))
        == str(resolved_loop.resolve())
    )
    assert (
        reader.get(_file_exists, str(resolved_loop))
        == fresh.get(_file_exists, str(resolved_loop))
        is True
    )
    assert scope_tree(reader, scope_loop) == scope_tree(fresh, scope_loop)
    assert scope_tree(reader, scope_loop).bindings[0].name == "value"
    assert requirements_analysis(reader, requirements_loop) == requirements_analysis(
        fresh, requirements_loop
    )
    assert requirements_analysis(reader, requirements_loop).requirements[0].name == "requests"
    assert deep_requirements_analysis(reader, requirements_loop) == deep_requirements_analysis(
        fresh, requirements_loop
    )
    assert deep_requirements_analysis(reader, requirements_root) == deep_requirements_analysis(
        fresh, requirements_root
    )
    assert deep_requirements_analysis(reader, requirements_root).requirements[0].name == "flask"
    assert workspace_requirements_analysis(reader, workspace_loop) == (
        workspace_requirements_analysis(fresh, workspace_loop)
    )
    workspace_result = workspace_requirements_analysis(reader, workspace_loop)
    assert workspace_result is not None
    assert workspace_result.requirements[0].name == "click"
    assert (
        reader.get(_directory_entries, str(workspace_loop))
        == fresh.get(_directory_entries, str(workspace_loop))
        == ("requirements.txt",)
    )

    assert reader.inspect(_resolved_value, str(resolved_loop)).last_recompute == "executed"
    assert reader.inspect(_file_exists, str(resolved_loop)).last_recompute == "executed"
    assert reader.inspect(scope_tree_payload, str(scope_loop)).last_recompute == "executed"
    assert (
        reader.inspect(requirements_public_payload, str(requirements_loop)).last_recompute
        == "executed"
    )
    assert reader.inspect(_directory_entries, str(workspace_loop)).last_recompute == "executed"
