"""Security detection-content compiler.

Compiles a small, deliberately-scoped normalized detection-rule format (stdlib
JSON) into per-backend query artifacts, a normalized bundle, per-rule coverage
fragments, an aggregate coverage matrix, rule docs, and deterministic rule-test
results — reconciled to disk through the action layer (`pyinc.actions`).

This is **not** Sigma or YARA and claims no compliance with them; it is a compact
inspired format. No network, subprocess, clock, or environment dependencies; no
external SIEM or YARA engine is executed. See
`docs/detection-content-format.md` for the input format and supported grammar.

Input layout under one rules root::

    rules/<rule_id>.json      one rule per file
    mappings.json             logical field -> {backend: physical field}
    macros.json               reusable named expressions
    tests/<rule_id>.json      optional rule-test fixtures

Detection-expression grammar (the complete supported set)::

    leaf:        {"field": <logical>, "op": <operator>, "value": <scalar|[scalars]>}
    operators:   equals not_equals in contains startswith endswith exists
                 gt gte lt lte regex
    combinators: {"all": [expr, ...]} {"any": [expr, ...]} {"not": expr}
    macro:       {"macro": <name>}

Unknown operators / macros / fields produce explicit deterministic diagnostics;
the offending rule is reported but never rendered with approximated output.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from pyinc.actions import (
    ActionIdentity,
    ActionResult,
    DesiredArtifact,
    DesiredArtifactSet,
    FilesystemReconciler,
    ToolIdentity,
    default_state_dir,
)
from pyinc.core import query
from pyinc.explain import InspectionNode
from pyinc.resources import DirectoryResource, _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

_TOOL = ToolIdentity(name="pyinc.detection_rules", version="1.0.0", schema_version=1)
_ACTION_ID = "pyinc.detection_rules"

_BACKENDS: tuple[str, ...] = ("elastic", "sentinel", "splunk")
_SUPPORTED_OPS = frozenset(
    {
        "equals",
        "not_equals",
        "in",
        "contains",
        "startswith",
        "endswith",
        "exists",
        "gt",
        "gte",
        "lt",
        "lte",
        "regex",
    }
)
_MACRO_DEPTH_LIMIT = 32


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DetectionDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class DetectionRule:
    rule_id: str
    title: str
    severity: str
    techniques: tuple[str, ...]
    references: tuple[str, ...]
    status: str  # "ok" | "error"
    diagnostics: tuple[DetectionDiagnostic, ...]


@dataclass(frozen=True)
class DetectionAnalysis:
    rules: tuple[DetectionRule, ...]
    diagnostics: tuple[DetectionDiagnostic, ...]


@dataclass(frozen=True)
class DetectionProvenance:
    output_path: str
    rule_id: str
    rule_source: str
    backend: str
    mappings: tuple[str, ...]
    macros: tuple[str, ...]
    transforms: tuple[str, ...]


# ---------------------------------------------------------------------------
# Payload type aliases
# ---------------------------------------------------------------------------

Expr: TypeAlias = tuple[Any, ...]
DiagnosticPayload: TypeAlias = tuple[str, str]
MetaPayload: TypeAlias = tuple[
    str,  # rule_id
    str,  # title
    str,  # description
    str,  # severity
    tuple[str, ...],  # techniques
    tuple[str, ...],  # references
]
ResolvedRulePayload: TypeAlias = tuple[
    MetaPayload,
    Expr,  # IR (logical fields)
    tuple[str, ...],  # referenced fields
    tuple[str, ...],  # referenced macros
    tuple[DiagnosticPayload, ...],
]
ArtifactPayload: TypeAlias = tuple[str, bytes]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _TextFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"detection-file[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        probe, _text = _file_read_snapshot(path, self.encoding)
        return probe

    def load(self, db: Database, path: str) -> str:
        _probe, text = _file_read_snapshot(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = _file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _TextFileResource()
_DIRS = DirectoryResource()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _json_cutoff_token(text: str) -> tuple[str, str]:
    try:
        return ("json", repr(freeze(json.loads(text))))
    except json.JSONDecodeError:
        return ("raw", text)


def _str_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if value in (None, ""):
        return ()
    return (str(value),)


def _scalar(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_scalar(v) for v in value)
    if isinstance(value, bool | int | float | str):
        return value
    return str(value)


# ---------------------------------------------------------------------------
# Layer 1 — source payload queries
# ---------------------------------------------------------------------------


@query
def rule_ids(db: Database, root: str) -> tuple[str, ...]:
    names = _DIRS.read(db, os.path.join(root, "rules"))
    return tuple(sorted(n[:-5] for n in names if n.endswith(".json")))


@query(cutoff=_json_cutoff_token)
def rule_text(db: Database, root: str, rule_id: str) -> str:
    return _FILES.read(db, os.path.join(root, "rules", f"{rule_id}.json"))


@query(cutoff=_json_cutoff_token)
def mappings_text(db: Database, root: str) -> str:
    return _FILES.read(db, os.path.join(root, "mappings.json"))


@query(cutoff=_json_cutoff_token)
def macros_text(db: Database, root: str) -> str:
    return _FILES.read(db, os.path.join(root, "macros.json"))


@query(cutoff=_json_cutoff_token)
def rule_test_text(db: Database, root: str, rule_id: str) -> str:
    return _FILES.read(db, os.path.join(root, "tests", f"{rule_id}.json"))


@query
def field_mapping(db: Database, root: str, field: str) -> tuple[tuple[str, str], ...]:
    """Per-field backend mapping; backdates when this field's mapping is unchanged,
    so editing an unrelated field's mapping performs zero downstream writes."""
    try:
        mappings = json.loads(mappings_text(db, root) or "{}")
    except json.JSONDecodeError:
        return ()
    entry = mappings.get(field) if isinstance(mappings, dict) else None
    if not isinstance(entry, dict):
        return ()
    return tuple(sorted((str(b), str(p)) for b, p in entry.items()))


