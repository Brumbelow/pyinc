"""GraphQL introspection-driven incremental code/doc generator.

Reads a local GraphQL introspection JSON document (the standard ``__schema``
shape) and produces, granularly per type:

- typed Python model/client stubs (`models/<Type>.py`),
- operation/probe stubs for root query/mutation fields (`operations/<field>.py`),
- per-type schema documentation (`docs/types/<Type>.md`),
- aggregate index files (`models/__init__.py`, `docs/index.md`).

It follows the three-layer integration shape (payload queries → composition →
entrypoints) and never writes files itself: a payload query computes the desired
output bytes and the entrypoints hand a `DesiredArtifactSet` to the action layer,
which reconciles outside query evaluation.

Code artifacts depend on a *code-shape* model (kind, field/arg names, nullability,
return signatures, enum values, input fields) and are invariant to ``description``;
documentation artifacts depend on a *doc-shape* model that includes descriptions.
The two are separate cutoff-bearing queries, so a description-only edit backdates
the code model (no code rewrites) while only the affected docs regenerate. No
network access; stdlib ``json`` only.
"""

from __future__ import annotations

import json
import os
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
from pyinc.resources import _file_read_snapshot
from pyinc.runtime import Database
from pyinc.value import freeze, thaw

_TOOL = ToolIdentity(name="pyinc.graphql_schema", version="1.0.0", schema_version=1)
_ACTION_ID = "pyinc.graphql_schema"

# Built-in GraphQL scalars map to Python builtins; kept as a frozenset so it is a
# safe immutable ambient capture inside queries.
_BUILTIN_SCALARS = frozenset({"ID", "String", "Int", "Float", "Boolean"})


def _scalar_python(name: str) -> str:
    """Map a GraphQL scalar name to a Python annotation. Uses a local literal (not
    a module-level dict) so it is never an unsupported mutable ambient capture."""
    mapping = {"ID": "str", "String": "str", "Int": "int", "Float": "float", "Boolean": "bool"}
    return mapping.get(name, name)


# ---------------------------------------------------------------------------
# Public result model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphQLArgument:
    name: str
    signature: str
    description: str


@dataclass(frozen=True)
class GraphQLField:
    name: str
    signature: str
    description: str
    arguments: tuple[GraphQLArgument, ...]


@dataclass(frozen=True)
class GraphQLEnumValue:
    name: str
    description: str


@dataclass(frozen=True)
class GraphQLType:
    name: str
    kind: str
    description: str
    interfaces: tuple[str, ...]
    fields: tuple[GraphQLField, ...]
    enum_values: tuple[GraphQLEnumValue, ...]
    possible_types: tuple[str, ...]


@dataclass(frozen=True)
class GraphQLDiagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class GraphQLSchema:
    query_type: str | None
    mutation_type: str | None
    types: tuple[GraphQLType, ...]
    diagnostics: tuple[GraphQLDiagnostic, ...]


# ---------------------------------------------------------------------------
# Payload type aliases (snapshot-safe tuples crossing the kernel boundary)
# ---------------------------------------------------------------------------

ArgPayload: TypeAlias = tuple[str, str, str]  # (name, signature, description)
FieldPayload: TypeAlias = tuple[str, str, str, tuple[ArgPayload, ...]]
TypePayload: TypeAlias = tuple[
    str,  # name
    str,  # kind
    str,  # description
    tuple[str, ...],  # interfaces
    tuple[FieldPayload, ...],  # fields (object/interface/input)
    tuple[tuple[str, str], ...],  # enum values (name, description)
    tuple[str, ...],  # possible types (union/interface)
]
DiagnosticPayload: TypeAlias = tuple[str, str]
SchemaPayload: TypeAlias = tuple[
    str | None,  # query type
    str | None,  # mutation type
    tuple[TypePayload, ...],
    tuple[DiagnosticPayload, ...],
]
ArtifactPayload: TypeAlias = tuple[str, bytes]


