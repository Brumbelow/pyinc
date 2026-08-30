from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.integrations.installed_packages import environment_index
from pyinc.resources import DirectoryResource, FileStatResource, ResolvedPathResource
from pyinc.runtime import Database
from pyinc.value import thaw

from ._decoding import _reject_in_query
from ._resources import file_probe, file_read_snapshot, file_text

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

ModulePathEntryPayload: TypeAlias = tuple[str, str]
#                                         path, source

PthDirectivePayload: TypeAlias = tuple[str, str, str]
#                                       source_file, kind, value

NamespacePackagePayload: TypeAlias = tuple[str, tuple[str, ...]]
#                                           dotted_name, contributing_paths

ResolvedModuleLocationPayload: TypeAlias = tuple[
    str,  # dotted_name
    str,  # kind
    str | None,  # file_path
    str | None,  # directory_path
    tuple[str, ...],  # contributing_paths (non-empty only for namespace-package)
    str | None,  # distribution_name
    str | None,  # distribution_version
]

DiagnosticPayload: TypeAlias = tuple[str, str]

DeepModuleResolutionAnalysisPayload: TypeAlias = tuple[
    tuple[ModulePathEntryPayload, ...],
    tuple[PthDirectivePayload, ...],
    tuple[NamespacePackagePayload, ...],
    tuple[DiagnosticPayload, ...],
]

PthParsePayload: TypeAlias = tuple[tuple[str, ...], tuple[str, ...]]
#                                   resolved_paths,  exec_lines

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModulePathEntry:
    path: str
    source: str  # "sys.path", "pth"


@dataclass(frozen=True)
class PthDirective:
    source_file: str
    kind: str  # "path", "exec"
    value: str


@dataclass(frozen=True)
class NamespacePackage:
    dotted_name: str
    contributing_paths: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedModuleLocation:
    dotted_name: str
    kind: str  # "regular-module", "regular-package", "namespace-package",
    #          # "stdlib", "missing", "ambiguous"
    file_path: str | None
    directory_path: str | None
    contributing_paths: tuple[str, ...]
    distribution_name: str | None
    distribution_version: str | None


@dataclass(frozen=True)
class DeepModuleResolutionAnalysis:
    entries: tuple[ModulePathEntry, ...]
    pth_directives: tuple[PthDirective, ...]
    namespace_packages: tuple[NamespacePackage, ...]
    diagnostics: tuple[DiagnosticPayload, ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PthFileResource:
    """Read a .pth file, returning empty string when absent."""

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"pth-file[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, "utf-8")
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, "utf-8")
        return probe, text if text is not None else ""


_DIRECTORIES = DirectoryResource()
_FILES = _PthFileResource()
_FILESTAT = FileStatResource()
_RESOLVED = ResolvedPathResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _canonical_path(db: Database, path: str) -> str:
    """Resolve ``path`` as a tracked read; an unresolvable path is its own key.

    Canonical paths dedupe search-path entries and suppress traversal cycles,
    so a symlink retarget has to invalidate the queries that used them.
    """
    resolved = _RESOLVED.read(db, path)
    return resolved if resolved is not None else path


