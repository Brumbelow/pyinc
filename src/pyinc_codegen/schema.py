"""Incremental JSON-Schema analysis and rendering queries.

Only the deliberately small subset documented in ``docs/codegen-guide.md`` is
accepted. Unsupported or malformed shapes produce structured diagnostics; the
high-level generator refuses to reconcile outputs while any error is present.
"""

from __future__ import annotations

import json
import keyword
import math
import unicodedata
from collections.abc import Callable, Iterable

from pyinc import BinaryFileResource, Database, query

from .models import DiagnosticPayload, FieldPayload, ModelPayload

_FILES = BinaryFileResource()
_ERROR = "error"
_WARNING = "warning"
_MAX_JSON_DEPTH = 256
_MAX_PORTABLE_COMPONENT_BYTES = 255
_MAX_PORTABLE_COMPONENT_UTF16_UNITS = 255
_INVALID_UTF8_PREFIX = "\0pyinc-invalid-utf8:"
_DEFINITION_SECTIONS = ("$defs", "definitions")
_SCHEMA_ANNOTATION_KEYS = frozenset({"$comment", "description", "title"})
_ROOT_METADATA_KEYS = _SCHEMA_ANNOTATION_KEYS | frozenset({"$id", "$schema"})
_SHAPE_KEYWORDS = frozenset({"$ref", "enum", "items", "properties", "required"})
_ROOT_UNSUPPORTED_CONSTRUCTS = frozenset(
    {
        "allOf",
        "anyOf",
        "contains",
        "dependentSchemas",
        "else",
        "if",
        "not",
        "oneOf",
        "patternProperties",
        "prefixItems",
        "then",
        "unevaluatedProperties",
    }
)
_WINDOWS_RESERVED_MODULE_STEMS = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
    }
)


class _DuplicateKeyError(ValueError):
    pass


class _InvalidJsonError(ValueError):
    pass


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON number {value!r} is not permitted")


def _parse_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number {value!r} is not permitted")
    return parsed