# ---------------------------------------------------------------------------
# Resource
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SchemaFileResource:
    encoding: str = "utf-8"

    def read(self, db: Database, path: str | os.PathLike[str]) -> str:
        return cast(str, db._read_resource(self, os.fspath(path)))

    def label(self, path: str) -> str:
        return f"graphql-schema[{path}]"

    def probe(self, path: str) -> tuple[str, str] | tuple[str]:
        probe, _text = _file_read_snapshot(path, self.encoding)
        return probe

    def load(self, db: Database, path: str) -> str:
        probe, text = _file_read_snapshot(path, self.encoding)
        return text if text is not None else ""

    def probe_and_load(
        self, db: Database, path: str
    ) -> tuple[tuple[str, str] | tuple[str], str]:
        probe, text = _file_read_snapshot(path, self.encoding)
        return probe, text if text is not None else ""


_FILES = _SchemaFileResource()


# ---------------------------------------------------------------------------
# Parsing / normalization helpers
# ---------------------------------------------------------------------------


def _json_cutoff_token(text: str) -> tuple[str, str]:
    """Whitespace / key-order insensitive cutoff for the raw schema bytes."""
    try:
        return ("json", repr(freeze(json.loads(text))))
    except json.JSONDecodeError:
        return ("raw", text)


def _snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (not name[i - 1].isupper()):
            out.append("_")
        out.append(ch.lower())
    return "".join(out) or "field"


def _ref_signature(ref: dict[str, Any]) -> str:
    kind = ref.get("kind")
    if kind == "NON_NULL":
        return _ref_signature(ref["ofType"]) + "!"
    if kind == "LIST":
        return "[" + _ref_signature(ref["ofType"]) + "]"
    return str(ref.get("name"))


def _decode_arg(arg: dict[str, Any]) -> ArgPayload:
    return (
        str(arg.get("name", "")),
        _ref_signature(arg["type"]),
        str(arg.get("description") or ""),
    )


def _decode_field(field: dict[str, Any]) -> FieldPayload:
    args = tuple(_decode_arg(a) for a in field.get("args") or ())
    return (
        str(field.get("name", "")),
        _ref_signature(field["type"]),
        str(field.get("description") or ""),
        args,
    )


def _decode_input_field(field: dict[str, Any]) -> FieldPayload:
    return (
        str(field.get("name", "")),
        _ref_signature(field["type"]),
        str(field.get("description") or ""),
        (),
    )


_SUPPORTED_KINDS = frozenset(
    {"SCALAR", "OBJECT", "ENUM", "INPUT_OBJECT", "INTERFACE", "UNION"}
)


def _normalize_schema(text: str) -> SchemaPayload:
    diagnostics: list[DiagnosticPayload] = []
    if not text.strip():
        return (None, None, (), (("empty-document", "Schema document is empty."),))
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return (None, None, (), (("json-decode-error", str(exc)),))

    schema = _locate_schema(document)
    if schema is None:
        return (
            None,
            None,
            (),
            (("missing-schema", "No __schema object found in introspection document."),),
        )

    query_type = _root_name(schema.get("queryType"))
    mutation_type = _root_name(schema.get("mutationType"))

    raw_types = schema.get("types")
    if not isinstance(raw_types, list):
        return (
            query_type,
            mutation_type,
            (),
            (("missing-types", "__schema.types is missing or not a list."),),
        )

    types: list[TypePayload] = []
    for raw in raw_types:
        if not isinstance(raw, dict):
            diagnostics.append(("invalid-type", "Encountered a non-object type entry."))
            continue
        name = raw.get("name")
        if not isinstance(name, str) or name.startswith("__"):
            continue  # skip introspection meta types and unnamed entries
        kind = raw.get("kind")
        if kind not in _SUPPORTED_KINDS:
            diagnostics.append(
                ("unsupported-kind", f"Type {name!r} has unsupported kind {kind!r}.")
            )
            continue
        types.append(_normalize_type(name, str(kind), raw))

    types.sort(key=lambda t: t[0])
    diagnostics.sort()
    return (query_type, mutation_type, tuple(types), tuple(diagnostics))


