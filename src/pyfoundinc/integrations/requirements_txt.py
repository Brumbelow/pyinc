from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyfoundinc.core import query
from pyfoundinc.resources import DirectoryResource
from pyfoundinc.runtime import Database
from pyfoundinc.value import thaw

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

RequirementPayload: TypeAlias = tuple[str, str, int, tuple[str, ...], str, str, bool]
#                                      name, raw_line, lineno, extras, version_spec, markers, is_editable

FileReferencePayload: TypeAlias = tuple[str, str, int]
#                                       kind, path, lineno

IndexDirectivePayload: TypeAlias = tuple[str, str, int]
#                                        kind, url, lineno

DiagnosticPayload: TypeAlias = tuple[str, str]
#                                    code, message

RequirementsAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[RequirementPayload, ...],
    tuple[FileReferencePayload, ...],
    tuple[IndexDirectivePayload, ...],
    tuple[DiagnosticPayload, ...],
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RequirementRef:
    name: str
    raw_line: str
    lineno: int
    extras: tuple[str, ...]
    version_spec: str
    markers: str
    is_editable: bool


@dataclass(frozen=True)
class FileReference:
    kind: str
    path: str
    lineno: int


@dataclass(frozen=True)
class IndexDirective:
    kind: str
    url: str
    lineno: int


@dataclass(frozen=True)
class RequirementsAnalysis:
    path: str
    requirements: tuple[RequirementRef, ...]
    file_references: tuple[FileReference, ...]
    index_directives: tuple[IndexDirective, ...]
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RequirementsFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"requirementsfile[{path}]"

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
            return file_path.read_text(encoding=self.encoding)


_FILES = _RequirementsFileResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Regex pattern strings — kept as strings (not compiled re.Pattern objects)
# to avoid ambient-capture issues with the query runtime, which cannot freeze
# compiled Pattern objects.  Each helper compiles locally on first call.

_REQ_PAT = (
    r"^"
    r"(?P<name>[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[^\]]*)\])?"
    r"(?P<version>[^;@#]*?)"
    r"(?:\s*;\s*(?P<markers>.+))?"
    r"$"
)

_URL_REQ_PAT = (
    r"^"
    r"(?P<name>[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"(?:\[(?P<extras>[^\]]*)\])?"
    r"\s*@\s*(?P<url>\S+)"
    r"(?:\s*;\s*(?P<markers>.+))?"
    r"$"
)

_FILE_REF_PAT = r"^(?:-r|--requirement)\s+(.+)$"
_CONSTRAINT_REF_PAT = r"^(?:-c|--constraint)\s+(.+)$"
_EDITABLE_PAT = r"^(?:-e|--editable)\s+(.+)$"
_INDEX_URL_PAT = r"^--index-url\s+(.+)$"
_EXTRA_INDEX_PAT = r"^--extra-index-url\s+(.+)$"
_FIND_LINKS_PAT = r"^(?:-f|--find-links)\s+(.+)$"


def _normalize_name(name: str) -> str:
    """PEP 503 package name normalization."""
    return re.sub(r"[-_.]+", "_", name).lower()


def _resolve_continuations(text: str) -> str:
    """Join backslash-continued lines."""
    return text.replace("\\\n", "")


def _strip_inline_comment(line: str) -> str:
    """Remove inline comment from a requirement line.

    Inline comments start with `` #`` (space then hash) that is not
    inside a quoted marker string.
    """
    in_quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if in_quote is not None:
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
        elif ch == "#" and i > 0 and line[i - 1] == " ":
            return line[: i - 1].rstrip()
        i += 1
    return line


