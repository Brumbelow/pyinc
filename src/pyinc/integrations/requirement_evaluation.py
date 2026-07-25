from __future__ import annotations

import os
import platform
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, cast

from pyinc.core import query
from pyinc.integrations._pep440 import (
    compare_versions,
    parse_specifier_set,
    parse_version,
    satisfies,
)
from pyinc.integrations.installed_packages import installed_distributions_index
from pyinc.integrations.requirements_txt import (
    RequirementPayload,
    requirements_payload,
)
from pyinc.resources import DirectoryResource
from pyinc.runtime import Database
from pyinc.value import thaw

# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

PythonEnvironmentPayload: TypeAlias = tuple[str, str, str, str, str, str, str, str, str, str, str]
#   python_version, python_full_version, implementation_name, implementation_version,
#   os_name, sys_platform, platform_system, platform_release, platform_machine,
#   platform_python_implementation, platform_version

MarkerEvaluationPayload: TypeAlias = tuple[str, bool, tuple[tuple[str, str], ...]]
#                                          marker, value, diagnostics

VersionSpecifierEvalPayload: TypeAlias = tuple[str, str, bool, str]
#                                               specifier, version, satisfied, detail

ApplicableRequirementPayload: TypeAlias = tuple[str, str, str, bool, str, str, str]
#  name, version_spec, markers, applicable, installed_version, status, detail

ApplicableRequirementsAnalysisPayload: TypeAlias = tuple[
    str,
    tuple[ApplicableRequirementPayload, ...],
    PythonEnvironmentPayload,
    tuple[tuple[str, str], ...],
]


ApplicableStatus: TypeAlias = Literal[
    "satisfied", "missing", "version_mismatch", "ambiguous", "not_applicable"
]


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MarkerEvaluation:
    marker: str
    value: bool
    diagnostics: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class VersionSpecifierEvaluation:
    specifier: str
    version: str
    satisfied: bool
    detail: str


@dataclass(frozen=True)
class PythonEnvironmentSnapshot:
    python_version: str
    python_full_version: str
    implementation_name: str
    implementation_version: str
    os_name: str
    sys_platform: str
    platform_system: str
    platform_release: str
    platform_machine: str
    platform_python_implementation: str
    platform_version: str


@dataclass(frozen=True)
class ApplicableRequirement:
    name: str
    version_spec: str
    markers: str
    applicable: bool
    installed_version: str
    status: str
    detail: str


@dataclass(frozen=True)
class ApplicableRequirementsAnalysis:
    path: str
    requirements: tuple[ApplicableRequirement, ...]
    environment: PythonEnvironmentSnapshot
    diagnostics: tuple[tuple[str, str], ...]


# ---------------------------------------------------------------------------
# Environment reading (single point for monkeypatching in tests)
# ---------------------------------------------------------------------------


def _current_python_env() -> PythonEnvironmentPayload:
    impl = sys.implementation
    impl_version = ".".join(str(x) for x in impl.version[:3])
    full_version = ".".join(str(x) for x in sys.version_info[:3])
    short_version = ".".join(str(x) for x in sys.version_info[:2])
    return (
        short_version,
        full_version,
        impl.name,
        impl_version,
        os.name,
        sys.platform,
        platform.system(),
        platform.release(),
        platform.machine(),
        platform.python_implementation(),
        platform.version(),
    )


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PythonEnvironmentResource:
    def read(self, db: Database) -> PythonEnvironmentPayload:
        return cast(PythonEnvironmentPayload, db.read_resource(self, "python"))

    def label(self, _key: str) -> str:
        return "py-env"

    def probe(self, _key: str) -> PythonEnvironmentPayload:
        return _current_python_env()

    def load(self, _db: Database, _key: str) -> PythonEnvironmentPayload:
        return _current_python_env()

    def probe_and_load(
        self, _db: Database, _key: str
    ) -> tuple[PythonEnvironmentPayload, PythonEnvironmentPayload]:
        value = _current_python_env()
        return value, value


_PY_ENV = _PythonEnvironmentResource()
_DIRECTORIES = DirectoryResource()


# ---------------------------------------------------------------------------
# PEP 508 — marker tokenization and parsing
# ---------------------------------------------------------------------------