def _normalize_type(name: str, kind: str, raw: dict[str, Any]) -> TypePayload:
    description = str(raw.get("description") or "")
    interfaces = tuple(
        sorted(
            str(i.get("name"))
            for i in raw.get("interfaces") or ()
            if isinstance(i, dict) and i.get("name")
        )
    )
    fields: tuple[FieldPayload, ...] = ()
    enum_values: tuple[tuple[str, str], ...] = ()
    possible_types: tuple[str, ...] = ()

    if kind in ("OBJECT", "INTERFACE"):
        fields = tuple(_decode_field(f) for f in raw.get("fields") or ())
    elif kind == "INPUT_OBJECT":
        fields = tuple(_decode_input_field(f) for f in raw.get("inputFields") or ())
    elif kind == "ENUM":
        enum_values = tuple(
            (str(v.get("name", "")), str(v.get("description") or ""))
            for v in raw.get("enumValues") or ()
        )
    elif kind == "UNION":
        possible_types = tuple(
            sorted(
                str(p.get("name"))
                for p in raw.get("possibleTypes") or ()
                if isinstance(p, dict) and p.get("name")
            )
        )

    return (name, kind, description, interfaces, fields, enum_values, possible_types)


def _locate_schema(document: Any) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    if isinstance(document.get("__schema"), dict):
        return cast("dict[str, Any]", document["__schema"])
    data = document.get("data")
    if isinstance(data, dict) and isinstance(data.get("__schema"), dict):
        return cast("dict[str, Any]", data["__schema"])
    return None


def _root_name(root: Any) -> str | None:
    if isinstance(root, dict) and isinstance(root.get("name"), str):
        return cast(str, root["name"])
    return None


# ---------------------------------------------------------------------------
# Layer 1 / 2 — payload + model queries
# ---------------------------------------------------------------------------


@query(cutoff=_json_cutoff_token)
def schema_text(db: Database, path: str) -> str:
    return _FILES.read(db, path)


@query
def normalized_schema_payload(db: Database, path: str) -> SchemaPayload:
    return _normalize_schema(schema_text(db, path))


def _code_token(payload: SchemaPayload) -> Any:
    """Cutoff token capturing only code-relevant shape (drops descriptions)."""
    query_type, mutation_type, types, _diagnostics = payload
    coded = tuple(
        (
            name,
            kind,
            interfaces,
            tuple((fname, fsig, tuple((a[0], a[1]) for a in fargs)) for fname, fsig, _fd, fargs in fields),
            tuple(ev[0] for ev in enum_values),
            possible_types,
        )
        for name, kind, _desc, interfaces, fields, enum_values, possible_types in types
    )
    return (query_type, mutation_type, coded)


def _doc_token(payload: SchemaPayload) -> Any:
    """Cutoff token capturing doc-relevant shape (includes descriptions)."""
    query_type, mutation_type, types, _diagnostics = payload
    return (query_type, mutation_type, types)


@query(cutoff=_code_token)
def code_model(db: Database, path: str) -> SchemaPayload:
    return normalized_schema_payload(db, path)


@query(cutoff=_doc_token)
def doc_model(db: Database, path: str) -> SchemaPayload:
    return normalized_schema_payload(db, path)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_HEADER = "# Generated by pyinc.graphql_schema. Do not edit by hand.\n"


def _render_model(type_payload: TypePayload) -> bytes:
    name, kind, _desc, interfaces, fields, enum_values, possible_types = type_payload
    lines = [_HEADER, "from __future__ import annotations", ""]
    if kind in ("OBJECT", "INTERFACE", "INPUT_OBJECT"):
        lines += ["from dataclasses import dataclass", "", "", "@dataclass(frozen=True)", f"class {name}:"]
        if interfaces and kind == "OBJECT":
            lines.append(f"    # implements: {', '.join(interfaces)}")
        if not fields:
            lines.append("    pass")
        else:
            for fname, fsig, _fd, _fargs in fields:
                lines.append(f"    {_snake(fname)}: {_signature_to_annotation(fsig)}")
    elif kind == "ENUM":
        lines += ["from enum import Enum", "", "", f"class {name}(Enum):"]
        if not enum_values:
            lines.append("    pass")
        else:
            for value_name, _vd in enum_values:
                lines.append(f"    {value_name} = {value_name!r}")
    elif kind == "UNION":
        lines += ["from typing import Union", ""]
        members = ", ".join(repr(p) for p in possible_types)
        lines.append(f"{name} = Union[{members}]" if members else f"{name} = object")
    else:  # custom SCALAR
        lines.append(f"{name} = str  # custom scalar")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _signature_to_annotation(signature: str) -> str:
    """Convert a GraphQL signature string back into a Python annotation."""
    nullable = True
    sig = signature
    if sig.endswith("!"):
        nullable = False
        sig = sig[:-1]
    if sig.startswith("[") and sig.endswith("]"):
        inner = _signature_to_annotation(sig[1:-1])
        listed = f"tuple[{inner}, ...]"
        return listed if not nullable else f"{listed} | None"
    base = _scalar_python(sig)
    return base if not nullable else f"{base} | None"