@query
def macro_expr(db: Database, root: str, name: str) -> str:
    """Canonical JSON of one macro body ("" if unknown). Backdates per-macro."""
    try:
        macros = json.loads(macros_text(db, root) or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(macros, dict) or name not in macros:
        return ""
    return json.dumps(macros[name], sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Layer 2 — resolution
# ---------------------------------------------------------------------------


def _resolve_expr(
    db: Database,
    root: str,
    node: Any,
    fields_out: set[str],
    macros_out: set[str],
    diagnostics: list[DiagnosticPayload],
    depth: int,
) -> Expr:
    if depth > _MACRO_DEPTH_LIMIT:
        diagnostics.append(("macro-recursion", "Macro expansion exceeded depth limit."))
        return ("all", ())
    if not isinstance(node, dict):
        diagnostics.append(("invalid-expression", f"Expression node is not an object: {node!r}."))
        return ("all", ())

    for combinator in ("all", "any"):
        if combinator in node:
            children = node[combinator]
            if not isinstance(children, list):
                diagnostics.append((f"invalid-{combinator}", f"{combinator!r} must be a list."))
                return ("all", ())
            return (
                combinator,
                tuple(
                    _resolve_expr(db, root, c, fields_out, macros_out, diagnostics, depth + 1)
                    for c in children
                ),
            )
    if "not" in node:
        return ("not", _resolve_expr(db, root, node["not"], fields_out, macros_out, diagnostics, depth + 1))
    if "macro" in node:
        name = str(node["macro"])
        macros_out.add(name)
        body = macro_expr(db, root, name)
        if not body:
            diagnostics.append(("unknown-macro", f"Macro {name!r} is not defined."))
            return ("all", ())
        return _resolve_expr(db, root, json.loads(body), fields_out, macros_out, diagnostics, depth + 1)
    if "field" in node:
        field = str(node["field"])
        op = str(node.get("op", ""))
        fields_out.add(field)
        if op not in _SUPPORTED_OPS:
            diagnostics.append(("unsupported-operator", f"Operator {op!r} on field {field!r} is not supported."))
            return ("all", ())
        return ("pred", field, op, _scalar(node.get("value")))

    diagnostics.append(("invalid-expression", f"Unrecognized expression node: {sorted(node)!r}."))
    return ("all", ())


@query
def resolved_rule(db: Database, root: str, rule_id: str) -> ResolvedRulePayload:
    text = rule_text(db, root, rule_id)
    diagnostics: list[DiagnosticPayload] = []
    empty_meta: MetaPayload = (rule_id, "", "", "", (), ())
    try:
        raw = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        return (empty_meta, ("all", ()), (), (), (("json-decode-error", str(exc)),))
    if not isinstance(raw, dict):
        return (empty_meta, ("all", ()), (), (), (("invalid-rule", "Rule is not a JSON object."),))

    meta: MetaPayload = (
        str(raw.get("id", rule_id)),
        str(raw.get("title", "")),
        str(raw.get("description", "")),
        str(raw.get("severity", "medium")),
        _str_tuple(raw.get("attack")),
        _str_tuple(raw.get("references")),
    )
    fields_out: set[str] = set()
    macros_out: set[str] = set()
    detection = raw.get("detection")
    if detection is None:
        diagnostics.append(("missing-detection", "Rule has no 'detection' expression."))
        ir: Expr = ("all", ())
    else:
        ir = _resolve_expr(db, root, detection, fields_out, macros_out, diagnostics, 0)
    return (
        meta,
        ir,
        tuple(sorted(fields_out)),
        tuple(sorted(macros_out)),
        tuple(diagnostics),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _physical(field: str, backend: str, fmap: dict[str, dict[str, str]]) -> str:
    return fmap.get(field, {}).get(backend, field)


def _render_pred(field: str, op: str, value: Any, backend: str, fmap: dict[str, dict[str, str]]) -> str:
    phys = _physical(field, backend, fmap)
    if op == "in":
        values = value if isinstance(value, tuple) else (value,)
        return "(" + " OR ".join(_render_pred(field, "equals", v, backend, fmap) for v in values) + ")"
    text = _escape(str(value))
    numeric = isinstance(value, bool | int | float)
    if backend == "splunk":
        table = {
            "equals": f'{phys}="{text}"',
            "not_equals": f'{phys}!="{text}"',
            "contains": f'{phys}="*{text}*"',
            "startswith": f'{phys}="{text}*"',
            "endswith": f'{phys}="*{text}"',
            "exists": f"{phys}=*",
            "regex": f'match({phys}, "{text}")',
            "gt": f"{phys}>{text}",
            "gte": f"{phys}>={text}",
            "lt": f"{phys}<{text}",
            "lte": f"{phys}<={text}",
        }
        return table[op]
    if backend == "elastic":
        table = {
            "equals": f'{phys}:"{text}"',
            "not_equals": f'NOT {phys}:"{text}"',
            "contains": f"{phys}:*{text}*",
            "startswith": f"{phys}:{text}*",
            "endswith": f"{phys}:*{text}",
            "exists": f"{phys}:*",
            "regex": f"{phys}:/{text}/",
            "gt": f"{phys}>{text}",
            "gte": f"{phys}>={text}",
            "lt": f"{phys}<{text}",
            "lte": f"{phys}<={text}",
        }
        return table[op]
    # sentinel (KQL)
    quoted = text if numeric else f'"{text}"'
    table = {
        "equals": f"{phys} == {quoted}",
        "not_equals": f"{phys} != {quoted}",
        "contains": f'{phys} contains "{text}"',
        "startswith": f'{phys} startswith "{text}"',
        "endswith": f'{phys} endswith "{text}"',
        "exists": f"isnotempty({phys})",
        "regex": f'{phys} matches regex "{text}"',
        "gt": f"{phys} > {quoted}",
        "gte": f"{phys} >= {quoted}",
        "lt": f"{phys} < {quoted}",
        "lte": f"{phys} <= {quoted}",
    }
    return table[op]


def _render_expr(node: Expr, backend: str, fmap: dict[str, dict[str, str]]) -> str:
    tag = node[0]
    if tag == "pred":
        return _render_pred(node[1], node[2], node[3], backend, fmap)
    if tag in ("all", "any"):
        joiner = " AND " if tag == "all" else " OR "
        parts = [_render_expr(child, backend, fmap) for child in node[1]]
        if not parts:
            return "true"
        return "(" + joiner.join(parts) + ")"
    if tag == "not":
        return f"NOT ({_render_expr(node[1], backend, fmap)})"
    return "true"


def _backend_extension(backend: str) -> str:
    return {"splunk": "spl", "elastic": "kql", "sentinel": "kql"}.get(backend, "txt")


@query
def rendered_rule(db: Database, root: str, rule_id: str, backend: str) -> str:
    payload = resolved_rule(db, root, rule_id)
    _meta, ir, ref_fields, _macros, diagnostics = payload
    if diagnostics:
        return ""  # invalid rules render nothing; surfaced via diagnostics/bundle
    fmap: dict[str, dict[str, str]] = {
        field: dict(field_mapping(db, root, field)) for field in ref_fields
    }
    return _render_expr(ir, backend, fmap)


# ---------------------------------------------------------------------------
# Tests / coverage / docs / aggregates
# ---------------------------------------------------------------------------


def _evaluate(node: Expr, event: dict[str, Any]) -> bool:
    tag = node[0]
    if tag == "all":
        return all(_evaluate(c, event) for c in node[1])
    if tag == "any":
        return any(_evaluate(c, event) for c in node[1])
    if tag == "not":
        return not _evaluate(node[1], event)
    field, op, value = node[1], node[2], node[3]
    present = field in event
    actual = event.get(field)
    if op == "exists":
        return present
    if op == "equals":
        return bool(actual == value)
    if op == "not_equals":
        return bool(actual != value)
    if op == "in":
        return actual in (value if isinstance(value, tuple) else (value,))
    if op in ("contains", "startswith", "endswith"):
        text = "" if actual is None else str(actual)
        needle = str(value)
        return {"contains": needle in text, "startswith": text.startswith(needle), "endswith": text.endswith(needle)}[op]
    if op == "regex":
        return present and re.search(str(value), str(actual)) is not None
    if op in ("gt", "gte", "lt", "lte") and isinstance(actual, int | float) and isinstance(value, int | float):
        return {"gt": actual > value, "gte": actual >= value, "lt": actual < value, "lte": actual <= value}[op]
    return False


@query
def rule_test_results(db: Database, root: str, rule_id: str) -> tuple[tuple[int, bool, bool, bool], ...]:
    payload = resolved_rule(db, root, rule_id)
    _meta, ir, _fields, _macros, diagnostics = payload
    text = rule_test_text(db, root, rule_id)
    if diagnostics or not text:
        return ()
    try:
        fixture = json.loads(text)
    except json.JSONDecodeError:
        return ()
    events = fixture.get("events", []) if isinstance(fixture, dict) else []
    expected = fixture.get("expect", []) if isinstance(fixture, dict) else []
    results: list[tuple[int, bool, bool, bool]] = []
    for i, event in enumerate(events):
        matched = _evaluate(ir, event) if isinstance(event, dict) else False
        want = bool(expected[i]) if i < len(expected) else matched
        results.append((i, matched, want, matched == want))
    return tuple(results)


def _render_bytes_json(obj: Any) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode("utf-8")


@query
def artifacts_payload(db: Database, root: str) -> tuple[ArtifactPayload, ...]:
    ids = rule_ids(db, root)
    artifacts: list[ArtifactPayload] = []
    bundle_rules: list[dict[str, Any]] = []
    technique_index: dict[str, list[str]] = {}

    for rule_id in ids:
        payload = resolved_rule(db, root, rule_id)
        meta, ir, _fields, _macros, diagnostics = payload
        status = "error" if diagnostics else "ok"

        if not diagnostics:
            for backend in _BACKENDS:
                rendered = rendered_rule(db, root, rule_id, backend)
                ext = _backend_extension(backend)
                artifacts.append(
                    (f"queries/{backend}/{rule_id}.{ext}", (rendered + "\n").encode("utf-8"))
                )
            results = rule_test_results(db, root, rule_id)
            if results:
                test_obj = {
                    "rule_id": rule_id,
                    "results": [
                        {"event": i, "matched": m, "expected": e, "pass": p}
                        for i, m, e, p in results
                    ],
                }
                artifacts.append((f"tests/{rule_id}.json", _render_bytes_json(test_obj)))

        coverage = {
            "rule_id": meta[0],
            "severity": meta[3],
            "status": status,
            "techniques": list(meta[4]),
            "backends": list(_BACKENDS) if not diagnostics else [],
        }
        artifacts.append((f"coverage/{rule_id}.json", _render_bytes_json(coverage)))
        artifacts.append((f"docs/{rule_id}.md", _render_doc(meta, diagnostics)))

        bundle_rules.append(
            {
                "id": meta[0],
                "title": meta[1],
                "severity": meta[3],
                "techniques": list(meta[4]),
                "status": status,
                "ir": _ir_to_json(ir),
                "diagnostics": [{"code": c, "message": m} for c, m in diagnostics],
            }
        )
        for technique in meta[4]:
            technique_index.setdefault(technique, []).append(meta[0])

    artifacts.append(("bundle.json", _render_bytes_json({"rules": bundle_rules})))
    matrix = {
        "techniques": [
            {"technique": t, "rules": sorted(technique_index[t])} for t in sorted(technique_index)
        ]
    }
    artifacts.append(("coverage_matrix.json", _render_bytes_json(matrix)))

    artifacts.sort(key=lambda item: item[0])
    return tuple(artifacts)


def _ir_to_json(node: Expr) -> Any:
    tag = node[0]
    if tag == "pred":
        value = list(node[3]) if isinstance(node[3], tuple) else node[3]
        return {"field": node[1], "op": node[2], "value": value}
    if tag in ("all", "any"):
        return {tag: [_ir_to_json(c) for c in node[1]]}
    if tag == "not":
        return {"not": _ir_to_json(node[1])}
    return {"all": []}


def _render_doc(meta: MetaPayload, diagnostics: tuple[DiagnosticPayload, ...]) -> bytes:
    rule_id, title, description, severity, techniques, references = meta
    lines = [f"# {title or rule_id}", "", f"- **ID:** `{rule_id}`", f"- **Severity:** {severity}"]
    if techniques:
        lines.append(f"- **ATT&CK:** {', '.join(techniques)}")
    lines.append("")
    if description:
        lines += [description, ""]
    if references:
        lines += ["## References", ""]
        lines += [f"- {ref}" for ref in references]
        lines.append("")
    if diagnostics:
        lines += ["## Diagnostics", ""]
        lines += [f"- `{code}`: {message}" for code, message in diagnostics]
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Layer 3 — entrypoints + provenance
# ---------------------------------------------------------------------------


def detection_analysis(db: Database, root: str | os.PathLike[str]) -> DetectionAnalysis:
    normalized = os.fspath(root)
    rules: list[DetectionRule] = []
    all_diagnostics: list[DetectionDiagnostic] = []
    for rule_id in db.get(rule_ids, normalized):
        payload = cast(ResolvedRulePayload, thaw(db.get(resolved_rule, normalized, rule_id)))
        meta, _ir, _fields, _macros, diagnostics = payload
        diags = tuple(DetectionDiagnostic(code=c, message=m) for c, m in diagnostics)
        rules.append(
            DetectionRule(
                rule_id=meta[0],
                title=meta[1],
                severity=meta[3],
                techniques=meta[4],
                references=meta[5],
                status="error" if diags else "ok",
                diagnostics=diags,
            )
        )
        all_diagnostics.extend(diags)
    return DetectionAnalysis(rules=tuple(rules), diagnostics=tuple(all_diagnostics))


def detection_artifacts(
    db: Database, root: str | os.PathLike[str], output_root: str | os.PathLike[str]
) -> DesiredArtifactSet:
    normalized = os.fspath(root)
    payload = cast("tuple[ArtifactPayload, ...]", thaw(db.get(artifacts_payload, normalized)))
    artifacts = tuple(DesiredArtifact(rel, content) for rel, content in payload)
    identity = ActionIdentity(
        action_id=_ACTION_ID, output_root=os.fspath(output_root), tool=_TOOL
    )
    return DesiredArtifactSet(identity, artifacts)


def generate_detections(
    db: Database,
    root: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> ActionResult:
    desired = detection_artifacts(db, root, output_root)
    resolved_state = (
        default_state_dir(output_root, _ACTION_ID) if state_dir is None else state_dir
    )
    return FilesystemReconciler(output_root, state_dir=resolved_state).apply(desired)


def _collect_labels(node: InspectionNode, out: list[str]) -> None:
    out.append(node.label)
    for dep in node.dependencies:
        _collect_labels(dep, out)


def rule_provenance(
    db: Database,
    root: str | os.PathLike[str],
    rule_id: str,
    backend: str,
) -> DetectionProvenance:
    """Provenance for one generated query: its source rule, referenced
    mappings/macros, backend, and the transformation query nodes that produced it
    (derived from `Database.inspect`)."""
    normalized = os.fspath(root)
    payload = cast(ResolvedRulePayload, thaw(db.get(resolved_rule, normalized, rule_id)))
    meta, _ir, ref_fields, ref_macros, _diagnostics = payload
    db.get(rendered_rule, normalized, rule_id, backend)  # ensure the node is recorded
    tree = db.inspect(rendered_rule, normalized, rule_id, backend)
    labels: list[str] = []
    _collect_labels(tree, labels)
    transforms = tuple(
        sorted(
            label.split(":", 1)[-1].split("(", 1)[0]
            for label in labels
            if "detection_rules:" in label
        )
    )
    return DetectionProvenance(
        output_path=f"queries/{backend}/{rule_id}.{_backend_extension(backend)}",
        rule_id=meta[0],
        rule_source=os.path.join(normalized, "rules", f"{rule_id}.json"),
        backend=backend,
        mappings=ref_fields,
        macros=ref_macros,
        transforms=transforms,
    )


__all__ = [
    "DetectionAnalysis",
    "DetectionDiagnostic",
    "DetectionProvenance",
    "DetectionRule",
    "detection_analysis",
    "detection_artifacts",
    "generate_detections",
    "rule_provenance",
]