_TOK_NAME_PAT = r"[A-Za-z_][A-Za-z0-9_]*"


def _tokenize_marker(text: str) -> list[tuple[str, str]] | None:
    tokens: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN", "("))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN", ")"))
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            j = i + 1
            while j < n and text[j] != quote:
                j += 1
            if j >= n:
                return None
            tokens.append(("STRING", text[i + 1 : j]))
            i = j + 1
            continue
        if ch in "=!<>~":
            if text[i : i + 3] == "===":
                tokens.append(("OP", "==="))
                i += 3
                continue
            two = text[i : i + 2]
            if two in ("==", "!=", "<=", ">=", "~="):
                tokens.append(("OP", two))
                i += 2
                continue
            if ch in "<>":
                tokens.append(("OP", ch))
                i += 1
                continue
            return None
        m = re.match(_TOK_NAME_PAT, text[i:])
        if m is None:
            return None
        word = m.group(0)
        if word in ("and", "or"):
            tokens.append(("BOOL", word))
        elif word == "in":
            tokens.append(("OP", "in"))
        elif word == "not":
            # Only 'not in' is valid in PEP 508; peek ahead for 'in'
            j = i + len(word)
            while j < n and text[j].isspace():
                j += 1
            if text[j : j + 2] == "in" and (
                j + 2 >= n or not text[j + 2].isalnum() and text[j + 2] != "_"
            ):
                tokens.append(("OP", "not in"))
                i = j + 2
                continue
            return None
        else:
            tokens.append(("NAME", word))
        i += len(word)
    tokens.append(("EOF", ""))
    return tokens


@dataclass(frozen=True)
class _CompareNode:
    left_kind: str  # "name" or "string"
    left: str
    op: str
    right_kind: str
    right: str


@dataclass(frozen=True)
class _AndNode:
    children: tuple[_MarkerNode, ...]


@dataclass(frozen=True)
class _OrNode:
    children: tuple[_MarkerNode, ...]


_MarkerNode: TypeAlias = _CompareNode | _AndNode | _OrNode


class _MarkerParser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.pos = 0

    def _peek(self) -> tuple[str, str]:
        return self.tokens[self.pos]

    def _advance(self) -> tuple[str, str]:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def _expect(self, kind: str) -> tuple[str, str] | None:
        if self._peek()[0] != kind:
            return None
        return self._advance()

    def parse(self) -> _MarkerNode | None:
        node = self._parse_or()
        if node is None:
            return None
        if self._peek()[0] != "EOF":
            return None
        return node

    def _parse_or(self) -> _MarkerNode | None:
        first = self._parse_and()
        if first is None:
            return None
        children: list[_MarkerNode] = [first]
        while self._peek() == ("BOOL", "or"):
            self._advance()
            nxt = self._parse_and()
            if nxt is None:
                return None
            children.append(nxt)
        if len(children) == 1:
            return children[0]
        return _OrNode(tuple(children))

    def _parse_and(self) -> _MarkerNode | None:
        first = self._parse_atom()
        if first is None:
            return None
        children: list[_MarkerNode] = [first]
        while self._peek() == ("BOOL", "and"):
            self._advance()
            nxt = self._parse_atom()
            if nxt is None:
                return None
            children.append(nxt)
        if len(children) == 1:
            return children[0]
        return _AndNode(tuple(children))

    def _parse_atom(self) -> _MarkerNode | None:
        if self._peek()[0] == "LPAREN":
            self._advance()
            inner = self._parse_or()
            if inner is None:
                return None
            if self._expect("RPAREN") is None:
                return None
            return inner
        left = self._parse_var()
        if left is None:
            return None
        if self._peek()[0] != "OP":
            return None
        op = self._advance()[1]
        right = self._parse_var()
        if right is None:
            return None
        return _CompareNode(
            left_kind=left[0], left=left[1], op=op, right_kind=right[0], right=right[1]
        )

    def _parse_var(self) -> tuple[str, str] | None:
        tok = self._peek()
        if tok[0] == "NAME":
            self._advance()
            return ("name", tok[1])
        if tok[0] == "STRING":
            self._advance()
            return ("string", tok[1])
        return None


