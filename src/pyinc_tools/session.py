from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pyinc import Database
from pyinc.integrations import (
    ConfigAnalysis,
    DependencyCheckAnalysis,
    DependencyStatus,
    DependencySurface,
    ModuleSymbolTable,
    PythonModuleAnalysis,
    PythonWorkspaceAnalysis,
    Reference,
    ReferenceQueryResult,
    RequirementsAnalysis,
    ResolvedImportRef,
    ResolvedSymbol,
    WorkspaceSymbolIndex,
    find_references,
    module_analysis,
    module_symbol_table,
    resolve_symbol,
    workspace_analysis,
    workspace_config_analysis,
    workspace_dependency_check,
    workspace_requirements_analysis,
    workspace_symbol_index,
)

DiagnosticSeverity = Literal["error", "warning", "information", "hint"]

DEFAULT_IGNORED_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".hypothesis",
        "dist",
    }
)


@dataclass(frozen=True)
class AnalysisDiagnostic:
    path: str
    code: str
    message: str
    severity: DiagnosticSeverity
    source: str
    lineno: int | None = None
    col_offset: int | None = None


@dataclass(frozen=True)
class FileAnalysisResult:
    path: str
    module: PythonModuleAnalysis | None
    symbols: ModuleSymbolTable | None
    dependency_check: DependencyCheckAnalysis
    diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class WorkspaceAnalysisResult:
    root: str
    python: PythonWorkspaceAnalysis
    symbols: WorkspaceSymbolIndex
    dependency_check: DependencyCheckAnalysis
    files: tuple[FileAnalysisResult, ...]
    diagnostics: tuple[AnalysisDiagnostic, ...]


@dataclass(frozen=True)
class _DependencyInputs:
    config: ConfigAnalysis | None
    requirements: RequirementsAnalysis | None
    declared_dependencies: tuple[str, ...]


def _normalize_dependency_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _collect_filesystem_snapshot(
    root: str, ignored_dir_names: frozenset[str]
) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored_dir_names]
        for filename in filenames:
            file_path = Path(current_root, filename).resolve(strict=False)
            try:
                stat = file_path.stat()
            except FileNotFoundError:
                continue
            snapshot[str(file_path)] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