def _parse_pth_content(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    paths: list[str] = []
    exec_lines: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if (
            stripped.startswith("import ")
            or stripped.startswith("import\t")
            or stripped == "import"
        ):
            exec_lines.append(stripped)
            continue
        paths.append(stripped)
    return tuple(paths), tuple(exec_lines)


def _get_sys_path_entries() -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in sys.path:
        if not isinstance(raw, str) or not raw:
            continue
        real = os.path.realpath(raw)
        if real in seen:
            continue
        if not os.path.isdir(real):
            continue
        seen.add(real)
        result.append(real)
    return tuple(result)


def _path_exists(db: Database, path: str) -> bool:
    return _FILESTAT.read(db, path).exists


def _directory_exists(db: Database, path: str) -> bool:
    if not _path_exists(db, path):
        return False
    try:
        _DIRECTORIES.read(db, path)
    except (NotADirectoryError, FileNotFoundError):
        return False
    return True


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
def _raw_sys_path_entries(db: Database) -> tuple[str, ...]:
    """sys.path entries filtered to existing directories. Marks sys.path untracked."""
    db.report_untracked_read("sys.path is a mutable runtime list")
    return _get_sys_path_entries()


@query
def _pth_listing(db: Database, directory: str) -> tuple[str, ...]:
    """.pth filenames in a directory, sorted."""
    try:
        entries = _DIRECTORIES.read(db, directory)
    except (NotADirectoryError, FileNotFoundError):
        return tuple()
    return tuple(sorted(entry for entry in entries if entry.endswith(".pth")))


@query
def _pth_file_text(db: Database, pth_path: str) -> str:
    """Raw text of a .pth file, exactly as written.

    Comment and whitespace edits are absorbed one layer up, by
    `_pth_directives_payload`, which re-derives an equal set of directives from
    the new text and backdates on it.
    """
    return _FILES.read(db, pth_path)


@query
def _pth_directives_payload(db: Database, pth_path: str) -> PthParsePayload:
    """Parse one .pth file into (resolved_paths, exec_lines)."""
    text = _pth_file_text(db, pth_path)
    paths, exec_lines = _parse_pth_content(text)
    if exec_lines:
        db.report_untracked_read(f"pth exec line in {pth_path}")
    base_dir = str(Path(pth_path).parent)
    resolved: list[str] = []
    for raw_path in paths:
        candidate = (
            raw_path
            if os.path.isabs(raw_path)
            else os.path.normpath(os.path.join(base_dir, raw_path))
        )
        resolved.append(candidate)
    return tuple(resolved), exec_lines


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def _effective_search_paths_payload(db: Database) -> tuple[ModulePathEntryPayload, ...]:
    """sys.path entries plus .pth-expanded directories, ordered and deduplicated."""
    base = _raw_sys_path_entries(db)
    seen: set[str] = set()
    results: list[ModulePathEntryPayload] = []
    for entry in base:
        canonical = _canonical_path(db, entry)
        if canonical in seen:
            continue
        seen.add(canonical)
        results.append((canonical, "sys.path"))

    for entry, _ in list(results):
        pth_names = _pth_listing(db, entry)
        for pth_name in pth_names:
            pth_path = os.path.join(entry, pth_name)
            extras, _ = _pth_directives_payload(db, pth_path)
            for extra in extras:
                canonical = _canonical_path(db, extra)
                if canonical in seen:
                    continue
                if not _directory_exists(db, canonical):
                    continue
                seen.add(canonical)
                results.append((canonical, "pth"))
    return tuple(results)


@query
def _all_pth_directives_payload(db: Database) -> tuple[PthDirectivePayload, ...]:
    """Collected .pth directives across sys.path entries (for the analysis view)."""
    base = _raw_sys_path_entries(db)
    directives: list[PthDirectivePayload] = []
    for entry in base:
        pth_names = _pth_listing(db, entry)
        for pth_name in pth_names:
            pth_path = os.path.join(entry, pth_name)
            extras, exec_lines = _pth_directives_payload(db, pth_path)
            for extra in extras:
                directives.append((pth_path, "path", extra))
            for line in exec_lines:
                directives.append((pth_path, "exec", line))
    return tuple(directives)


def _classify_candidate(db: Database, entry: str, name: str) -> tuple[str, str] | None:
    """Return ``(path, kind)`` where ``kind`` is ``package``, ``module``, or
    ``namespace-dir``, preferring the CPython precedence (package > module >
    namespace). ``None`` means nothing matches.
    """
    pkg_dir = os.path.join(entry, name)
    dir_is_real = _directory_exists(db, pkg_dir)

    if dir_is_real:
        init_file = os.path.join(pkg_dir, "__init__.py")
        if _path_exists(db, init_file):
            return pkg_dir, "package"

    module_file = os.path.join(entry, f"{name}.py")
    if _path_exists(db, module_file):
        return module_file, "module"

    if dir_is_real:
        return pkg_dir, "namespace-dir"
    return None


def _descend(
    db: Database,
    dirs: tuple[str, ...],
    name: str,
    *,
    visited: set[str],
) -> tuple[list[str], str | None, str | None]:
    """Descend one component across each of ``dirs`` in order.

    Returns ``(namespace_dirs, module_file, package_dir)``. First regular
    package or regular module wins (sys.path ordering); all encountered
    namespace-dir contributions are collected.
    """
    namespace_dirs: list[str] = []
    for base in dirs:
        candidate = _classify_candidate(db, base, name)
        if candidate is None:
            continue
        path, kind = candidate
        canonical = _canonical_path(db, path)
        if canonical in visited:
            continue
        visited.add(canonical)
        if kind == "package":
            return [], None, path
        if kind == "module":
            return [], path, None
        namespace_dirs.append(path)
    return namespace_dirs, None, None


@query
def resolve_module_location(db: Database, dotted_name: str) -> ResolvedModuleLocationPayload:
    """Resolve ``dotted_name`` to a module file, package directory, or namespace.

    Cross-integration composition query: exported in this module's ``__all__``
    but intentionally not re-exported from ``pyinc.integrations``.
    """
    if not dotted_name:
        return (dotted_name, "missing", None, None, (), None, None)

    parts = dotted_name.split(".")
    top_level = parts[0]

    env_data = cast(
        tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]],
        thaw(environment_index(db)),
    )
    stdlib_modules, pkg_entries = env_data
    if top_level in frozenset(stdlib_modules):
        return (dotted_name, "stdlib", None, None, (), None, None)

    dist_lookup = {name: (dist, ver) for name, dist, ver in pkg_entries}
    search_paths = tuple(path for path, _ in _effective_search_paths_payload(db))

    visited: set[str] = set()
    current_dirs: tuple[str, ...] = search_paths
    namespace_paths: list[str] = []
    module_file: str | None = None
    package_dir: str | None = None

    for index, name in enumerate(parts):
        ns, mod, pkg = _descend(db, current_dirs, name, visited=visited)
        is_last = index == len(parts) - 1
        if pkg is not None:
            if is_last:
                package_dir = pkg
                namespace_paths = []
                module_file = None
                break
            current_dirs = (pkg,)
            namespace_paths = []
            continue
        if mod is not None and is_last:
            module_file = mod
            break
        if ns:
            if is_last:
                namespace_paths = ns
                break
            current_dirs = tuple(ns)
            continue
        return (dotted_name, "missing", None, None, (), None, None)

    dist_name: str | None = None
    dist_ver: str | None = None
    if top_level in dist_lookup:
        dist_name, dist_ver = dist_lookup[top_level]

    if package_dir is not None:
        return (
            dotted_name,
            "regular-package",
            os.path.join(package_dir, "__init__.py"),
            package_dir,
            (),
            dist_name,
            dist_ver,
        )
    if module_file is not None:
        return (
            dotted_name,
            "regular-module",
            module_file,
            None,
            (),
            dist_name,
            dist_ver,
        )
    if namespace_paths:
        return (
            dotted_name,
            "namespace-package",
            None,
            None,
            tuple(namespace_paths),
            dist_name,
            dist_ver,
        )
    return (dotted_name, "missing", None, None, (), None, None)