def _parse_requirement_line(line: str, lineno: int) -> RequirementPayload | None:
    """Parse a single PEP 508 specifier line into a RequirementPayload."""
    stripped = _strip_inline_comment(line.strip())
    if not stripped:
        return None

    # URL-based requirement (name @ url)
    url_match = re.match(_URL_REQ_PAT, stripped)
    if url_match:
        name = _normalize_name(url_match.group("name"))
        extras_str = url_match.group("extras") or ""
        extras = tuple(e.strip() for e in extras_str.split(",") if e.strip()) if extras_str else ()
        markers = (url_match.group("markers") or "").strip()
        url = url_match.group("url").strip()
        return (name, line.strip(), lineno, extras, f"@ {url}", markers, False)

    match = re.match(_REQ_PAT, stripped)
    if match is None:
        return None

    name = _normalize_name(match.group("name"))
    extras_str = match.group("extras") or ""
    extras = tuple(e.strip() for e in extras_str.split(",") if e.strip()) if extras_str else ()
    version_spec = match.group("version").strip()
    markers = (match.group("markers") or "").strip()

    return (name, line.strip(), lineno, extras, version_spec, markers, False)


def _parse_requirements(text: str) -> tuple[RequirementPayload, ...]:
    """Extract all requirement payloads from text."""
    resolved = _resolve_continuations(text)
    results: list[RequirementPayload] = []
    for lineno, line in enumerate(resolved.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Skip option lines and file references
        if stripped.startswith("-") or stripped.startswith("--"):
            # Check for editable installs
            editable_match = re.match(_EDITABLE_PAT, stripped)
            if editable_match:
                target = editable_match.group(1).strip()
                # Editable installs of local paths or VCS URLs are stored as-is
                results.append(
                    (_normalize_name(target) if not target.startswith((".", "/", "git+", "hg+", "svn+", "bzr+"))
                     else target,
                     stripped, lineno, (), "", "", True)
                )
            continue
        payload = _parse_requirement_line(stripped, lineno)
        if payload is not None:
            results.append(payload)
    return tuple(results)


def _parse_file_references(text: str) -> tuple[FileReferencePayload, ...]:
    """Extract -r/--requirement and -c/--constraint references."""
    resolved = _resolve_continuations(text)
    results: list[FileReferencePayload] = []
    for lineno, line in enumerate(resolved.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        req_match = re.match(_FILE_REF_PAT, stripped)
        if req_match:
            results.append(("requirement", req_match.group(1).strip(), lineno))
            continue
        con_match = re.match(_CONSTRAINT_REF_PAT, stripped)
        if con_match:
            results.append(("constraint", con_match.group(1).strip(), lineno))
    return tuple(results)


def _parse_index_directives(text: str) -> tuple[IndexDirectivePayload, ...]:
    """Extract --index-url, --extra-index-url, and --find-links directives."""
    resolved = _resolve_continuations(text)
    results: list[IndexDirectivePayload] = []
    for lineno, line in enumerate(resolved.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        idx_match = re.match(_INDEX_URL_PAT, stripped)
        if idx_match:
            results.append(("index-url", idx_match.group(1).strip(), lineno))
            continue
        extra_match = re.match(_EXTRA_INDEX_PAT, stripped)
        if extra_match:
            results.append(("extra-index-url", extra_match.group(1).strip(), lineno))
            continue
        fl_match = re.match(_FIND_LINKS_PAT, stripped)
        if fl_match:
            results.append(("find-links", fl_match.group(1).strip(), lineno))
    return tuple(results)


def _parse_diagnostics(text: str) -> tuple[DiagnosticPayload, ...]:
    """Identify unparseable non-blank, non-comment, non-option lines."""
    resolved = _resolve_continuations(text)
    results: list[DiagnosticPayload] = []
    for lineno, line in enumerate(resolved.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Known option/directive lines are not diagnostics
        if stripped.startswith(("-r ", "--requirement ", "-c ", "--constraint ",
                                "-e ", "--editable ",
                                "--index-url ", "--extra-index-url ",
                                "-f ", "--find-links ",
                                "--no-binary", "--only-binary",
                                "--prefer-binary", "--require-hashes",
                                "--pre", "--trusted-host",
                                "--no-deps", "--global-option",
                                "--hash=")):
            continue
        # Try parsing as a requirement
        payload = _parse_requirement_line(stripped, lineno)
        if payload is None:
            results.append(("unparseable-line", f"line {lineno}: {stripped}"))
    return tuple(results)


# ---------------------------------------------------------------------------
# Cutoff
# ---------------------------------------------------------------------------


def _requirements_cutoff_token(text: str) -> tuple[str, ...]:
    """Normalize requirements text for semantic comparison.

    Preserves line structure (blank lines and comment lines keep their
    positions) because downstream results include line numbers.  Only
    comment *text* and trailing whitespace are normalized so that edits
    to comment wording — without changing line count — are backdated.
    """
    resolved = _resolve_continuations(text)
    lines: list[str] = []
    for line in resolved.splitlines():
        stripped = line.strip()
        if not stripped:
            lines.append("")
        elif stripped.startswith("#"):
            lines.append("#")
        else:
            lines.append(_strip_inline_comment(stripped).rstrip())
    return tuple(lines)


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query(cutoff=_requirements_cutoff_token)
def requirements_file_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def requirements_payload(db: Database, path: str) -> tuple[RequirementPayload, ...]:
    text = requirements_file_text(db, path)
    return _parse_requirements(text)


@query
def file_references_payload(db: Database, path: str) -> tuple[FileReferencePayload, ...]:
    text = requirements_file_text(db, path)
    return _parse_file_references(text)


@query
def index_directives_payload(db: Database, path: str) -> tuple[IndexDirectivePayload, ...]:
    text = requirements_file_text(db, path)
    return _parse_index_directives(text)


@query
def requirements_diagnostics_payload(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = requirements_file_text(db, path)
    return _parse_diagnostics(text)


# ---------------------------------------------------------------------------
# Layer 2 — Composition
# ---------------------------------------------------------------------------


@query
def requirements_analysis_payload(db: Database, path: str) -> RequirementsAnalysisPayload:
    reqs = requirements_payload(db, path)
    refs = file_references_payload(db, path)
    indices = index_directives_payload(db, path)
    diagnostics = requirements_diagnostics_payload(db, path)
    return (path, reqs, refs, indices, diagnostics)


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_requirement(payload: RequirementPayload) -> RequirementRef:
    name, raw_line, lineno, extras, version_spec, markers, is_editable = payload
    return RequirementRef(
        name=name,
        raw_line=raw_line,
        lineno=lineno,
        extras=extras,
        version_spec=version_spec,
        markers=markers,
        is_editable=is_editable,
    )


def _decode_file_reference(payload: FileReferencePayload) -> FileReference:
    kind, path, lineno = payload
    return FileReference(kind=kind, path=path, lineno=lineno)


def _decode_index_directive(payload: IndexDirectivePayload) -> IndexDirective:
    kind, url, lineno = payload
    return IndexDirective(kind=kind, url=url, lineno=lineno)


def requirements_analysis(db: Database, path: str | os.PathLike[str]) -> RequirementsAnalysis:
    normalized = os.fspath(path)
    payload = cast(RequirementsAnalysisPayload, thaw(db.get(requirements_analysis_payload, normalized)))
    path_str, reqs, refs, indices, diagnostics = payload
    return RequirementsAnalysis(
        path=path_str,
        requirements=tuple(_decode_requirement(r) for r in reqs),
        file_references=tuple(_decode_file_reference(f) for f in refs),
        index_directives=tuple(_decode_index_directive(i) for i in indices),
        diagnostics=diagnostics,
    )


def workspace_requirements_analysis(
    db: Database, root: str | os.PathLike[str]
) -> RequirementsAnalysis | None:
    normalized_root = os.fspath(root)
    entries = _DIRECTORIES.read(db, normalized_root)
    for name in entries:
        if name == "requirements.txt":
            return requirements_analysis(db, str(Path(normalized_root) / name))
    return None


__all__ = [
    "FileReference",
    "IndexDirective",
    "RequirementRef",
    "RequirementsAnalysis",
    "requirements_analysis",
    "workspace_requirements_analysis",
]
