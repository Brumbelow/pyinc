"""JSON-Schema query graph: file -> defs -> per-definition model -> rendered
Python / docs / index. Stdlib-only (``json`` + dict walking), built entirely on
pyinc's public API.

Decomposition for output-granular incrementality:

    schema_text (cutoff=canonical JSON)  -> whitespace/key-reorder edits backdate
    definition_names                     -> sorted $defs + definitions names
    definition_raw(name)                 -> one definition's canonical JSON; backdates per-def
    definition_model(name)               -> one definition's semantic model payload
    model_python(name)                   -> rendered .py (description-independent -> backdates on doc-only edits)
    model_doc(name)                      -> rendered .md (includes descriptions)
    index_init                           -> aggregate __init__.py (depends only on the name set)

The "local reference graph" is realised as ``model_python(A)`` depending on
``definition_model(B)`` for each ``$ref`` target ``B``: a change to ``B`` puts
``A`` in the re-validation closure; ``A`` is rewritten only if its emitted bytes
actually change.

Regexes are avoided and primitive maps are inlined as locals because the kernel
rejects mutable / ``Pattern`` ambient captures reachable from a query.
"""

from __future__ import annotations

import json
import keyword

from pyinc import Database, FileResource, query

from .models import DiagnosticPayload, FieldPayload, ModelPayload

_FILES = FileResource()  # ONE shared file resource; the path is the node key.


def _canonical_json_token(text: str) -> tuple[str, str]:
    try:
        return ("parsed", json.dumps(json.loads(text), sort_keys=True))
    except json.JSONDecodeError:
        return ("raw", text)


def _load(text: str) -> object:
    parsed: object = json.loads(text)
    return parsed


def _all_defs(data: object) -> dict[str, object]:
    if not isinstance(data, dict):
        return {}
    result: dict[str, object] = {}
    for key in ("$defs", "definitions"):
        section = data.get(key)
        if isinstance(section, dict):
            for name, frag in section.items():
                if isinstance(name, str):
                    result.setdefault(name, frag)
    return result


def _snake(name: str) -> str:
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0 and not name[index - 1].isupper():
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars) or "model"


def _is_py_identifier(name: str) -> bool:
    """True if `name` can be emitted as a Python class/field name as-is."""
    return name.isidentifier() and not keyword.iskeyword(name)


def _render_type(
    spec: object, defs_names: frozenset[str]
) -> tuple[str, tuple[str, ...], tuple[DiagnosticPayload, ...]]:
    primitives = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    if not isinstance(spec, dict):
        return ("object", (), ())

    ref = spec.get("$ref")
    if isinstance(ref, str):
        for prefix in ("#/$defs/", "#/definitions/"):
            if ref.startswith(prefix):
                target = ref[len(prefix) :]
                if target in defs_names:
                    if _is_py_identifier(target):
                        return (target, (target,), ())
                    return (
                        "object",
                        (),
                        (("unsupported-ref-name", f"$ref target is not an identifier: {target!r}"),),
                    )
                return ("object", (), (("unknown-ref", f"unresolved local $ref: {ref}"),))
        return ("object", (), (("unsupported-ref", f"non-local $ref: {ref}"),))

    for combinator in ("allOf", "anyOf", "oneOf"):
        if combinator in spec:
            return ("object", (), (("unsupported-combinator", f"{combinator} is not supported"),))

    if "enum" in spec:
        base = spec.get("type")
        return (primitives.get(base, "str") if isinstance(base, str) else "str", (), ())

    type_field = spec.get("type")
    if isinstance(type_field, list):
        non_null = [item for item in type_field if item != "null"]
        if len(non_null) == 1 and isinstance(non_null[0], str):
            rest = dict(spec)
            rest["type"] = non_null[0]
            inner, refs, diags = _render_type(rest, defs_names)
            return (f"{inner} | None" if "null" in type_field else inner, refs, diags)
        return ("object", (), (("unsupported-union", f"unsupported type union: {type_field!r}"),))

    if type_field == "array":
        item_type, refs, diags = _render_type(spec.get("items", {}), defs_names)
        return (f"list[{item_type}]", refs, diags)

    if isinstance(type_field, str) and type_field in primitives:
        return (primitives[type_field], (), ())

    if type_field == "object":
        return ("dict[str, object]", (), ())

    return ("object", (), ())