_SKIP_NAMESPACE_SUFFIXES = (
    ".py",
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".dist-info",
    ".egg-info",
    ".egg-link",
    ".pth",
    ".txt",
)


@query
def _top_level_namespace_packages_payload(
    db: Database,
) -> tuple[NamespacePackagePayload, ...]:
    """Top-level bare directories (no ``__init__.py``) shared across search paths."""
    search_paths = tuple(path for path, _ in _effective_search_paths_payload(db))
    name_to_dirs: dict[str, list[str]] = {}
    regular_names: set[str] = set()
    for entry in search_paths:
        try:
            children = _DIRECTORIES.read(db, entry)
        except (NotADirectoryError, FileNotFoundError):
            continue
        for child in children:
            if child.startswith("."):
                continue
            if any(child.endswith(suffix) for suffix in _SKIP_NAMESPACE_SUFFIXES):
                continue
            child_path = os.path.join(entry, child)
            if not _directory_exists(db, child_path):
                continue
            if _path_exists(db, os.path.join(child_path, "__init__.py")):
                regular_names.add(child)
                continue
            name_to_dirs.setdefault(child, []).append(child_path)

    result: list[NamespacePackagePayload] = []
    for name, dirs in sorted(name_to_dirs.items()):
        if name in regular_names:
            continue
        result.append((name, tuple(sorted(dirs))))
    return tuple(result)