class WorkspaceSession:
    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        mode: str = "strict",
        ignored_dir_names: tuple[str, ...] | None = None,
    ) -> None:
        root_path = Path(root).resolve(strict=False)
        if not root_path.exists() or not root_path.is_dir():
            raise ValueError(f"{root_path!s} is not an existing workspace directory.")

        self.root = str(root_path)
        self.db = Database(mode=mode)
        self._ignored_dir_names = frozenset(ignored_dir_names or DEFAULT_IGNORED_DIR_NAMES)
        self._tempdir: tempfile.TemporaryDirectory[str] = tempfile.TemporaryDirectory(
            prefix="pyinc-tools-"
        )
        self.mirror_root = str(Path(self._tempdir.name, "workspace"))
        self._mirror_root_path = Path(self.mirror_root)
        self._mirror_root_path.mkdir(parents=True, exist_ok=True)
        self._overlays: dict[str, str] = {}
        self._scheduled_paths: set[str] = set()
        self._state_lock = threading.RLock()
        self._closed = False
        self._copy_workspace_into_mirror()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._tempdir.cleanup()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("WorkspaceSession is closed.")

    def __enter__(self) -> WorkspaceSession:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    def set_overlay(self, path: str | os.PathLike[str], text: str) -> str:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            mirror_path.write_text(text, encoding="utf-8")
            self._overlays[real_path] = text
            self._scheduled_paths.add(real_path)
            return real_path

    def clear_overlay(self, path: str | os.PathLike[str]) -> str:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            self._overlays.pop(real_path, None)
            self._sync_path_from_disk(real_path)
            self._scheduled_paths.add(real_path)
            return real_path

    def refresh_paths(
        self,
        paths: Sequence[str | os.PathLike[str]],
    ) -> tuple[str, ...]:
        with self._state_lock:
            self._check_open()
            refreshed: list[str] = []
            seen: set[str] = set()
            for raw_path in paths:
                real_path = self._normalize_real_path(raw_path)
                if real_path in seen:
                    continue
                seen.add(real_path)
                if real_path not in self._overlays:
                    self._sync_path_from_disk(real_path)
                self._scheduled_paths.add(real_path)
                refreshed.append(real_path)
            return tuple(refreshed)

    def analyze_file(self, path: str | os.PathLike[str]) -> FileAnalysisResult:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            dependency_inputs = self._dependency_inputs()
            dependency_check = workspace_dependency_check(
                self.db,
                self.mirror_root,
                dependency_inputs.declared_dependencies,
            )
            result = self._build_file_result(real_path, dependency_inputs, dependency_check)
            self._scheduled_paths.discard(real_path)
            return result

    def analyze_workspace(self) -> WorkspaceAnalysisResult:
        with self._state_lock:
            self._check_open()
            dependency_inputs = self._dependency_inputs()
            dependency_check = workspace_dependency_check(
                self.db,
                self.mirror_root,
                dependency_inputs.declared_dependencies,
            )
            python_analysis = self._remap_workspace_analysis(
                workspace_analysis(self.db, self.mirror_root)
            )
            symbol_index = self._remap_workspace_symbol_index(
                workspace_symbol_index(self.db, self.mirror_root)
            )
            files = tuple(
                self._build_file_result(
                    module.path,
                    dependency_inputs,
                    dependency_check,
                    module=module,
                )
                for module in python_analysis.modules
            )
            diagnostics = self._dedupe_diagnostics(
                tuple(
                    diagnostic
                    for file_result in files
                    for diagnostic in file_result.diagnostics
                )
                + self._dependency_status_diagnostics(dependency_inputs, dependency_check)
            )
            self._scheduled_paths.clear()
            return WorkspaceAnalysisResult(
                root=self.root,
                python=python_analysis,
                symbols=symbol_index,
                dependency_check=dependency_check,
                files=files,
                diagnostics=diagnostics,
            )

    def resolve_symbol_reference(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
    ) -> ResolvedSymbol:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            resolved = resolve_symbol(
                self.db, self.mirror_root, str(mirror_path), qualified_name
            )
            return self._remap_resolved_symbol(resolved)

    def find_references(
        self,
        path: str | os.PathLike[str],
        qualified_name: str,
        *,
        include_declaration: bool = True,
    ) -> ReferenceQueryResult:
        with self._state_lock:
            self._check_open()
            real_path = self._normalize_real_path(path)
            mirror_path = self._mirror_path_for_real(real_path)
            if not mirror_path.exists() or mirror_path.suffix != ".py":
                raise FileNotFoundError(real_path)
            result = find_references(
                self.db,
                self.mirror_root,
                str(mirror_path),
                qualified_name,
                include_declaration=include_declaration,
            )
            remapped_target = self._remap_resolved_symbol(result.target)
            remapped_refs = tuple(
                Reference(
                    path=self._remap_path(ref.path) or ref.path,
                    lineno=ref.lineno,
                    col_offset=ref.col_offset,
                    end_col_offset=ref.end_col_offset,
                    is_declaration=ref.is_declaration,
                )
                for ref in result.references
            )
            return ReferenceQueryResult(target=remapped_target, references=remapped_refs)

    def source_text(self, path: str | os.PathLike[str]) -> str | None:
        real_path = self._normalize_real_path(path)
        with self._state_lock:
            if self._closed:
                return None
            overlay = self._overlays.get(real_path)
        if overlay is not None:
            return overlay
        try:
            return Path(real_path).read_text(encoding="utf-8")
        except OSError:
            return None

    def _build_file_result(
        self,
        real_path: str,
        dependency_inputs: _DependencyInputs,
        dependency_check: DependencyCheckAnalysis,
        *,
        module: PythonModuleAnalysis | None = None,
    ) -> FileAnalysisResult:
        diagnostics = list(
            self._dependency_status_diagnostics(
                dependency_inputs,
                dependency_check,
                only_path=real_path,
            )
        )
        mirror_path = self._mirror_path_for_real(real_path)
        if not mirror_path.exists() or mirror_path.suffix != ".py":
            return FileAnalysisResult(
                path=real_path,
                module=None,
                symbols=None,
                dependency_check=dependency_check,
                diagnostics=self._dedupe_diagnostics(tuple(diagnostics)),
            )

        module_result = module or self._remap_module_analysis(
            module_analysis(self.db, self.mirror_root, str(mirror_path))
        )
        symbol_table = self._remap_module_symbol_table(
            module_symbol_table(self.db, self.mirror_root, str(mirror_path))
        )
        diagnostics.extend(
            self._module_diagnostics(
                real_path,
                str(mirror_path),
                module_result,
                dependency_check,
            )
        )
        return FileAnalysisResult(
            path=real_path,
            module=module_result,
            symbols=symbol_table,
            dependency_check=dependency_check,
            diagnostics=self._dedupe_diagnostics(tuple(diagnostics)),
        )

    def _module_diagnostics(
        self,
        real_path: str,
        mirror_path: str,
        module_result: PythonModuleAnalysis,
        dependency_check: DependencyCheckAnalysis,
    ) -> tuple[AnalysisDiagnostic, ...]:
        diagnostics: list[AnalysisDiagnostic] = []
        declared_names = {status.name for status in dependency_check.statuses}

        for diagnostic in module_result.diagnostics:
            diagnostics.append(
                AnalysisDiagnostic(
                    path=real_path,
                    code=diagnostic.code,
                    message=diagnostic.message,
                    severity="error",
                    source="pyinc.python_source",
                    lineno=diagnostic.lineno,
                    col_offset=diagnostic.col_offset,
                )
            )

        for resolved_import in module_result.resolved_imports:
            if resolved_import.resolution == "missing":
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="missing-import",
                        message=f"Import {resolved_import.module!r} could not be resolved.",
                        severity="error",
                        source="pyinc.python_source",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )
            elif resolved_import.resolution == "ambiguous":
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="ambiguous-import",
                        message=f"Import {resolved_import.module!r} resolved ambiguously.",
                        severity="warning",
                        source="pyinc.python_source",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )

            if (
                resolved_import.resolution == "installed"
                and resolved_import.distribution_name is not None
                and _normalize_dependency_name(resolved_import.distribution_name) not in declared_names
            ):
                diagnostics.append(
                    AnalysisDiagnostic(
                        path=real_path,
                        code="undeclared-import",
                        message=(
                            f"Import {resolved_import.module!r} comes from installed distribution "
                            f"{resolved_import.distribution_name!r}, but that dependency is not declared."
                        ),
                        severity="warning",
                        source="pyinc.dependency_check",
                        lineno=resolved_import.lineno,
                        col_offset=0,
                    )
                )

            if (
                resolved_import.resolution == "workspace"
                and resolved_import.imported_name is not None
                and resolved_import.imported_name != "*"
            ):
                symbol_result = self._remap_resolved_symbol(
                    resolve_symbol(
                        self.db,
                        self.mirror_root,
                        mirror_path,
                        resolved_import.imported_name,
                    )
                )
                if symbol_result.resolution == "missing":
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=real_path,
                            code="unresolved-symbol",
                            message=(
                                f"Imported name {resolved_import.imported_name!r} could not be resolved "
                                f"from {resolved_import.module!r}."
                            ),
                            severity="error",
                            source="pyinc.symbol_resolution",
                            lineno=resolved_import.lineno,
                            col_offset=0,
                        )
                    )
                elif symbol_result.resolution == "ambiguous":
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=real_path,
                            code="ambiguous-symbol",
                            message=(
                                f"Imported name {resolved_import.imported_name!r} is ambiguous "
                                f"when resolved from {resolved_import.module!r}."
                            ),
                            severity="warning",
                            source="pyinc.symbol_resolution",
                            lineno=resolved_import.lineno,
                            col_offset=0,
                        )
                    )

        return tuple(diagnostics)

    def _dependency_inputs(self) -> _DependencyInputs:
        config = workspace_config_analysis(self.db, self.mirror_root)
        requirements = workspace_requirements_analysis(self.db, self.mirror_root)

        declared: list[str] = []
        if config is not None:
            declared.extend(config.dependencies)
            for _, group_entries in config.optional_dependency_groups:
                declared.extend(group_entries)
        if requirements is not None:
            declared.extend(requirement.raw_line for requirement in requirements.requirements)

        return _DependencyInputs(
            config=self._remap_config_analysis(config),
            requirements=self._remap_requirements_analysis(requirements),
            declared_dependencies=tuple(dict.fromkeys(declared)),
        )

    def _dependency_status_diagnostics(
        self,
        dependency_inputs: _DependencyInputs,
        dependency_check: DependencyCheckAnalysis,
        *,
        only_path: str | None = None,
    ) -> tuple[AnalysisDiagnostic, ...]:
        diagnostics: list[AnalysisDiagnostic] = []
        requirements_by_name: dict[str, tuple[str, int]] = {}
        if dependency_inputs.requirements is not None:
            for requirement in dependency_inputs.requirements.requirements:
                requirements_by_name.setdefault(
                    _normalize_dependency_name(requirement.name),
                    (dependency_inputs.requirements.path, requirement.lineno),
                )

        config_path = dependency_inputs.config.path if dependency_inputs.config is not None else None

        for status in dependency_check.statuses:
            if status.status == "satisfied":
                continue

            if status.name in requirements_by_name:
                path, lineno = requirements_by_name[status.name]
            elif config_path is not None:
                path, lineno = config_path, 1
            else:
                continue

            if only_path is not None and path != only_path:
                continue

            message = self._dependency_status_message(status)
            diagnostics.append(
                AnalysisDiagnostic(
                    path=path,
                    code=f"dependency-{status.status}",
                    message=message,
                    severity="warning",
                    source="pyinc.dependency_check",
                    lineno=lineno,
                    col_offset=0,
                )
            )

        if dependency_check.diagnostics:
            target_path: str | None = None
            if dependency_inputs.requirements is not None:
                target_path = dependency_inputs.requirements.path
            elif dependency_inputs.config is not None:
                target_path = dependency_inputs.config.path
            if target_path is not None and (only_path is None or target_path == only_path):
                for code, message in dependency_check.diagnostics:
                    diagnostics.append(
                        AnalysisDiagnostic(
                            path=target_path,
                            code=code,
                            message=message,
                            severity="warning",
                            source="pyinc.dependency_check",
                            lineno=1,
                            col_offset=0,
                        )
                    )

        return self._dedupe_diagnostics(tuple(diagnostics))

    def _dependency_status_message(self, status: DependencyStatus) -> str:
        if status.status == "missing":
            return f"Declared dependency {status.name!r} is not installed."
        if status.status == "version_mismatch":
            return (
                f"Declared dependency {status.name!r} with constraint {status.declared_spec!r} "
                f"does not match installed version {status.installed_version!r}: {status.detail}"
            )
        return (
            f"Declared dependency {status.name!r} could not be evaluated: "
            f"{status.detail or status.declared_spec!r}"
        )

    def _copy_workspace_into_mirror(self) -> None:
        for current_root, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [name for name in dirnames if name not in self._ignored_dir_names]
            relative_dir = Path(current_root).resolve(strict=False).relative_to(Path(self.root))
            target_dir = self._mirror_root_path / relative_dir
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename in filenames:
                source_path = Path(current_root, filename)
                target_path = target_dir / filename
                shutil.copy2(source_path, target_path)

    def _normalize_real_path(self, path: str | os.PathLike[str]) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = Path(self.root, candidate)
        normalized = candidate.resolve(strict=False)
        try:
            normalized.relative_to(Path(self.root))
        except ValueError as exc:
            raise ValueError(f"{normalized!s} is outside the workspace root {self.root!r}.") from exc
        return str(normalized)

    def _mirror_path_for_real(self, real_path: str) -> Path:
        relative_path = Path(real_path).relative_to(Path(self.root))
        return self._mirror_root_path / relative_path

    def _sync_path_from_disk(self, real_path: str) -> None:
        source_path = Path(real_path)
        mirror_path = self._mirror_path_for_real(real_path)
        if source_path.exists():
            if source_path.is_dir():
                mirror_path.mkdir(parents=True, exist_ok=True)
                return
            mirror_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, mirror_path)
            return

        if mirror_path.is_dir():
            shutil.rmtree(mirror_path)
        elif mirror_path.exists():
            mirror_path.unlink()
        self._prune_empty_parents(mirror_path.parent)

    def _prune_empty_parents(self, directory: Path) -> None:
        while directory != self._mirror_root_path:
            if not directory.exists():
                directory = directory.parent
                continue
            try:
                next(directory.iterdir())
                return
            except StopIteration:
                directory.rmdir()
                directory = directory.parent

    def _remap_workspace_analysis(self, analysis: PythonWorkspaceAnalysis) -> PythonWorkspaceAnalysis:
        return PythonWorkspaceAnalysis(
            root=self.root,
            modules=tuple(self._remap_module_analysis(module) for module in analysis.modules),
        )

    def _remap_module_analysis(self, analysis: PythonModuleAnalysis) -> PythonModuleAnalysis:
        return PythonModuleAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            module=analysis.module,
            imports=analysis.imports,
            definitions=analysis.definitions,
            diagnostics=analysis.diagnostics,
            resolved_imports=tuple(
                ResolvedImportRef(
                    module=item.module,
                    kind=item.kind,
                    lineno=item.lineno,
                    imported_name=item.imported_name,
                    resolved_module=item.resolved_module,
                    resolved_path=self._remap_path(item.resolved_path),
                    resolution=item.resolution,
                    distribution_name=item.distribution_name,
                    distribution_version=item.distribution_version,
                )
                for item in analysis.resolved_imports
            ),
            dependencies=tuple(
                DependencySurface(
                    module=item.module,
                    path=self._remap_path(item.path) or item.path,
                    exports=item.exports,
                )
                for item in analysis.dependencies
            ),
        )

    def _remap_module_symbol_table(self, table: ModuleSymbolTable) -> ModuleSymbolTable:
        return ModuleSymbolTable(
            module=table.module,
            path=self._remap_path(table.path) or table.path,
            symbols=table.symbols,
            impurity_reasons=table.impurity_reasons,
        )

    def _remap_workspace_symbol_index(self, index: WorkspaceSymbolIndex) -> WorkspaceSymbolIndex:
        return WorkspaceSymbolIndex(
            root=self.root,
            entries=index.entries,
        )

    def _remap_resolved_symbol(self, symbol: ResolvedSymbol) -> ResolvedSymbol:
        return ResolvedSymbol(
            original_module=symbol.original_module,
            qualified_name=symbol.qualified_name,
            resolution=symbol.resolution,
            defining_module=symbol.defining_module,
            defining_path=self._remap_path(symbol.defining_path),
            defining_lineno=symbol.defining_lineno,
            distribution_name=symbol.distribution_name,
            distribution_version=symbol.distribution_version,
            follow_depth=symbol.follow_depth,
            trail=symbol.trail,
        )

    def _remap_config_analysis(self, analysis: ConfigAnalysis | None) -> ConfigAnalysis | None:
        if analysis is None:
            return None
        return ConfigAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            sections=analysis.sections,
            dependencies=analysis.dependencies,
            optional_dependency_groups=analysis.optional_dependency_groups,
            tool_configs=analysis.tool_configs,
            diagnostics=analysis.diagnostics,
        )

    def _remap_requirements_analysis(
        self, analysis: RequirementsAnalysis | None
    ) -> RequirementsAnalysis | None:
        if analysis is None:
            return None
        return RequirementsAnalysis(
            path=self._remap_path(analysis.path) or analysis.path,
            requirements=analysis.requirements,
            file_references=analysis.file_references,
            index_directives=analysis.index_directives,
            diagnostics=analysis.diagnostics,
        )

    def _remap_path(self, path: str | None) -> str | None:
        if path is None:
            return None
        candidate = Path(path)
        try:
            relative_path = candidate.relative_to(self._mirror_root_path)
        except ValueError:
            return path
        return str(Path(self.root, relative_path))

    def _dedupe_diagnostics(
        self, diagnostics: tuple[AnalysisDiagnostic, ...]
    ) -> tuple[AnalysisDiagnostic, ...]:
        seen: set[tuple[str, str, str, str, str, int | None, int | None]] = set()
        ordered: list[AnalysisDiagnostic] = []
        for diagnostic in diagnostics:
            key = (
                diagnostic.path,
                diagnostic.code,
                diagnostic.message,
                diagnostic.severity,
                diagnostic.source,
                diagnostic.lineno,
                diagnostic.col_offset,
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(diagnostic)
        return tuple(ordered)


class PollingWorkspaceWatcher:
    def __init__(
        self,
        session: WorkspaceSession,
        *,
        debounce_ms: int = 200,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._session = session
        self._debounce_seconds = debounce_ms / 1000.0
        self._clock = clock or time.monotonic
        self._snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
        )
        self._pending: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._on_change: Callable[[tuple[str, ...]], None] | None = None
        self._on_error: Callable[[Exception], None] | None = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def poll(self) -> tuple[str, ...]:
        if self.is_running:
            raise RuntimeError(
                "PollingWorkspaceWatcher is running; stop() it before calling poll() directly."
            )
        return self._poll_once()

    def _poll_once(self) -> tuple[str, ...]:
        now = self._clock()
        current_snapshot = _collect_filesystem_snapshot(
            self._session.root,
            self._session._ignored_dir_names,
        )

        changed_paths = {
            path
            for path in set(self._snapshot) | set(current_snapshot)
            if self._snapshot.get(path) != current_snapshot.get(path)
        }
        for path in changed_paths:
            self._pending[path] = now

        ready = tuple(
            sorted(
                path
                for path, seen_at in self._pending.items()
                if now - seen_at >= self._debounce_seconds
            )
        )
        for path in ready:
            self._pending.pop(path, None)
        if ready:
            self._session.refresh_paths(list(ready))

        self._snapshot = current_snapshot
        return ready

    def start(
        self,
        on_change: Callable[[tuple[str, ...]], None],
        *,
        interval_s: float | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        if self.is_running:
            raise RuntimeError("PollingWorkspaceWatcher is already running.")
        effective_interval = (
            interval_s if interval_s is not None else max(self._debounce_seconds / 2.0, 0.05)
        )
        self._on_change = on_change
        self._on_error = on_error
        self._stop_event = threading.Event()
        thread = threading.Thread(
            target=self._run,
            args=(effective_interval,),
            name="pyinc-tools-watcher",
            daemon=True,
        )
        self._thread = thread
        thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        thread.join(timeout)
        if thread.is_alive():
            print(
                "pyinc-tools watcher: thread did not stop within timeout",
                file=sys.stderr,
            )
        self._thread = None
        self._on_change = None
        self._on_error = None

    def __enter__(self) -> PollingWorkspaceWatcher:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.stop()

    def _run(self, interval_s: float) -> None:
        while not self._stop_event.is_set():
            try:
                ready = self._poll_once()
            except RuntimeError:
                # Session was closed out from under us; exit cleanly.
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._handle_error(exc)
                ready = ()
            if ready:
                callback = self._on_change
                if callback is not None:
                    try:
                        callback(ready)
                    except Exception as exc:
                        self._handle_error(exc)
            if self._stop_event.wait(interval_s):
                return

    def _handle_error(self, exc: Exception) -> None:
        if self._on_error is not None:
            with contextlib.suppress(Exception):  # pragma: no cover - defensive
                self._on_error(exc)
            return
        print(
            f"pyinc-tools watcher: callback raised: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