def _build_model(name: str, frag: object, defs_names: frozenset[str]) -> ModelPayload:
    name_diags: tuple[DiagnosticPayload, ...] = (
        ()
        if _is_py_identifier(name)
        else (("unsupported-definition-name", f"definition name is not an identifier: {name!r}"),)
    )
    if not isinstance(frag, dict):
        return (name, "alias", (), (), "object", "", (), name_diags + (("invalid-definition", name),))

    raw_desc = frag.get("description", "")
    description = raw_desc if isinstance(raw_desc, str) else ""

    if "enum" in frag:
        values = frag.get("enum")
        rendered = tuple(repr(item) for item in values) if isinstance(values, list) else ()
        base = frag.get("type")
        base_map = {"string": "str", "integer": "int", "number": "float", "boolean": "bool"}
        base_type = base_map.get(base, "str") if isinstance(base, str) else "str"
        return (name, "enum", (), rendered, base_type, description, (), name_diags)

    if frag.get("type") == "object" or "properties" in frag:
        props = frag.get("properties")
        required_raw = frag.get("required")
        required = (
            {item for item in required_raw if isinstance(item, str)}
            if isinstance(required_raw, list)
            else set()
        )
        fields: list[FieldPayload] = []
        refs: set[str] = set()
        diags: list[DiagnosticPayload] = []
        if isinstance(props, dict):
            for prop_name in sorted(props):
                if not _is_py_identifier(prop_name):
                    diags.append(
                        ("unsupported-field-name", f"property name is not an identifier: {prop_name!r}")
                    )
                    continue
                spec = props[prop_name]
                type_expr, prop_refs, prop_diags = _render_type(spec, defs_names)
                prop_desc = spec.get("description", "") if isinstance(spec, dict) else ""
                prop_desc = prop_desc if isinstance(prop_desc, str) else ""
                fields.append((prop_name, type_expr, prop_name in required, prop_desc))
                refs.update(prop_refs)
                diags.extend(prop_diags)
        return (
            name,
            "object",
            tuple(fields),
            (),
            "",
            description,
            tuple(sorted(refs)),
            name_diags + tuple(diags),
        )

    type_expr, refs2, diags2 = _render_type(frag, defs_names)
    return (
        name,
        "alias",
        (),
        (),
        type_expr,
        description,
        tuple(sorted(refs2)),
        name_diags + tuple(diags2),
    )


def _render_python(payload: ModelPayload) -> str:
    name, kind, fields, enum_values, base_type, _description, refs, _diags = payload
    imports = [f"from .{_snake(ref)} import {ref}" for ref in sorted(refs) if ref != name]
    lines: list[str] = ["from __future__ import annotations", ""]

    if kind == "enum":
        lines += ["from typing import Literal", "", f"{name} = Literal[{', '.join(enum_values)}]", ""]
        return "\n".join(lines)

    if kind == "alias":
        if imports:
            lines += [*imports, ""]
        lines += [f"{name} = {base_type}", ""]
        return "\n".join(lines)

    lines += ["from dataclasses import dataclass"]
    if imports:
        lines += ["", *imports]
    lines += ["", "", "@dataclass(frozen=True)", f"class {name}:"]
    required_fields = [field for field in fields if field[2]]
    optional_fields = [field for field in fields if not field[2]]
    if not required_fields and not optional_fields:
        lines.append("    pass")
    for field_name, type_expr, _req, _desc in required_fields:
        lines.append(f"    {field_name}: {type_expr}")
    for field_name, type_expr, _req, _desc in optional_fields:
        optional = type_expr if "None" in type_expr else f"{type_expr} | None"
        lines.append(f"    {field_name}: {optional} = None")
    lines.append("")
    return "\n".join(lines)


def _render_doc(payload: ModelPayload) -> str:
    name, kind, fields, enum_values, base_type, description, _refs, diags = payload
    lines: list[str] = [f"# {name}", ""]
    if description:
        lines += [description, ""]
    if kind == "enum":
        lines.append("Allowed values:")
        lines += [f"- `{value}`" for value in enum_values]
    elif kind == "alias":
        lines.append(f"Type alias for `{base_type}`.")
    else:
        lines.append("Fields:")
        for field_name, type_expr, required, field_desc in fields:
            flag = "required" if required else "optional"
            suffix = f" — {field_desc}" if field_desc else ""
            lines.append(f"- `{field_name}`: `{type_expr}` ({flag}){suffix}")
    for code, message in diags:
        lines.append(f"- diagnostic `{code}`: {message}")
    lines.append("")
    return "\n".join(lines)


@query(cutoff=_canonical_json_token)
def schema_text(db: Database, path: str) -> str:
    """Raw schema text. Whitespace and key-reordering edits backdate."""
    return _FILES.read(db, path)


@query
def definition_names(db: Database, path: str) -> tuple[str, ...]:
    try:
        data = _load(schema_text(db, path))
    except json.JSONDecodeError:
        return ()
    return tuple(sorted(_all_defs(data)))


@query
def definition_raw(db: Database, path: str, name: str) -> str:
    """Canonical JSON of one definition. Backdates when that definition's
    fragment is unchanged, even if the rest of the document changed."""
    try:
        data = _load(schema_text(db, path))
    except json.JSONDecodeError:
        return ""
    frag = _all_defs(data).get(name)
    return json.dumps(frag, sort_keys=True) if frag is not None else ""


@query
def definition_model(db: Database, path: str, name: str) -> ModelPayload:
    raw = definition_raw(db, path, name)
    names = frozenset(definition_names(db, path))
    if not raw:
        return (name, "alias", (), (), "object", "", (), (("missing-definition", name),))
    return _build_model(name, _load(raw), names)


@query
def model_python(db: Database, path: str, name: str) -> str:
    payload = definition_model(db, path, name)
    for ref in payload[6]:
        definition_model(db, path, ref)  # establish the local reference-graph edge
    if not _is_py_identifier(name):
        # Can't emit `class <name>:` — keep the file valid Python and record why.
        return f"# {name!r} skipped: not a valid Python identifier\n"
    return _render_python(payload)


@query
def model_doc(db: Database, path: str, name: str) -> str:
    return _render_doc(definition_model(db, path, name))


@query
def index_init(db: Database, path: str) -> str:
    names = [name for name in definition_names(db, path) if _is_py_identifier(name)]
    if not names:
        return "__all__: list[str] = []\n"
    imports = "\n".join(f"from .{_snake(name)} import {name}" for name in names)
    exports = ", ".join(f'"{name}"' for name in names)
    return f"{imports}\n\n__all__ = [{exports}]\n"