def _parse_marker(text: str) -> _MarkerNode | None:
    tokens = _tokenize_marker(text)
    if tokens is None:
        return None
    return _MarkerParser(tokens).parse()


# ---------------------------------------------------------------------------
# PEP 508 — marker evaluation
# ---------------------------------------------------------------------------

_MARKER_VARIABLES = frozenset(
    {
        "python_version",
        "python_full_version",
        "os_name",
        "sys_platform",
        "platform_release",
        "platform_system",
        "platform_version",
        "platform_machine",
        "platform_python_implementation",
        "implementation_name",
        "implementation_version",
        "extra",
    }
)

_VERSION_VARIABLES = frozenset({"python_version", "python_full_version", "implementation_version"})


def _env_lookup(name: str, env: PythonEnvironmentPayload) -> str:
    idx = {
        "python_version": 0,
        "python_full_version": 1,
        "implementation_name": 2,
        "implementation_version": 3,
        "os_name": 4,
        "sys_platform": 5,
        "platform_system": 6,
        "platform_release": 7,
        "platform_machine": 8,
        "platform_python_implementation": 9,
        "platform_version": 10,
    }
    if name in idx:
        return env[idx[name]]
    if name == "extra":
        return ""
    return ""


def _evaluate_marker(
    node: _MarkerNode, env: PythonEnvironmentPayload
) -> tuple[bool, list[tuple[str, str]]]:
    diagnostics: list[tuple[str, str]] = []
    value = _eval_node(node, env, diagnostics)
    return value, diagnostics


def _eval_node(
    node: _MarkerNode,
    env: PythonEnvironmentPayload,
    diagnostics: list[tuple[str, str]],
) -> bool:
    if isinstance(node, _OrNode):
        return any(_eval_node(c, env, diagnostics) for c in node.children)
    if isinstance(node, _AndNode):
        return all(_eval_node(c, env, diagnostics) for c in node.children)
    return _eval_compare(node, env, diagnostics)


def _eval_compare(
    node: _CompareNode,
    env: PythonEnvironmentPayload,
    diagnostics: list[tuple[str, str]],
) -> bool:
    def resolve(kind: str, text: str) -> tuple[str, bool]:
        if kind == "name":
            if text not in _MARKER_VARIABLES:
                diagnostics.append(("unknown-marker-variable", f"unknown marker variable: {text}"))
                return "", False
            if text == "extra":
                diagnostics.append(
                    (
                        "extras-not-modeled",
                        "marker references 'extra'; extras are not modeled — "
                        "treating as empty string",
                    )
                )
            if text == "platform_version":
                diagnostics.append(
                    (
                        "platform-version-unstable",
                        "marker references 'platform_version', which is noisy "
                        "across kernel patch versions",
                    )
                )
            return _env_lookup(text, env), text in _VERSION_VARIABLES
        return text, False

    left_val, left_is_ver = resolve(node.left_kind, node.left)
    right_val, right_is_ver = resolve(node.right_kind, node.right)
    op = node.op

    use_version_compare = op in ("<", "<=", ">", ">=", "==", "!=") and (
        (node.left_kind == "name" and left_is_ver) or (node.right_kind == "name" and right_is_ver)
    )

    if op == "in":
        return left_val in right_val
    if op == "not in":
        return left_val not in right_val
    if op == "===":
        return left_val == right_val
    if op == "~=":
        left_v = parse_version(left_val)
        right_v = parse_version(right_val)
        if left_v is None or right_v is None:
            diagnostics.append(
                (
                    "unparseable-version",
                    f"cannot parse version in marker: {left_val} ~= {right_val}",
                )
            )
            return False
        spec_set = (("~=", right_val),)
        ok, _ = satisfies(spec_set, left_val, include_prerelease=True)
        return ok

    if use_version_compare:
        left_v = parse_version(left_val)
        right_v = parse_version(right_val)
        if left_v is None or right_v is None:
            diagnostics.append(
                (
                    "unparseable-version",
                    f"cannot parse version in marker: {left_val} {op} {right_val}",
                )
            )
            return False
        cmp = compare_versions(left_v, right_v)
        if op == "<":
            return cmp < 0
        if op == "<=":
            return cmp <= 0
        if op == ">":
            return cmp > 0
        if op == ">=":
            return cmp >= 0
        if op == "==":
            return cmp == 0
        if op == "!=":
            return cmp != 0
        return False

    if op == "<":
        return left_val < right_val
    if op == "<=":
        return left_val <= right_val
    if op == ">":
        return left_val > right_val
    if op == ">=":
        return left_val >= right_val
    if op == "==":
        return left_val == right_val
    if op == "!=":
        return left_val != right_val
    return False