@query
def _deep_analysis_payload(db: Database) -> DeepModuleResolutionAnalysisPayload:
    entries = _effective_search_paths_payload(db)
    directives = _all_pth_directives_payload(db)
    namespaces = _top_level_namespace_packages_payload(db)
    diagnostics: list[DiagnosticPayload] = []
    exec_count = sum(1 for _source, kind, _value in directives if kind == "exec")
    if exec_count:
        diagnostics.append(
            ("pth-exec-lines", f"{exec_count} .pth exec line(s) treated as untracked"),
        )
    return entries, directives, namespaces, tuple(diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_entry(payload: ModulePathEntryPayload) -> ModulePathEntry:
    path, source = payload
    return ModulePathEntry(path=path, source=source)


def _decode_pth(payload: PthDirectivePayload) -> PthDirective:
    source_file, kind, value = payload
    return PthDirective(source_file=source_file, kind=kind, value=value)


def _decode_namespace(payload: NamespacePackagePayload) -> NamespacePackage:
    dotted_name, paths = payload
    return NamespacePackage(dotted_name=dotted_name, contributing_paths=paths)


def _decode_location(payload: ResolvedModuleLocationPayload) -> ResolvedModuleLocation:
    (
        dotted_name,
        kind,
        file_path,
        directory_path,
        contributing_paths,
        dist_name,
        dist_ver,
    ) = payload
    return ResolvedModuleLocation(
        dotted_name=dotted_name,
        kind=kind,
        file_path=file_path,
        directory_path=directory_path,
        contributing_paths=contributing_paths,
        distribution_name=dist_name,
        distribution_version=dist_ver,
    )


def resolve_module_path(db: Database, dotted_name: str) -> ResolvedModuleLocation:
    """Resolve a dotted module name to its file or namespace contributions."""
    _reject_in_query(db, "resolve_module_path")
    payload = cast(
        ResolvedModuleLocationPayload,
        thaw(db.get(resolve_module_location, dotted_name)),
    )
    return _decode_location(payload)


def deep_module_resolution_analysis(db: Database) -> DeepModuleResolutionAnalysis:
    """Snapshot of effective module search paths, .pth directives, and namespaces."""
    _reject_in_query(db, "deep_module_resolution_analysis")
    payload = cast(
        DeepModuleResolutionAnalysisPayload,
        thaw(db.get(_deep_analysis_payload)),
    )
    entries, directives, namespaces, diagnostics = payload
    return DeepModuleResolutionAnalysis(
        entries=tuple(_decode_entry(e) for e in entries),
        pth_directives=tuple(_decode_pth(d) for d in directives),
        namespace_packages=tuple(_decode_namespace(n) for n in namespaces),
        diagnostics=diagnostics,
    )


__all__ = [
    "DeepModuleResolutionAnalysis",
    "ModulePathEntry",
    "NamespacePackage",
    "PthDirective",
    "ResolvedModuleLocation",
    "deep_module_resolution_analysis",
    "resolve_module_location",
    "resolve_module_path",
]
