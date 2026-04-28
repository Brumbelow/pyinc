from __future__ import annotations

import contextlib
import hashlib
import os
import re
import site
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource, _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import thaw

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

InstalledPackagePayload: TypeAlias = tuple[
    str, str, tuple[str, ...], tuple[str, ...], str
]
#                                          dist_name, version, top_level_names, requires_dist, summary

DiagnosticPayload: TypeAlias = tuple[str, str]
#                                    code, message

InstalledPackagesAnalysisPayload: TypeAlias = tuple[
    tuple[InstalledPackagePayload, ...],
    tuple[str, ...],
    tuple[DiagnosticPayload, ...],
]

InstalledDistributionsIndexPayload: TypeAlias = tuple[tuple[str, str], ...]
#                                                      (normalized_dist_name, version)

EnvironmentIndexPayload: TypeAlias = tuple[
    tuple[str, ...],  # stdlib_modules
    tuple[tuple[str, str, str], ...],  # (top_level_name, dist_name, version)
]

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstalledPackageRef:
    distribution_name: str
    version: str
    top_level_names: tuple[str, ...]
    requires_dist: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class ImportNameResolution:
    import_name: str
    origin: str  # "stdlib", "installed", "unknown"
    distribution_name: str | None
    distribution_version: str | None


@dataclass(frozen=True)
class InstalledPackagesAnalysis:
    packages: tuple[InstalledPackageRef, ...]
    stdlib_modules: tuple[str, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DistInfoMetadataResource:
    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"dist-info-metadata[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        file_path = Path(path)
        if not file_path.exists():
            return ("missing",)
        return ("present", hashlib.sha256(file_path.read_bytes()).hexdigest())

    def load(self, db: Database, path: str) -> str:
        file_path = Path(path)
        if not file_path.exists():
            return ""
        with db._allow_raw_open():
            return file_path.read_text(encoding="utf-8")

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = _file_read_snapshot(path, "utf-8")
        return probe, text if text is not None else ""


_METADATA = _DistInfoMetadataResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DIST_INFO_PAT = r"^(.+)-(.+)\.dist-info$"


def _normalize_dist_name(name: str) -> str:
    """PEP 503 normalize a distribution name for matching."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _dist_name_to_import_fallback(dist_name: str) -> str:
    """Heuristic: map distribution name to likely top-level import name."""
    return re.sub(r"[-_.]+", "_", dist_name).lower()


def _parse_metadata_field(text: str, field_name: str) -> str | None:
    """Extract a single-value field from email-style METADATA."""
    pat = r"^" + re.escape(field_name) + r": (.+)$"
    m = re.search(pat, text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_metadata_fields(text: str, field_name: str) -> tuple[str, ...]:
    """Extract all occurrences of a multi-value field from METADATA."""
    pat = r"^" + re.escape(field_name) + r": (.+)$"
    return tuple(m.group(1).strip() for m in re.finditer(pat, text, re.MULTILINE))


def _metadata_cutoff_token(text: str) -> tuple[str, ...]:
    """Cutoff: only the parsed field values, not comments or whitespace."""
    name = _parse_metadata_field(text, "Name") or ""
    version = _parse_metadata_field(text, "Version") or ""
    summary = _parse_metadata_field(text, "Summary") or ""
    requires = _parse_metadata_fields(text, "Requires-Dist")
    return (name, version, summary, *requires)


def _get_site_packages_dirs() -> tuple[str, ...]:
    """Discover site-packages directories from the current Python environment."""
    dirs: list[str] = []
    with contextlib.suppress(AttributeError):
        dirs.extend(site.getsitepackages())
    with contextlib.suppress(AttributeError):
        user_site = site.getusersitepackages()
        if isinstance(user_site, str):
            dirs.append(user_site)
    seen: set[str] = set()
    result: list[str] = []
    for d in dirs:
        real = os.path.realpath(d)
        if real not in seen and os.path.isdir(real):
            seen.add(real)
            result.append(real)
    return tuple(result)


def _get_stdlib_modules() -> tuple[str, ...]:
    """Return sorted stdlib module names (Python 3.10+)."""
    names: frozenset[str] = getattr(sys, "stdlib_module_names", frozenset())
    return tuple(sorted(names))


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
def _site_packages_dirs(db: Database) -> tuple[str, ...]:
    """Discover site-packages directories. Marks sys.path as untracked."""
    db.report_untracked_read("sys.path is a mutable runtime list")
    # site.getsitepackages() and site.getusersitepackages() check os.environ
    # (e.g. VIRTUAL_ENV) internally on Python 3.11+. Since this query already
    # declares all its reads as untracked, suppress the environ guard here.
    with db._allow_raw_open():
        return _get_site_packages_dirs()


@query
def _dist_info_listing(db: Database, site_dir: str) -> tuple[str, ...]:
    """List .dist-info directories in a site-packages via DirectoryResource."""
    entries = _DIRECTORIES.read(db, site_dir)
    return tuple(sorted(entry for entry in entries if re.match(_DIST_INFO_PAT, entry)))


@query(cutoff=_metadata_cutoff_token)
def _metadata_text(db: Database, metadata_path: str) -> str:
    """Read a dist-info METADATA file. Cutoff on parsed fields only."""
    return _METADATA.read(db, metadata_path)


@query
def _top_level_text(db: Database, top_level_path: str) -> str:
    """Read a dist-info top_level.txt file."""
    return _METADATA.read(db, top_level_path)


@query
def _package_metadata_payload(
    db: Database, site_dir: str, dist_info_name: str
) -> InstalledPackagePayload | None:
    """Parse a single package's metadata from its .dist-info directory."""
    dist_info_path = os.path.join(site_dir, dist_info_name)
    metadata_file = os.path.join(dist_info_path, "METADATA")
    top_level_file = os.path.join(dist_info_path, "top_level.txt")

    text = _metadata_text(db, metadata_file)
    if not text:
        return None

    name = _parse_metadata_field(text, "Name")
    version = _parse_metadata_field(text, "Version")
    if name is None or version is None:
        return None

    summary = _parse_metadata_field(text, "Summary") or ""
    requires_dist = _parse_metadata_fields(text, "Requires-Dist")

    # Determine top-level import names
    top_level_raw = _top_level_text(db, top_level_file)
    if top_level_raw.strip():
        top_level_names = tuple(
            line.strip() for line in top_level_raw.strip().splitlines() if line.strip()
        )
    else:
        # Fallback: derive from distribution name
        top_level_names = (_dist_name_to_import_fallback(name),)

    return (name, version, top_level_names, requires_dist, summary)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def _installed_packages_payload(db: Database) -> InstalledPackagesAnalysisPayload:
    """Compose all site-packages into a full package listing."""
    site_dirs = _site_packages_dirs(db)
    stdlib_modules = _get_stdlib_modules()

    packages: list[InstalledPackagePayload] = []
    diagnostics: list[DiagnosticPayload] = []

    for site_dir in site_dirs:
        dist_infos = _dist_info_listing(db, site_dir)
        for dist_info_name in dist_infos:
            payload = _package_metadata_payload(db, site_dir, dist_info_name)
            if payload is not None:
                packages.append(payload)
            else:
                diagnostics.append(
                    (
                        "metadata-parse-failed",
                        f"Could not parse metadata from {dist_info_name} in {site_dir}",
                    )
                )

    packages.sort(key=lambda p: _normalize_dist_name(p[0]))
    return (tuple(packages), stdlib_modules, tuple(diagnostics))


@query
def environment_index(db: Database) -> EnvironmentIndexPayload:
    """Environment classification data for cross-integration import resolution."""
    raw = _installed_packages_payload(db)
    packages_raw, stdlib_modules, _ = raw
    entries: list[tuple[str, str, str]] = []
    for pkg in packages_raw:
        dist_name, version, top_level_names, _, _ = pkg
        for tln in top_level_names:
            entries.append((tln, dist_name, version))
    return (stdlib_modules, tuple(sorted(entries)))


@query
def installed_distributions_index(db: Database) -> InstalledDistributionsIndexPayload:
    """Distribution-name-to-version index for cross-integration dependency validation."""
    raw = _installed_packages_payload(db)
    packages_raw, _, _ = raw
    entries: list[tuple[str, str]] = []
    for pkg in packages_raw:
        dist_name, version, _, _, _ = pkg
        entries.append((_normalize_dist_name(dist_name), version))
    return tuple(sorted(entries))


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_package(payload: InstalledPackagePayload) -> InstalledPackageRef:
    dist_name, version, top_level_names, requires_dist, summary = payload
    return InstalledPackageRef(
        distribution_name=dist_name,
        version=version,
        top_level_names=top_level_names,
        requires_dist=requires_dist,
        summary=summary,
    )


def installed_packages_analysis(db: Database) -> InstalledPackagesAnalysis:
    """Discover all installed packages and stdlib modules."""
    raw = cast(
        InstalledPackagesAnalysisPayload,
        thaw(db.get(_installed_packages_payload)),
    )
    packages_raw, stdlib_modules, diagnostics = raw
    return InstalledPackagesAnalysis(
        packages=tuple(_decode_package(p) for p in packages_raw),
        stdlib_modules=stdlib_modules,
        diagnostics=diagnostics,
    )


def resolve_import_name(db: Database, import_name: str) -> ImportNameResolution:
    """Resolve an import name to its origin: stdlib, installed, or unknown."""
    raw = cast(
        InstalledPackagesAnalysisPayload,
        thaw(db.get(_installed_packages_payload)),
    )
    packages_raw, stdlib_modules, _ = raw

    # Check stdlib first
    top_level = import_name.split(".")[0]
    if top_level in stdlib_modules:
        return ImportNameResolution(
            import_name=import_name,
            origin="stdlib",
            distribution_name=None,
            distribution_version=None,
        )

    # Check installed packages
    for pkg in packages_raw:
        dist_name, version, top_level_names, _, _ = pkg
        if top_level in top_level_names:
            return ImportNameResolution(
                import_name=import_name,
                origin="installed",
                distribution_name=dist_name,
                distribution_version=version,
            )

    return ImportNameResolution(
        import_name=import_name,
        origin="unknown",
        distribution_name=None,
        distribution_version=None,
    )


__all__ = [
    "ImportNameResolution",
    "InstalledPackageRef",
    "InstalledPackagesAnalysis",
    "environment_index",
    "installed_distributions_index",
    "installed_packages_analysis",
    "resolve_import_name",
]