def _render_operation(op_kind: str, field: FieldPayload) -> bytes:
    fname, fsig, _fd, fargs = field
    arg_decls = ", ".join(f"${a[0]}: {a[1]}" for a in fargs)
    var_block = f"({arg_decls})" if arg_decls else ""
    arg_pass = ", ".join(f"{a[0]}: ${a[0]}" for a in fargs)
    call_block = f"({arg_pass})" if arg_pass else ""
    op_name = fname[0].upper() + fname[1:] if fname else "Operation"
    doc = (
        f"{op_kind} {op_name}{var_block} {{\n"
        f"  {fname}{call_block}\n"
        f"}}"
    )
    py_params = ", ".join(f"{_snake(a[0])}: {_signature_to_annotation(a[1])}" for a in fargs)
    lines = [
        _HEADER,
        "from __future__ import annotations",
        "",
        f"# {op_kind} root field {fname!r} -> {fsig}",
        f'OPERATION = """{doc}"""',
        "",
        "",
        f"def {_snake(fname)}({py_params}) -> str:",
        "    return OPERATION",
    ]
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _render_models_index(type_names: tuple[str, ...]) -> bytes:
    listed = ",\n".join(f"    {n!r}" for n in type_names)
    body = f"__all__ = [\n{listed},\n]" if type_names else "__all__: list[str] = []"
    return (_HEADER + "from __future__ import annotations\n\n" + body + "\n").encode("utf-8")


def _render_doc(type_payload: TypePayload) -> bytes:
    name, kind, description, interfaces, fields, enum_values, possible_types = type_payload
    lines = [f"# {name}", "", f"**Kind:** {kind}"]
    if interfaces:
        lines.append(f"**Implements:** {', '.join(interfaces)}")
    if possible_types:
        lines.append(f"**Members:** {', '.join(possible_types)}")
    lines.append("")
    if description:
        lines += [description, ""]
    if fields:
        lines += ["## Fields", ""]
        for fname, fsig, fdesc, fargs in fields:
            suffix = f" — {fdesc}" if fdesc else ""
            arg_note = f" ({', '.join(f'{a[0]}: {a[1]}' for a in fargs)})" if fargs else ""
            lines.append(f"- `{fname}{arg_note}`: `{fsig}`{suffix}")
        lines.append("")
    if enum_values:
        lines += ["## Values", ""]
        for value_name, vdesc in enum_values:
            suffix = f" — {vdesc}" if vdesc else ""
            lines.append(f"- `{value_name}`{suffix}")
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def _render_docs_index(payload: SchemaPayload) -> bytes:
    query_type, mutation_type, types, _diagnostics = payload
    lines = ["# GraphQL Schema", ""]
    lines.append(f"- Query root: `{query_type}`" if query_type else "- Query root: (none)")
    lines.append(f"- Mutation root: `{mutation_type}`" if mutation_type else "- Mutation root: (none)")
    lines += ["", "## Types", ""]
    for name, kind, *_rest in types:
        lines.append(f"- [`{name}`](types/{name}.md) ({kind})")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Artifact composition
# ---------------------------------------------------------------------------


def _root_fields(payload: SchemaPayload, root_name: str | None) -> tuple[FieldPayload, ...]:
    if root_name is None:
        return ()
    for name, _kind, _desc, _interfaces, fields, _enum, _poss in payload[2]:
        if name == root_name:
            return fields
    return ()


@query
def code_artifacts_payload(db: Database, path: str) -> tuple[ArtifactPayload, ...]:
    """Code-shape artifacts: models + operations + model index. Depends only on
    `code_model`, so a description-only edit backdates this whole query and the
    model/operation files are not re-rendered."""
    code = code_model(db, path)
    artifacts: list[ArtifactPayload] = []

    code_types = code[2]
    for type_payload in code_types:
        type_name = type_payload[0]
        if type_payload[1] == "SCALAR" and type_name in _BUILTIN_SCALARS:
            continue  # builtin scalars map to Python builtins; no file
        artifacts.append((f"models/{type_name}.py", _render_model(type_payload)))

    model_names = tuple(
        t[0] for t in code_types if not (t[1] == "SCALAR" and t[0] in _BUILTIN_SCALARS)
    )
    artifacts.append(("models/__init__.py", _render_models_index(model_names)))

    query_root, mutation_root = code[0], code[1]
    for op_kind, root_name in (("query", query_root), ("mutation", mutation_root)):
        for field in _root_fields(code, root_name):
            artifacts.append(
                (f"operations/{field[0]}.py", _render_operation(op_kind, field))
            )
    return tuple(artifacts)