def _object_from_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _validate_json_tree(root: object) -> None:
    pending = [(root, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > _MAX_JSON_DEPTH:
            raise ValueError(f"JSON nesting exceeds the supported limit of {_MAX_JSON_DEPTH}")
        if isinstance(value, str):
            if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
                raise ValueError("JSON strings must not contain unpaired surrogate code points")
            continue
        if isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                pending.append((key, depth))
                pending.append((item, depth + 1))


def _load(text: str) -> object:
    try:
        parsed: object = json.loads(
            text,
            object_pairs_hook=_object_from_pairs,
            parse_constant=_reject_constant,
            parse_float=_parse_float,
        )
        _validate_json_tree(parsed)
    except _InvalidJsonError:
        raise
    except (ValueError, RecursionError, OverflowError) as error:
        raise _InvalidJsonError(str(error)) from error
    return parsed


def _canonical_json_token(text: str) -> tuple[str, str]:
    try:
        parsed = _load(text)
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        return ("raw", text)
    return ("parsed", canonical)


def _pointer_token(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _pointer(base: str, *parts: str) -> str:
    suffix = "".join(f"/{_pointer_token(part)}" for part in parts)
    return f"{base}{suffix}"


def _diagnostic(
    code: str,
    message: str,
    json_pointer: str,
    *,
    severity: str = _ERROR,
) -> DiagnosticPayload:
    return (code, message, severity, json_pointer)


def _definition_entries(data: object) -> tuple[tuple[str, object, str], ...]:
    if not isinstance(data, dict):
        return ()
    entries: list[tuple[str, object, str]] = []
    seen: set[str] = set()
    for section_name in _DEFINITION_SECTIONS:
        section = data.get(section_name)
        if not isinstance(section, dict):
            continue
        for name, fragment in section.items():
            if name not in seen:
                entries.append((name, fragment, _pointer("", section_name, name)))
                seen.add(name)
    return tuple(entries)


def _all_defs(data: object) -> dict[str, object]:
    return {name: fragment for name, fragment, _path in _definition_entries(data)}


def _python_identifier(name: str) -> str:
    return unicodedata.normalize("NFKC", name)


def _snake(name: str) -> str:
    """Return a deterministic normalized snake-case module stem."""

    normalized = _python_identifier(name)
    chars: list[str] = []
    for index, char in enumerate(normalized):
        previous = normalized[index - 1] if index else ""
        following = normalized[index + 1] if index + 1 < len(normalized) else ""
        if (
            index
            and char.isupper()
            and (
                previous.islower()
                or previous.isdigit()
                or (previous.isupper() and following.islower())
            )
        ):
            chars.append("_")
        chars.append(char.lower())
    return "".join(chars) or "model"


def _module_collision_key(name: str) -> str:
    return unicodedata.normalize("NFKC", _snake(name)).casefold()


def _is_py_identifier(name: str) -> bool:
    normalized = _python_identifier(name)
    return normalized.isidentifier() and not keyword.iskeyword(normalized)


def _is_reserved_field_name(name: str) -> bool:
    normalized = _python_identifier(name)
    return normalized.startswith("__") and normalized.endswith("__")


def _module_name_diagnostics(name: str, json_pointer: str) -> tuple[DiagnosticPayload, ...]:
    stem = _snake(name)
    if not stem.isidentifier() or keyword.iskeyword(stem):
        return (
            _diagnostic(
                "invalid-module-name",
                f"definition maps to an invalid Python module name: {stem!r}",
                json_pointer,
            ),
        )
    if stem.casefold() in _WINDOWS_RESERVED_MODULE_STEMS:
        return (
            _diagnostic(
                "nonportable-module-name",
                f"definition maps to a Windows-reserved module name: {stem!r}",
                json_pointer,
            ),
        )
    if stem.casefold() == "__init__":
        return (
            _diagnostic(
                "reserved-module-name",
                "definition would overwrite the generated package index",
                json_pointer,
            ),
        )
    filename = f"{stem}.py"
    if (
        len(filename.encode("utf-8")) > _MAX_PORTABLE_COMPONENT_BYTES
        or len(filename.encode("utf-16-le")) // 2 > _MAX_PORTABLE_COMPONENT_UTF16_UNITS
    ):
        return (
            _diagnostic(
                "nonportable-module-name",
                "definition maps to a filename longer than portable filesystem limits",
                json_pointer,
            ),
        )
    return ()


def _decode_pointer_segment(segment: str) -> str | None:
    chars: list[str] = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char != "~":
            chars.append(char)
            index += 1
            continue
        if index + 1 >= len(segment) or segment[index + 1] not in "01":
            return None
        chars.append("~" if segment[index + 1] == "0" else "/")
        index += 2
    return "".join(chars)


def _percent_decode(value: str) -> str | None:
    encoded = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char != "%":
            encoded.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 2 >= len(value):
            return None
        digits = value[index + 1 : index + 3]
        if any(digit not in "0123456789abcdefABCDEF" for digit in digits):
            return None
        encoded.append(int(digits, 16))
        index += 3
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _local_ref_target(ref: str) -> tuple[str | None, str | None]:
    """Return ``(target, problem)`` for the supported local reference shape."""

    if not ref.startswith("#/"):
        return (None, "only local $defs/definitions references are supported")
    decoded_fragment = _percent_decode(ref[1:])
    if decoded_fragment is None:
        return (None, "the URI fragment contains invalid percent-encoded UTF-8")
    raw_segments = decoded_fragment[1:].split("/")
    segments = tuple(_decode_pointer_segment(segment) for segment in raw_segments)
    if any(segment is None for segment in segments):
        return (None, "the JSON Pointer contains an invalid '~' escape")
    if len(segments) != 2 or segments[0] not in _DEFINITION_SECTIONS:
        return (None, "only direct references into $defs or definitions are supported")
    return (segments[1], None)


def _annotation_diagnostics(
    spec: dict[str, object],
    json_pointer: str,
    allowed_annotations: frozenset[str],
) -> tuple[DiagnosticPayload, ...]:
    diagnostics: list[DiagnosticPayload] = []
    for name in sorted(set(spec) & allowed_annotations):
        if isinstance(spec[name], str):
            continue
        code = "invalid-description" if name == "description" else "invalid-annotation"
        diagnostics.append(
            _diagnostic(
                code,
                f"schema annotation {name!r} must be a string",
                _pointer(json_pointer, name),
            )
        )
    return tuple(diagnostics)


def _effective_type(type_field: object) -> object:
    if not isinstance(type_field, list):
        return type_field
    if len(type_field) != 2 or any(not isinstance(item, str) for item in type_field):
        return type_field
    non_null = [item for item in type_field if item != "null"]
    return non_null[0] if len(non_null) == 1 else type_field


def _keyword_diagnostic(
    name: str,
    json_pointer: str,
    *,
    ambiguous: bool = False,
) -> DiagnosticPayload:
    if ambiguous:
        return _diagnostic(
            "ambiguous-schema-combination",
            f"JSON Schema keyword {name!r} cannot be combined with the selected schema shape",
            _pointer(json_pointer, name),
        )
    return _diagnostic(
        "unsupported-construct",
        f"JSON Schema keyword {name!r} is not supported in this schema context",
        _pointer(json_pointer, name),
    )


def _schema_node_diagnostics(
    spec: dict[str, object],
    json_pointer: str,
    *,
    definition_context: bool,
) -> tuple[DiagnosticPayload, ...]:
    """Validate the complete keyword set for one supported schema-node context."""

    diagnostics = list(_annotation_diagnostics(spec, json_pointer, _SCHEMA_ANNOTATION_KEYS))
    structural = set(spec) - set(_SCHEMA_ANNOTATION_KEYS)

    if "$ref" in structural:
        for name in sorted(structural - {"$ref"}):
            diagnostics.append(_keyword_diagnostic(name, json_pointer, ambiguous=True))
        return tuple(diagnostics)

    if "enum" in structural:
        if not definition_context:
            diagnostics.append(_keyword_diagnostic("enum", json_pointer))
        for name in sorted(structural - {"enum", "type"}):
            diagnostics.append(
                _keyword_diagnostic(
                    name,
                    json_pointer,
                    ambiguous=name in _SHAPE_KEYWORDS,
                )
            )
        return tuple(diagnostics)

    type_field = spec.get("type")
    effective_type = _effective_type(type_field)
    has_properties = "properties" in structural
    if definition_context and has_properties:
        allowed = {"properties", "required", "type"}
        if type_field is not None and type_field != "object":
            diagnostics.append(_keyword_diagnostic("type", json_pointer, ambiguous=True))
    elif effective_type == "object":
        allowed = {"type"}
        if definition_context:
            allowed.update({"properties", "required"})
    elif effective_type == "array":
        allowed = {"items", "type"}
    elif "type" in structural:
        allowed = {"type"}
    else:
        allowed = set()

    for name in sorted(structural - allowed):
        shape_conflict = name in _SHAPE_KEYWORDS and bool(allowed)
        diagnostics.append(_keyword_diagnostic(name, json_pointer, ambiguous=shape_conflict))
    return tuple(diagnostics)


def _render_type(
    spec: object,
    definition_exists: Callable[[str], bool],
    json_pointer: str,
    *,
    validate_current: bool = True,
) -> tuple[str, tuple[str, ...], tuple[DiagnosticPayload, ...], bool]:
    primitives = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    if isinstance(spec, bool):
        fallback = "object" if spec else "Never"
        return (
            fallback,
            (),
            (
                _diagnostic(
                    "unsupported-boolean-schema",
                    "boolean schemas are not supported by the model generator",
                    json_pointer,
                ),
            ),
            False,
        )
    if not isinstance(spec, dict):
        return (
            "object",
            (),
            (
                _diagnostic(
                    "invalid-schema-node",
                    "a schema node must be an object or boolean",
                    json_pointer,
                ),
            ),
            False,
        )

    diagnostics = (
        list(
            _schema_node_diagnostics(
                spec,
                json_pointer,
                definition_context=False,
            )
        )
        if validate_current
        else []
    )

    ref = spec.get("$ref")
    if "$ref" in spec and not isinstance(ref, str):
        diagnostics.append(
            _diagnostic("invalid-ref", "$ref must be a string", _pointer(json_pointer, "$ref"))
        )
        return ("object", (), tuple(diagnostics), False)
    if isinstance(ref, str):
        target, problem = _local_ref_target(ref)
        if problem is not None:
            diagnostics.append(
                _diagnostic("unsupported-ref", problem, _pointer(json_pointer, "$ref"))
            )
            return ("object", (), tuple(diagnostics), False)
        assert target is not None
        if not definition_exists(target):
            diagnostics.append(
                _diagnostic(
                    "unknown-ref",
                    f"unresolved local $ref: {ref}",
                    _pointer(json_pointer, "$ref"),
                )
            )
            return ("object", (), tuple(diagnostics), False)
        if not _is_py_identifier(target):
            diagnostics.append(
                _diagnostic(
                    "unsupported-ref-name",
                    f"$ref target is not a Python identifier: {target!r}",
                    _pointer(json_pointer, "$ref"),
                )
            )
            return ("object", (), tuple(diagnostics), False)
        return (_python_identifier(target), (target,), tuple(diagnostics), False)

    if "enum" in spec:
        enum_values = spec["enum"]
        enum_pointer = _pointer(json_pointer, "enum")
        if not isinstance(enum_values, list):
            diagnostics.append(_diagnostic("invalid-enum", "enum must be an array", enum_pointer))
        else:
            if not enum_values:
                diagnostics.append(
                    _diagnostic("empty-enum", "enum must contain at least one value", enum_pointer)
                )
            if _duplicates(enum_values):
                diagnostics.append(
                    _diagnostic("duplicate-enum-value", "enum values must be unique", enum_pointer)
                )
            for index, value in enumerate(enum_values):
                value_pointer = _pointer(enum_pointer, str(index))
                if _enum_value(value) is None:
                    diagnostics.append(
                        _diagnostic(
                            "unsupported-enum-value",
                            "enum values must be strings, integers, booleans, or null",
                            value_pointer,
                        )
                    )
                elif not _enum_type_matches(value, spec.get("type")):
                    diagnostics.append(
                        _diagnostic(
                            "enum-type-mismatch",
                            f"enum value {value!r} does not match type {spec.get('type')!r}",
                            value_pointer,
                        )
                    )

    type_field = spec.get("type")
    if isinstance(type_field, list):
        if not type_field or any(not isinstance(item, str) for item in type_field):
            diagnostics.append(
                _diagnostic(
                    "unsupported-union",
                    "type unions must contain schema type names",
                    _pointer(json_pointer, "type"),
                )
            )
            return ("object", (), tuple(diagnostics), False)
        non_null = [item for item in type_field if item != "null"]
        if len(type_field) == 2 and len(non_null) == 1:
            rest = dict(spec)
            rest["type"] = non_null[0]
            inner, refs, nested, _inner_allows_none = _render_type(
                rest,
                definition_exists,
                json_pointer,
                validate_current=False,
            )
            return (f"{inner} | None", refs, tuple(diagnostics) + nested, True)
        diagnostics.append(
            _diagnostic(
                "unsupported-union",
                f"only a single type plus null is supported, got {type_field!r}",
                _pointer(json_pointer, "type"),
            )
        )
        return ("object", (), tuple(diagnostics), False)

    if type_field == "array":
        if "items" not in spec:
            diagnostics.append(
                _diagnostic(
                    "unconstrained-array-items",
                    "missing items is represented as object by policy",
                    json_pointer,
                    severity=_WARNING,
                )
            )
            return ("list[object]", (), tuple(diagnostics), False)
        item_type, refs, nested, _items_allow_none = _render_type(
            spec["items"], definition_exists, _pointer(json_pointer, "items")
        )
        return (f"list[{item_type}]", refs, tuple(diagnostics) + nested, False)

    if isinstance(type_field, str) and type_field in primitives:
        return (primitives[type_field], (), tuple(diagnostics), type_field == "null")

    if type_field == "object" or "properties" in spec:
        if "properties" in spec and not isinstance(spec["properties"], dict):
            diagnostics.append(
                _diagnostic(
                    "invalid-properties",
                    "properties must be an object",
                    _pointer(json_pointer, "properties"),
                )
            )
        return ("dict[str, object]", (), tuple(diagnostics), False)

    if isinstance(type_field, str):
        diagnostics.append(
            _diagnostic(
                "unsupported-type",
                f"schema type {type_field!r} is not supported",
                _pointer(json_pointer, "type"),
            )
        )
        return ("object", (), tuple(diagnostics), False)

    if type_field is not None:
        diagnostics.append(
            _diagnostic(
                "invalid-type",
                "type must be a string or a list of strings",
                _pointer(json_pointer, "type"),
            )
        )
        return ("object", (), tuple(diagnostics), False)

    diagnostics.append(
        _diagnostic(
            "unconstrained-schema",
            "an unconstrained schema is represented as object by policy",
            json_pointer,
            severity=_WARNING,
        )
    )
    return ("object", (), tuple(diagnostics), False)


def _enum_value(value: object) -> str | None:
    if value is None or isinstance(value, (str, bool, int)):
        return repr(value)
    return None


def _enum_type_matches(value: object, declared_type: object) -> bool:
    if declared_type is None:
        return True
    if declared_type == "null":
        return value is None
    if declared_type == "boolean":
        return isinstance(value, bool)
    if declared_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if declared_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if declared_type == "string":
        return isinstance(value, str)
    return False


def _duplicates(values: Iterable[object]) -> bool:
    seen: list[object] = []
    for value in values:
        if any(value == previous and type(value) is type(previous) for previous in seen):
            return True
        seen.append(value)
    return False


def _build_enum(
    name: str,
    fragment: dict[str, object],
    description: str,
    json_pointer: str,
    initial_diagnostics: tuple[DiagnosticPayload, ...],
) -> ModelPayload:
    diagnostics = list(initial_diagnostics)
    values = fragment.get("enum")
    if not isinstance(values, list):
        diagnostics.append(
            _diagnostic("invalid-enum", "enum must be an array", _pointer(json_pointer, "enum"))
        )
        rendered: tuple[str, ...] = ()
    else:
        rendered_values: list[str] = []
        if not values:
            diagnostics.append(
                _diagnostic(
                    "empty-enum",
                    "enum must contain at least one value",
                    _pointer(json_pointer, "enum"),
                )
            )
        if _duplicates(values):
            diagnostics.append(
                _diagnostic(
                    "duplicate-enum-value",
                    "enum values must be unique",
                    _pointer(json_pointer, "enum"),
                )
            )
        declared_type = fragment.get("type")
        for index, value in enumerate(values):
            value_pointer = _pointer(json_pointer, "enum", str(index))
            literal = _enum_value(value)
            if literal is None:
                diagnostics.append(
                    _diagnostic(
                        "unsupported-enum-value",
                        "enum values must be strings, integers, booleans, or null",
                        value_pointer,
                    )
                )
                continue
            if not _enum_type_matches(value, declared_type):
                diagnostics.append(
                    _diagnostic(
                        "enum-type-mismatch",
                        f"enum value {value!r} does not match type {declared_type!r}",
                        value_pointer,
                    )
                )
            rendered_values.append(literal)
        rendered = tuple(rendered_values)

    base_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    declared = fragment.get("type")
    if declared is not None and (not isinstance(declared, str) or declared not in base_map):
        diagnostics.append(
            _diagnostic(
                "unsupported-enum-type",
                f"enum type {declared!r} is not supported",
                _pointer(json_pointer, "type"),
            )
        )
    base_type = base_map.get(declared, "object") if isinstance(declared, str) else "object"
    return (name, "enum", (), rendered, base_type, description, (), tuple(diagnostics))


def _field_collision_diagnostics(
    names: Iterable[str], properties_pointer: str
) -> tuple[DiagnosticPayload, ...]:
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(_python_identifier(name), []).append(name)
    diagnostics: list[DiagnosticPayload] = []
    for normalized, originals in sorted(groups.items()):
        if len(originals) < 2:
            continue
        rendered = ", ".join(repr(item) for item in sorted(originals))
        for original in sorted(originals):
            diagnostics.append(
                _diagnostic(
                    "field-name-collision",
                    f"property names normalize to the same Python name {normalized!r}: {rendered}",
                    _pointer(properties_pointer, original),
                )
            )
    return tuple(diagnostics)


def _build_model(
    name: str,
    fragment: object,
    definition_exists: Callable[[str], bool],
    json_pointer: str,
) -> ModelPayload:
    name_diagnostics: tuple[DiagnosticPayload, ...] = (
        ()
        if _is_py_identifier(name)
        else (
            _diagnostic(
                "unsupported-definition-name",
                f"definition name is not a Python identifier: {name!r}",
                json_pointer,
            ),
        )
    )
    if not isinstance(fragment, dict):
        code = "unsupported-boolean-schema" if isinstance(fragment, bool) else "invalid-definition"
        message = (
            "boolean definitions are not supported"
            if isinstance(fragment, bool)
            else "a definition must be a schema object"
        )
        return (
            name,
            "alias",
            (),
            (),
            "object",
            "",
            (),
            name_diagnostics + (_diagnostic(code, message, json_pointer),),
        )

    diagnostics = name_diagnostics + _schema_node_diagnostics(
        fragment,
        json_pointer,
        definition_context=True,
    )
    raw_description = fragment.get("description", "")
    description = raw_description if isinstance(raw_description, str) else ""

    if "enum" in fragment:
        return _build_enum(name, fragment, description, json_pointer, diagnostics)

    if fragment.get("type") == "object" or "properties" in fragment:
        properties = fragment.get("properties", {})
        required_raw = fragment.get("required", [])
        required: set[str] = set()
        if not isinstance(required_raw, list) or any(
            not isinstance(item, str) for item in required_raw
        ):
            diagnostics += (
                _diagnostic(
                    "invalid-required",
                    "required must be an array of property names",
                    _pointer(json_pointer, "required"),
                ),
            )
        else:
            required = set(required_raw)
            if len(required) != len(required_raw):
                diagnostics += (
                    _diagnostic(
                        "duplicate-required-name",
                        "required property names must be unique",
                        _pointer(json_pointer, "required"),
                    ),
                )

        if not isinstance(properties, dict):
            diagnostics += (
                _diagnostic(
                    "invalid-properties",
                    "properties must be an object",
                    _pointer(json_pointer, "properties"),
                ),
            )
            properties = {}

        properties_pointer = _pointer(json_pointer, "properties")
        diagnostics += _field_collision_diagnostics(properties, properties_pointer)
        unknown_required = required - set(properties)
        for missing in sorted(unknown_required):
            diagnostics += (
                _diagnostic(
                    "unsupported-required-property",
                    f"required property {missing!r} has no generated property schema",
                    _pointer(json_pointer, "required"),
                ),
            )

        fields: list[FieldPayload] = []
        refs: set[str] = set()
        for property_name in sorted(properties):
            property_pointer = _pointer(properties_pointer, property_name)
            if not _is_py_identifier(property_name):
                diagnostics += (
                    _diagnostic(
                        "unsupported-field-name",
                        f"property name is not a Python identifier: {property_name!r}",
                        property_pointer,
                    ),
                )
                continue
            if _is_reserved_field_name(property_name):
                diagnostics += (
                    _diagnostic(
                        "reserved-field-name",
                        f"property name is reserved by the Python data model: {property_name!r}",
                        property_pointer,
                    ),
                )
                continue
            spec = properties[property_name]
            type_expr, property_refs, property_diagnostics, allows_none = _render_type(
                spec, definition_exists, property_pointer
            )
            property_description = spec.get("description", "") if isinstance(spec, dict) else ""
            if not isinstance(property_description, str):
                property_description = ""
            fields.append(
                (
                    _python_identifier(property_name),
                    type_expr,
                    property_name in required,
                    property_description,
                    allows_none,
                )
            )
            refs.update(property_refs)
            diagnostics += property_diagnostics
        return (
            name,
            "object",
            tuple(fields),
            (),
            "",
            description,
            tuple(sorted(refs)),
            diagnostics,
        )

    type_expr, alias_refs, type_diagnostics, _allows_none = _render_type(
        fragment,
        definition_exists,
        json_pointer,
        validate_current=False,
    )
    return (
        name,
        "alias",
        (),
        (),
        type_expr,
        description,
        tuple(sorted(alias_refs)),
        diagnostics + type_diagnostics,
    )


def _type_checking_imports(refs: tuple[str, ...], name: str) -> list[str]:
    return [
        f"    from .{_snake(ref)} import {_python_identifier(ref)}"
        for ref in sorted(refs)
        if ref != name
    ]


def _render_python(payload: ModelPayload) -> str:
    name, kind, fields, enum_values, base_type, _description, refs, _diagnostics = payload
    python_name = _python_identifier(name)
    if not _is_py_identifier(name):
        return f"# {name!r} cannot be emitted as a Python identifier.\n"

    lines: list[str] = ["from __future__ import annotations", ""]
    imports = _type_checking_imports(refs, name)

    if kind == "enum":
        if enum_values:
            lines += ["from typing import Literal, TypeAlias", ""]
            lines += [f"{python_name}: TypeAlias = Literal[{', '.join(enum_values)}]", ""]
        else:
            lines += ["from typing import Never, TypeAlias", ""]
            lines += [f"{python_name}: TypeAlias = Never", ""]
        return "\n".join(lines)

    if kind == "alias":
        typing_names = "TYPE_CHECKING, TypeAlias" if imports else "TypeAlias"
        lines += [f"from typing import {typing_names}", ""]
        if imports:
            lines += ["if TYPE_CHECKING:", *imports, ""]
        lines += [f"{python_name}: TypeAlias = {base_type!r}", ""]
        return "\n".join(lines)

    lines += ["from dataclasses import dataclass"]
    if imports:
        lines += ["from typing import TYPE_CHECKING", "", "if TYPE_CHECKING:", *imports]
    lines += ["", "", "@dataclass(frozen=True)", f"class {python_name}:"]
    required_fields = [field for field in fields if field[2]]
    optional_fields = [field for field in fields if not field[2]]
    if not required_fields and not optional_fields:
        lines.append("    pass")
    for field_name, type_expr, _required, _field_description, _allows_none in required_fields:
        lines.append(f"    {field_name}: {type_expr}")
    for field_name, type_expr, _required, _field_description, allows_none in optional_fields:
        optional_type = type_expr if allows_none else f"{type_expr} | None"
        lines.append(f"    {field_name}: {optional_type} = None")
    lines.append("")
    return "\n".join(lines)


def _render_doc(payload: ModelPayload) -> str:
    name, kind, fields, enum_values, base_type, description, _refs, diagnostics = payload
    lines: list[str] = [f"# {name}", ""]
    if description:
        lines += [description, ""]
    if kind == "enum":
        if enum_values:
            lines.append("Allowed values:")
            lines += [f"- `{value}`" for value in enum_values]
        else:
            lines.append("No values are allowed.")
    elif kind == "alias":
        lines.append(f"Type alias for `{base_type}`.")
    else:
        lines.append("Fields:")
        for field_name, type_expr, required, field_description, _allows_none in fields:
            flag = "required" if required else "optional"
            suffix = f" — {field_description}" if field_description else ""
            lines.append(f"- `{field_name}`: `{type_expr}` ({flag}){suffix}")
    for code, message, severity, json_pointer in diagnostics:
        lines.append(f"- {severity} diagnostic `{code}` at `{json_pointer or '/'}`: {message}")
    lines.append("")
    return "\n".join(lines)


@query(cutoff=_canonical_json_token)
def schema_text(db: Database, path: str) -> str:
    """Raw schema text with a semantic JSON cutoff."""

    raw = _FILES.read(db, path)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        # Keep malformed input representable by the kernel's value grammar while
        # preserving a deterministic, actionable diagnostic for the public API.
        return f"{_INVALID_UTF8_PREFIX}{error}"


@query
def document_diagnostics(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    text = schema_text(db, path)
    if text.startswith(_INVALID_UTF8_PREFIX):
        detail = text.removeprefix(_INVALID_UTF8_PREFIX)
        return (_diagnostic("invalid-json", f"schema is not valid UTF-8: {detail}", ""),)
    try:
        data = _load(text)
    except (json.JSONDecodeError, ValueError) as error:
        return (_diagnostic("invalid-json", str(error), ""),)
    if not isinstance(data, dict):
        return (_diagnostic("invalid-schema-root", "schema document must be a JSON object", ""),)

    diagnostics = list(_annotation_diagnostics(data, "", _ROOT_METADATA_KEYS))
    allowed_root_keys = set(_DEFINITION_SECTIONS) | set(_ROOT_METADATA_KEYS)
    for root_key in sorted(set(data) - allowed_root_keys):
        code = (
            "unsupported-construct"
            if root_key in _ROOT_UNSUPPORTED_CONSTRUCTS
            else "unsupported-root-schema"
        )
        diagnostics.append(
            _diagnostic(
                code,
                f"schema keyword {root_key!r} is not supported at the document root",
                _pointer("", root_key),
            )
        )
    locations_by_name: dict[str, list[str]] = {}
    unique_names: list[str] = []
    for section_name in _DEFINITION_SECTIONS:
        if section_name not in data:
            continue
        section = data[section_name]
        section_pointer = _pointer("", section_name)
        if not isinstance(section, dict):
            diagnostics.append(
                _diagnostic(
                    "invalid-definitions",
                    f"{section_name} must be an object",
                    section_pointer,
                )
            )
            continue
        for name in section:
            locations_by_name.setdefault(name, []).append(_pointer(section_pointer, name))
            if name not in unique_names:
                unique_names.append(name)

    for name, locations in sorted(locations_by_name.items()):
        if len(locations) < 2:
            continue
        for location in locations:
            diagnostics.append(
                _diagnostic(
                    "duplicate-definition",
                    f"definition {name!r} appears in both $defs and definitions",
                    location,
                )
            )

    collision_groups: dict[str, list[str]] = {}
    for name in unique_names:
        collision_groups.setdefault(_module_collision_key(name), []).append(name)
    for module_name, names in sorted(collision_groups.items()):
        if len(names) < 2:
            continue
        rendered = ", ".join(repr(item) for item in sorted(names))
        for name in sorted(names):
            location = locations_by_name[name][0]
            diagnostics.append(
                _diagnostic(
                    "module-name-collision",
                    f"definitions map to the same portable module {module_name!r}: {rendered}",
                    location,
                )
            )

    for name in unique_names:
        diagnostics.extend(_module_name_diagnostics(name, locations_by_name[name][0]))
    return tuple(diagnostics)


@query
def definition_names(db: Database, path: str) -> tuple[str, ...]:
    try:
        data = _load(schema_text(db, path))
    except (json.JSONDecodeError, ValueError):
        return ()
    return tuple(sorted(_all_defs(data)))


@query
def definition_raw(db: Database, path: str, name: str) -> str:
    """Canonical JSON for one definition, independent of sibling edits."""

    try:
        data = _load(schema_text(db, path))
    except (json.JSONDecodeError, ValueError):
        return ""
    for entry_name, fragment, _json_pointer in _definition_entries(data):
        if entry_name == name:
            return json.dumps(
                fragment,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
    return ""


@query
def definition_pointer(db: Database, path: str, name: str) -> str:
    try:
        data = _load(schema_text(db, path))
    except (json.JSONDecodeError, ValueError):
        return ""
    for entry_name, _fragment, json_pointer in _definition_entries(data):
        if entry_name == name:
            return json_pointer
    return ""


@query
def definition_model(db: Database, path: str, name: str) -> ModelPayload:
    raw = definition_raw(db, path, name)
    json_pointer = definition_pointer(db, path, name)
    if not raw or not json_pointer:
        return (
            name,
            "alias",
            (),
            (),
            "object",
            "",
            (),
            (_diagnostic("missing-definition", f"definition {name!r} does not exist", ""),),
        )

    def definition_exists(target: str) -> bool:
        return bool(definition_raw(db, path, target))

    return _build_model(name, _load(raw), definition_exists, json_pointer)


@query
def definition_structure(db: Database, path: str, name: str) -> ModelPayload:
    """Description-free model used by Python rendering and ref closure edges."""

    model = definition_model(db, path, name)
    fields = tuple(
        (field_name, type_expr, required, "", allows_none)
        for field_name, type_expr, required, _description, allows_none in model[2]
    )
    return (model[0], model[1], fields, model[3], model[4], "", model[6], model[7])


@query
def model_python(db: Database, path: str, name: str) -> str:
    payload = definition_structure(db, path, name)
    pending = list(payload[6])
    visited = {name}
    while pending:
        ref = pending.pop()
        if ref in visited:
            continue
        visited.add(ref)
        referenced = definition_structure(db, path, ref)
        pending.extend(referenced[6])
    return _render_python(payload)


@query
def model_doc(db: Database, path: str, name: str) -> str:
    return _render_doc(definition_model(db, path, name))


@query
def index_init(db: Database, path: str) -> str:
    names = [name for name in definition_names(db, path) if _is_py_identifier(name)]
    if not names:
        return "__all__: list[str] = []\n"
    imports = "\n".join(f"from .{_snake(name)} import {_python_identifier(name)}" for name in names)
    exports = ", ".join(repr(_python_identifier(name)) for name in names)
    return f"{imports}\n\n__all__ = [{exports}]\n"