# ---------------------------------------------------------------------------
# Layer 1 — Payload queries
# ---------------------------------------------------------------------------


@query
def python_environment_snapshot(db: Database) -> PythonEnvironmentPayload:
    """Return a snapshot of the current Python environment for marker evaluation."""
    return _PY_ENV.read(db)


# ---------------------------------------------------------------------------
# Layer 2 — Composition queries (NOT re-exported from pyinc.integrations)
# ---------------------------------------------------------------------------


@query
def _evaluate_markers_payload(db: Database, marker: str) -> MarkerEvaluationPayload:
    env = python_environment_snapshot(db)
    stripped = marker.strip()
    if not stripped:
        return (marker, True, ())
    node = _parse_marker(stripped)
    if node is None:
        return (
            marker,
            False,
            (("marker-parse-error", f"cannot parse marker: {marker}"),),
        )
    value, diagnostics = _evaluate_marker(node, env)
    return (marker, value, tuple(diagnostics))


@query
def _evaluate_version_specifier_payload(
    db: Database, specifier: str, version: str
) -> VersionSpecifierEvalPayload:
    spec_set = parse_specifier_set(specifier)
    if spec_set is None:
        return (specifier, version, False, f"cannot parse specifier: {specifier}")
    ok, detail = satisfies(spec_set, version, include_prerelease=False)
    return (specifier, version, ok, detail)


def _evaluate_requirement(
    req: RequirementPayload,
    installed_map: dict[str, str],
    env: PythonEnvironmentPayload,
) -> tuple[ApplicableRequirementPayload, tuple[tuple[str, str], ...]]:
    name, _raw_line, _lineno, _extras, version_spec, markers, _is_editable = req
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    diagnostics: list[tuple[str, str]] = []

    marker_applicable = True
    if markers:
        node = _parse_marker(markers)
        if node is None:
            diagnostics.append(("marker-parse-error", f"cannot parse marker for {name}: {markers}"))
            marker_applicable = False
        else:
            marker_applicable, marker_diag = _evaluate_marker(node, env)
            diagnostics.extend(marker_diag)

    if not marker_applicable:
        return (
            (
                normalized,
                version_spec,
                markers,
                False,
                "",
                "not_applicable",
                "marker is false",
            ),
            tuple(diagnostics),
        )

    if version_spec.startswith("@"):
        diagnostics.append(
            (
                "url-requirement-deferred",
                f"URL-based requirement {name} is not evaluated",
            )
        )
        installed = installed_map.get(normalized, "")
        if installed:
            return (
                (
                    normalized,
                    version_spec,
                    markers,
                    True,
                    installed,
                    "satisfied",
                    "installed, URL requirement",
                ),
                tuple(diagnostics),
            )
        return (
            (normalized, version_spec, markers, True, "", "missing", "not installed"),
            tuple(diagnostics),
        )

    installed = installed_map.get(normalized, "")
    if not installed:
        return (
            (normalized, version_spec, markers, True, "", "missing", "not installed"),
            tuple(diagnostics),
        )

    if not version_spec:
        return (
            (
                normalized,
                version_spec,
                markers,
                True,
                installed,
                "satisfied",
                "installed, no constraint",
            ),
            tuple(diagnostics),
        )

    spec_set = parse_specifier_set(version_spec)
    if spec_set is None:
        return (
            (
                normalized,
                version_spec,
                markers,
                True,
                installed,
                "ambiguous",
                f"cannot parse specifier: {version_spec}",
            ),
            tuple(diagnostics),
        )

    ok, detail = satisfies(spec_set, installed, include_prerelease=False)
    status: ApplicableStatus = "satisfied" if ok else "version_mismatch"
    return (
        (normalized, version_spec, markers, True, installed, status, detail),
        tuple(diagnostics),
    )


