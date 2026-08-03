from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias, cast

from pyinc.core import query
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

from ._resources import file_probe, file_read_snapshot, file_text
from .source_geometry import SourcePosition, SourceRange

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
    range: SourceRange
    extras: tuple[str, ...]
    version_spec: str
    markers: str
    is_editable: bool


@dataclass(frozen=True)
class FileReference:
    kind: str
    path: str
    range: SourceRange


@dataclass(frozen=True)
class IndexDirective:
    kind: str
    url: str
    range: SourceRange


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
        return cast(str, db.read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"requirementsfile[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        return file_probe(path)

    def load(self, db: Database, path: str) -> str:
        text = file_text(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(self, db: Database, path: str) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


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


def _valid_file_reference_path(path: str) -> bool:
    return "\0" not in path


def _normalize_name(name: str) -> str:
    """Fold a package name to the underscore form `RequirementRef.name` carries.

    Runs of `-`, `_`, and `.` collapse to a single underscore and the result is
    lowercased. That is PEP 503 normalization with underscores where PEP 503
    specifies hyphens; the evaluation surfaces re-normalize to the hyphen form.
    """
    return re.sub(r"[-_.]+", "_", name).lower()


def _logical_lines(text: str) -> tuple[tuple[int, int, str], ...]:
    """Return ``(start_line, end_line, text)`` for physical/logical lines."""

    physical = text.splitlines()
    logical: list[tuple[int, int, str]] = []
    index = 0
    while index < len(physical):
        start = index + 1
        parts = [physical[index]]
        while parts[-1].endswith("\\") and index + 1 < len(physical):
            parts[-1] = parts[-1][:-1]
            index += 1
            parts.append(physical[index])
        logical.append((start, index + 1, "".join(parts)))
        index += 1
    return tuple(logical)


def _logical_line_ranges(text: str) -> dict[int, SourceRange]:
    physical = text.splitlines()
    ranges: dict[int, SourceRange] = {}
    for start_line, end_line, _ in _logical_lines(text):
        first = physical[start_line - 1]
        last = physical[end_line - 1]
        start_character = len(first) - len(first.lstrip())
        end_character = len(last.rstrip())
        start = SourcePosition(start_line - 1, start_character)
        end = SourcePosition(end_line - 1, end_character)
        if end < start:
            end = start
        ranges[start_line] = SourceRange(start, end)
    return ranges


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
        elif ch == "#" and i > 0 and line[i - 1].isspace():
            return line[:i].rstrip()
        i += 1
    return line


def _strip_requirement_options(line: str) -> str:
    """Remove pip per-requirement options from a requirement line.

    pip's requirements-file grammar places options such as ``--hash=...``
    after the requirement, whitespace-separated — pip-compile emits them on
    backslash continuation lines, which ``_logical_lines`` joins back into
    the requirement line.  A ``--`` token never occurs inside a PEP 508
    requirement, so everything from the first whitespace-delimited ``--``
    token onward is option text, not specifier text.
    """
    match = re.search(r"(?:^|\s)--", line)
    if match is None:
        return line
    return line[: match.start()].rstrip()


def _parse_requirement_line(line: str, lineno: int) -> RequirementPayload | None:
    """Parse a single PEP 508 specifier line into a RequirementPayload."""
    stripped = _strip_requirement_options(_strip_inline_comment(line.strip()))
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
    results: list[RequirementPayload] = []
    for lineno, _end_lineno, line in _logical_lines(text):
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
                    (
                        (
                            _normalize_name(target)
                            if not target.startswith((".", "/", "git+", "hg+", "svn+", "bzr+"))
                            else target
                        ),
                        stripped,
                        lineno,
                        (),
                        "",
                        "",
                        True,
                    )
                )
            continue
        payload = _parse_requirement_line(stripped, lineno)
        if payload is not None:
            results.append(payload)
    return tuple(results)


def _parse_file_references(text: str) -> tuple[FileReferencePayload, ...]:
    """Extract -r/--requirement and -c/--constraint references."""
    results: list[FileReferencePayload] = []
    for lineno, _end_lineno, line in _logical_lines(text):
        stripped = _strip_inline_comment(line.strip())
        if not stripped or stripped.startswith("#"):
            continue
        req_match = re.match(_FILE_REF_PAT, stripped)
        if req_match:
            path = req_match.group(1).strip()
            if _valid_file_reference_path(path):
                results.append(("requirement", path, lineno))
            continue
        con_match = re.match(_CONSTRAINT_REF_PAT, stripped)
        if con_match:
            path = con_match.group(1).strip()
            if _valid_file_reference_path(path):
                results.append(("constraint", path, lineno))
    return tuple(results)


def _parse_index_directives(text: str) -> tuple[IndexDirectivePayload, ...]:
    """Extract --index-url, --extra-index-url, and --find-links directives."""
    results: list[IndexDirectivePayload] = []
    for lineno, _end_lineno, line in _logical_lines(text):
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
    results: list[DiagnosticPayload] = []
    for lineno, _end_lineno, line in _logical_lines(text):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        reference_line = _strip_inline_comment(stripped)
        reference_match = re.match(_FILE_REF_PAT, reference_line) or re.match(
            _CONSTRAINT_REF_PAT, reference_line
        )
        if reference_match is not None and not _valid_file_reference_path(
            reference_match.group(1).strip()
        ):
            results.append(("unparseable-line", f"line {lineno}: {stripped}"))
            continue
        # Known option/directive lines are not diagnostics
        if stripped.startswith(
            (
                "-r ",
                "--requirement ",
                "-c ",
                "--constraint ",
                "-e ",
                "--editable ",
                "--index-url ",
                "--extra-index-url ",
                "-f ",
                "--find-links ",
                "--no-binary",
                "--only-binary",
                "--prefer-binary",
                "--require-hashes",
                "--pre",
                "--trusted-host",
                "--no-deps",
                "--global-option",
                "--hash=",
            )
        ):
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
    lines: list[str] = []
    for line in text.splitlines():
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


def _range_for_line(ranges: dict[int, SourceRange], lineno: int) -> SourceRange:
    source_range = ranges.get(lineno)
    if source_range is not None:
        return source_range
    position = SourcePosition(max(lineno - 1, 0), 0)
    return SourceRange(position, position)


def _decode_requirement(
    payload: RequirementPayload, ranges: dict[int, SourceRange]
) -> RequirementRef:
    name, raw_line, lineno, extras, version_spec, markers, is_editable = payload
    return RequirementRef(
        name=name,
        raw_line=raw_line,
        range=_range_for_line(ranges, lineno),
        extras=extras,
        version_spec=version_spec,
        markers=markers,
        is_editable=is_editable,
    )


def _decode_file_reference(
    payload: FileReferencePayload, ranges: dict[int, SourceRange]
) -> FileReference:
    kind, path, lineno = payload
    return FileReference(kind=kind, path=path, range=_range_for_line(ranges, lineno))


def _decode_index_directive(
    payload: IndexDirectivePayload, ranges: dict[int, SourceRange]
) -> IndexDirective:
    kind, url, lineno = payload
    return IndexDirective(kind=kind, url=url, range=_range_for_line(ranges, lineno))


def requirements_analysis(db: Database, path: str | os.PathLike[str]) -> RequirementsAnalysis:
    normalized = os.fspath(path)
    payload = cast(
        RequirementsAnalysisPayload,
        thaw(db.get(requirements_analysis_payload, normalized)),
    )
    path_str, reqs, refs, indices, diagnostics = payload
    ranges = _logical_line_ranges(requirements_file_text(db, normalized))
    return RequirementsAnalysis(
        path=path_str,
        requirements=tuple(_decode_requirement(r, ranges) for r in reqs),
        file_references=tuple(_decode_file_reference(f, ranges) for f in refs),
        index_directives=tuple(_decode_index_directive(i, ranges) for i in indices),
        diagnostics=diagnostics,
    )


def workspace_requirements_analysis(
    db: Database, root: str | os.PathLike[str]
) -> RequirementsAnalysis | None:
    normalized_root = os.fspath(root)
    entries = _DIRECTORIES.read(db, normalized_root)
    for name in entries:
        if name == "requirements.txt":
            return deep_requirements_analysis(db, str(Path(normalized_root) / name))
    return None


def deep_requirements_analysis(db: Database, path: str | os.PathLike[str]) -> RequirementsAnalysis:
    """Follow -r/--requirement references recursively, merging all requirements.

    Composes at the entrypoint layer: calls requirements_analysis() for each
    file in the chain, merges results.  Cycle detection via canonical path set.
    Constraint files (-c) are noted as file references but not followed.
    """
    root = Path(os.fspath(path)).resolve()
    project_root = root.parent
    all_requirements: dict[str, RequirementRef] = {}
    all_file_references: list[FileReference] = []
    all_index_directives: list[IndexDirective] = []
    all_diagnostics: list[tuple[str, str]] = []
    visited: set[str] = set()
    active: set[str] = set()

    def _walk(file_path: Path) -> None:
        canonical = str(file_path.resolve())
        if canonical in visited:
            return
        if canonical in active:
            all_diagnostics.append(("cycle", f"circular -r reference: {canonical}"))
            return
        if not file_path.is_file():
            all_diagnostics.append(
                (
                    "missing-requirements-file",
                    f"referenced requirements file is missing: {canonical}",
                )
            )
            return
        active.add(canonical)

        analysis = requirements_analysis(db, str(file_path))
        all_file_references.extend(analysis.file_references)
        all_index_directives.extend(analysis.index_directives)
        all_diagnostics.extend(analysis.diagnostics)

        # Walk referenced files first so the including file's requirements
        # take precedence (last-wins deduplication by name).
        for ref in analysis.file_references:
            if ref.kind == "requirement":
                if not _valid_file_reference_path(ref.path):
                    continue
                ref_path = Path(ref.path)
                if not ref_path.is_absolute():
                    ref_path = file_path.parent / ref_path
                ref_path = ref_path.resolve()
                try:
                    ref_path.relative_to(project_root)
                except ValueError:
                    all_diagnostics.append(("error", f"-r path outside project: {ref_path}"))
                    continue
                _walk(ref_path)

        for req in analysis.requirements:
            all_requirements[req.name] = req
        active.remove(canonical)
        visited.add(canonical)

    _walk(root)

    return RequirementsAnalysis(
        path=str(root),
        requirements=tuple(all_requirements.values()),
        file_references=tuple(all_file_references),
        index_directives=tuple(all_index_directives),
        diagnostics=tuple(all_diagnostics),
    )


__all__ = [
    "FileReference",
    "IndexDirective",
    "RequirementPayload",
    "RequirementRef",
    "RequirementsAnalysis",
    "deep_requirements_analysis",
    "requirements_payload",
    "requirements_analysis",
    "workspace_requirements_analysis",
]
