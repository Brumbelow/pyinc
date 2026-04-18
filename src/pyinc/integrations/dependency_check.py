from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.integrations.installed_packages import (
    environment_index,
    installed_distributions_index,
)
from pyinc.integrations.requirement_evaluation import (
    _parse_specifier_set,
    _satisfies,
)
from pyinc.runtime import Database
from pyinc.value import thaw

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

DependencyStatusPayload: TypeAlias = tuple[str, str, str, str, str]
#   normalized_name, declared_spec, installed_version, status, detail

DependencyCheckPayload: TypeAlias = tuple[
    tuple[DependencyStatusPayload, ...],
    tuple[tuple[str, str], ...],  # diagnostics: (code, message)
]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyStatus:
    name: str
    declared_spec: str
    installed_version: str
    status: str  # "satisfied", "missing", "version_mismatch", "ambiguous"
    detail: str


@dataclass(frozen=True)
class UndeclaredImport:
    import_name: str
    distribution_name: str


@dataclass(frozen=True)
class DependencyCheckAnalysis:
    statuses: tuple[DependencyStatus, ...]
    undeclared_imports: tuple[UndeclaredImport, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# PEP 440 version matching — delegated to requirement_evaluation
# ---------------------------------------------------------------------------


def _check_version_constraints(
    declared_spec: str, installed_version: str
) -> tuple[str, str]:
    spec_set = _parse_specifier_set(declared_spec)
    if spec_set is None:
        return "ambiguous", f"cannot parse specifier: {declared_spec}"
    for op, _ver in spec_set:
        if op == "===":
            return "ambiguous", f"cannot evaluate: {op}{_ver}"
    ok, detail = _satisfies(spec_set, installed_version, include_prerelease=True)
    if not ok:
        if "unparseable" in detail or "cannot evaluate" in detail:
            return "ambiguous", detail
        return "version_mismatch", detail
    return "satisfied", detail


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEP_NAME_PAT = r"^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)"


def _normalize_dep_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _extract_dep_name_and_spec(specifier: str) -> tuple[str, str]:
    stripped = specifier.strip()
    # Strip environment markers (everything after ;)
    marker_pos = stripped.find(";")
    if marker_pos >= 0:
        stripped = stripped[:marker_pos].strip()
    # Strip URL-based specs (name @ url)
    at_pos = stripped.find("@")
    if at_pos >= 0:
        name_part = stripped[:at_pos].strip()
        m = re.match(_DEP_NAME_PAT, name_part)
        if m:
            return _normalize_dep_name(m.group(1)), ""
    # Extract name and version specifier
    m = re.match(_DEP_NAME_PAT, stripped)
    if m is None:
        return _normalize_dep_name(stripped), ""
    name = m.group(1)
    rest = stripped[m.end():].strip()
    # Strip extras [...]
    if rest.startswith("["):
        bracket_end = rest.find("]")
        if bracket_end >= 0:
            rest = rest[bracket_end + 1:].strip()
    return _normalize_dep_name(name), rest


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
def _declared_deps_payload(
    db: Database, deps: tuple[str, ...]
) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    for spec in deps:
        name, version_spec = _extract_dep_name_and_spec(spec)
        result.append((name, version_spec))
    return tuple(result)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def dependency_check_payload(
    db: Database, declared_deps: tuple[str, ...]
) -> DependencyCheckPayload:
    parsed_deps = _declared_deps_payload(db, declared_deps)
    dist_index_raw = installed_distributions_index(db)
    installed_map: dict[str, str] = dict(dist_index_raw)

    statuses: list[DependencyStatusPayload] = []
    diagnostics: list[tuple[str, str]] = []

    for name, version_spec in parsed_deps:
        installed_version = installed_map.get(name, "")
        if not installed_version:
            statuses.append((name, version_spec, "", "missing", "not installed"))
        elif not version_spec:
            statuses.append(
                (name, version_spec, installed_version, "satisfied", "installed, no constraint")
            )
        else:
            status, detail = _check_version_constraints(version_spec, installed_version)
            statuses.append((name, version_spec, installed_version, status, detail))

    return (tuple(statuses), tuple(diagnostics))


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_status(payload: DependencyStatusPayload) -> DependencyStatus:
    name, declared_spec, installed_version, status, detail = payload
    return DependencyStatus(
        name=name,
        declared_spec=declared_spec,
        installed_version=installed_version,
        status=status,
        detail=detail,
    )


def dependency_check_analysis(
    db: Database, declared_deps: tuple[str, ...]
) -> DependencyCheckAnalysis:
    """Check declared dependencies against installed packages."""
    raw = cast(
        DependencyCheckPayload,
        thaw(db.get(dependency_check_payload, declared_deps)),
    )
    statuses_raw, diagnostics = raw
    return DependencyCheckAnalysis(
        statuses=tuple(_decode_status(s) for s in statuses_raw),
        undeclared_imports=(),
        diagnostics=diagnostics,
    )


def workspace_dependency_check(
    db: Database, root: str, declared_deps: tuple[str, ...]
) -> DependencyCheckAnalysis:
    """Full dependency check including undeclared import detection.

    Composes with python_source.workspace_analysis at the entrypoint layer
    (not the query layer) to detect imports that are installed but not declared.
    """
    from pyinc.integrations.python_source import workspace_analysis

    base = dependency_check_analysis(db, declared_deps)

    ws = workspace_analysis(db, root)
    env_data = cast(
        tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]],
        thaw(environment_index(db)),
    )
    _, _ = env_data  # stdlib_modules, pkg_entries (unused directly)

    declared_names: set[str] = set()
    for status in base.statuses:
        declared_names.add(status.name)

    undeclared: list[UndeclaredImport] = []
    seen: set[str] = set()
    for module in ws.modules:
        for imp in module.resolved_imports:
            if imp.resolution == "installed" and imp.distribution_name is not None:
                norm_dist = _normalize_dep_name(imp.distribution_name)
                if norm_dist not in declared_names and norm_dist not in seen:
                    seen.add(norm_dist)
                    undeclared.append(
                        UndeclaredImport(
                            import_name=imp.module,
                            distribution_name=imp.distribution_name,
                        )
                    )

    return DependencyCheckAnalysis(
        statuses=base.statuses,
        undeclared_imports=tuple(sorted(undeclared, key=lambda u: u.distribution_name)),
        diagnostics=base.diagnostics,
    )


__all__ = [
    "DependencyCheckAnalysis",
    "DependencyStatus",
    "UndeclaredImport",
    "dependency_check_analysis",
    "workspace_dependency_check",
]