@query
def applicable_requirements_payload(
    db: Database, path: str
) -> ApplicableRequirementsAnalysisPayload:
    reqs = requirements_payload(db, path)
    dist_index = installed_distributions_index(db)
    env = python_environment_snapshot(db)

    installed_map: dict[str, str] = dict(dist_index)

    entries: list[ApplicableRequirementPayload] = []
    all_diagnostics: list[tuple[str, str]] = []
    for req in reqs:
        entry, diagnostics = _evaluate_requirement(req, installed_map, env)
        entries.append(entry)
        all_diagnostics.extend(diagnostics)

    return (path, tuple(entries), env, tuple(all_diagnostics))


# ---------------------------------------------------------------------------
# Layer 3 — Entrypoints
# ---------------------------------------------------------------------------


def _decode_env(payload: PythonEnvironmentPayload) -> PythonEnvironmentSnapshot:
    return PythonEnvironmentSnapshot(
        python_version=payload[0],
        python_full_version=payload[1],
        implementation_name=payload[2],
        implementation_version=payload[3],
        os_name=payload[4],
        sys_platform=payload[5],
        platform_system=payload[6],
        platform_release=payload[7],
        platform_machine=payload[8],
        platform_python_implementation=payload[9],
        platform_version=payload[10],
    )


def _decode_applicable(payload: ApplicableRequirementPayload) -> ApplicableRequirement:
    name, version_spec, markers, applicable, installed_version, status, detail = payload
    return ApplicableRequirement(
        name=name,
        version_spec=version_spec,
        markers=markers,
        applicable=applicable,
        installed_version=installed_version,
        status=status,
        detail=detail,
    )


def evaluate_markers(db: Database, marker: str) -> MarkerEvaluation:
    """Evaluate a PEP 508 marker expression against the current Python environment."""
    payload = cast(
        MarkerEvaluationPayload,
        thaw(db.get(_evaluate_markers_payload, marker)),
    )
    text, value, diagnostics = payload
    return MarkerEvaluation(marker=text, value=value, diagnostics=diagnostics)


def evaluate_version_specifier(
    db: Database, specifier: str, version: str
) -> VersionSpecifierEvaluation:
    """Evaluate a PEP 440 specifier set against a concrete version string."""
    payload = cast(
        VersionSpecifierEvalPayload,
        thaw(db.get(_evaluate_version_specifier_payload, specifier, version)),
    )
    spec, ver, satisfied, detail = payload
    return VersionSpecifierEvaluation(
        specifier=spec, version=ver, satisfied=satisfied, detail=detail
    )


def applicable_requirements(
    db: Database, path: str | os.PathLike[str]
) -> ApplicableRequirementsAnalysis:
    """Return the effective applicable/satisfied requirement set for the current env."""
    normalized = os.fspath(path)
    payload = cast(
        ApplicableRequirementsAnalysisPayload,
        thaw(db.get(applicable_requirements_payload, normalized)),
    )
    path_str, entries, env_payload, diagnostics = payload
    return ApplicableRequirementsAnalysis(
        path=path_str,
        requirements=tuple(_decode_applicable(e) for e in entries),
        environment=_decode_env(env_payload),
        diagnostics=diagnostics,
    )


def workspace_applicable_requirements(
    db: Database, root: str | os.PathLike[str]
) -> ApplicableRequirementsAnalysis | None:
    normalized_root = os.fspath(root)
    entries = _DIRECTORIES.read(db, normalized_root)
    for name in entries:
        if name == "requirements.txt":
            return applicable_requirements(db, str(Path(normalized_root) / name))
    return None


__all__ = [
    "ApplicableRequirement",
    "ApplicableRequirementsAnalysis",
    "MarkerEvaluation",
    "PythonEnvironmentSnapshot",
    "VersionSpecifierEvaluation",
    "applicable_requirements",
    "evaluate_markers",
    "evaluate_version_specifier",
    "python_environment_snapshot",
    "workspace_applicable_requirements",
]