@query
def doc_artifacts_payload(db: Database, path: str) -> tuple[ArtifactPayload, ...]:
    """Doc-shape artifacts: per-type docs + the docs index. Depends only on
    `doc_model`; the index lists type names/kinds (no descriptions), so a
    description-only edit rewrites only the affected per-type doc."""
    doc = doc_model(db, path)
    artifacts: list[ArtifactPayload] = [
        (f"docs/types/{type_payload[0]}.md", _render_doc(type_payload))
        for type_payload in doc[2]
    ]
    artifacts.append(("docs/index.md", _render_docs_index(doc)))
    return tuple(artifacts)


@query
def artifacts_payload(db: Database, path: str) -> tuple[ArtifactPayload, ...]:
    combined = list(code_artifacts_payload(db, path)) + list(doc_artifacts_payload(db, path))
    combined.sort(key=lambda item: item[0])
    return tuple(combined)


# ---------------------------------------------------------------------------
# Layer 3 — entrypoints
# ---------------------------------------------------------------------------


def _decode_type(payload: TypePayload) -> GraphQLType:
    name, kind, description, interfaces, fields, enum_values, possible_types = payload
    return GraphQLType(
        name=name,
        kind=kind,
        description=description,
        interfaces=interfaces,
        fields=tuple(
            GraphQLField(
                name=f[0],
                signature=f[1],
                description=f[2],
                arguments=tuple(
                    GraphQLArgument(name=a[0], signature=a[1], description=a[2]) for a in f[3]
                ),
            )
            for f in fields
        ),
        enum_values=tuple(GraphQLEnumValue(name=v[0], description=v[1]) for v in enum_values),
        possible_types=possible_types,
    )


def graphql_analysis(db: Database, path: str | os.PathLike[str]) -> GraphQLSchema:
    """Return the normalized GraphQL schema model for ``path``."""
    payload = cast(SchemaPayload, thaw(db.get(normalized_schema_payload, os.fspath(path))))
    query_type, mutation_type, types, diagnostics = payload
    return GraphQLSchema(
        query_type=query_type,
        mutation_type=mutation_type,
        types=tuple(_decode_type(t) for t in types),
        diagnostics=tuple(GraphQLDiagnostic(code=d[0], message=d[1]) for d in diagnostics),
    )


def graphql_artifacts(
    db: Database, path: str | os.PathLike[str], output_root: str | os.PathLike[str]
) -> DesiredArtifactSet:
    """Return the desired generated artifacts for ``path`` rooted at ``output_root``."""
    normalized = os.fspath(path)
    payload = cast("tuple[ArtifactPayload, ...]", thaw(db.get(artifacts_payload, normalized)))
    root = os.fspath(output_root)
    artifacts = tuple(DesiredArtifact(rel, content) for rel, content in payload)
    identity = ActionIdentity(action_id=_ACTION_ID, output_root=root, tool=_TOOL)
    return DesiredArtifactSet(identity, artifacts)


def generate_graphql(
    db: Database,
    path: str | os.PathLike[str],
    output_root: str | os.PathLike[str],
    *,
    state_dir: str | os.PathLike[str] | None = None,
) -> ActionResult:
    """Generate and reconcile GraphQL artifacts to ``output_root`` (outside queries)."""
    desired = graphql_artifacts(db, path, output_root)
    resolved_state = (
        default_state_dir(output_root, _ACTION_ID) if state_dir is None else state_dir
    )
    reconciler = FilesystemReconciler(output_root, state_dir=resolved_state)
    return reconciler.apply(desired)


__all__ = [
    "GraphQLArgument",
    "GraphQLDiagnostic",
    "GraphQLEnumValue",
    "GraphQLField",
    "GraphQLSchema",
    "GraphQLType",
    "generate_graphql",
    "graphql_analysis",
    "graphql_artifacts",
]
