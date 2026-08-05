"""Incremental JSON-Schema analysis and rendering queries.

Only the deliberately small subset documented in ``docs/codegen-guide.md`` is
compiled into types. Annotation- and validation-only keywords are accepted with
a non-blocking ``ignored-constraint`` warning naming what the emitted type does
not enforce; unsupported or malformed shapes produce error diagnostics, and the
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
_SHAPE_KEYWORDS = frozenset({"$ref", "const", "enum", "items", "properties", "required"})
# The combinator spellings that select a shape, in the order they are selected.
_SUPPORTED_COMBINATORS = ("allOf", "anyOf")
# Keywords that select a schema shape before the ``type``-driven branches run,
# in the order they are applied wherever the compiler reads a schema node.
_SHAPE_SELECTOR_ORDER = ("$ref", *_SUPPORTED_COMBINATORS, "enum", "const")
_SHAPE_SELECTORS = frozenset(_SHAPE_SELECTOR_ORDER)
_IGNORED_NUMBER_KEYWORDS = frozenset({"exclusiveMaximum", "exclusiveMinimum", "maximum", "minimum"})
_IGNORED_SIZE_KEYWORDS = frozenset({"maxItems", "maxLength", "minItems", "minLength"})
_IGNORED_STRING_KEYWORDS = frozenset({"format", "pattern"})
_IGNORED_BOOLEAN_KEYWORDS = frozenset(
    {"additionalProperties", "deprecated", "readOnly", "uniqueItems", "writeOnly"}
)
_IGNORED_ANNOTATION_KEYWORDS = frozenset(
    {"default", "deprecated", "examples", "readOnly", "writeOnly"}
)
# Annotation- and validation-only keywords: accepted everywhere a schema node is
# accepted, validated for value shape, and reported as non-blocking
# ``ignored-constraint`` warnings because the emitted type cannot enforce them.
_IGNORED_KEYWORDS = (
    _IGNORED_NUMBER_KEYWORDS
    | _IGNORED_SIZE_KEYWORDS
    | _IGNORED_STRING_KEYWORDS
    | _IGNORED_BOOLEAN_KEYWORDS
    | frozenset({"default", "examples", "multipleOf"})
)
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
# The closed set of names every generated module binds: the fixed imports
# ``_render_python`` emits plus the builtins ``_render_type`` spells in type
# expressions. A model class with one of these names would shadow the binding
# in every module that imports it under ``TYPE_CHECKING``, and a field with one
# shadows it for the rest of its own class body, silently changing what the
# other annotations mean. Keep this in sync with the emitter.
_EMITTER_BOUND_NAMES = frozenset(
    {
        # Fixed imports.
        "dataclass",
        "Literal",
        "TypeAlias",
        "TYPE_CHECKING",
        "Never",
        # Builtins used in rendered type expressions.
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "object",
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


def _shadows_emitter_binding(name: str) -> bool:
    return _python_identifier(name) in _EMITTER_BOUND_NAMES


def _definition_name_diagnostics(name: str, json_pointer: str) -> tuple[DiagnosticPayload, ...]:
    normalized = _python_identifier(name)
    if _shadows_emitter_binding(name):
        return (
            _diagnostic(
                "reserved-definition-name",
                f"definition name shadows a binding the generated modules rely on: {normalized!r}",
                json_pointer,
            ),
        )
    return ()


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


def _constraint_shape_problem(name: str, value: object) -> str | None:
    """Return the expected value shape when an ignored keyword is malformed."""

    if name == "multipleOf":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        return None if valid else "a number greater than zero"
    if name in _IGNORED_NUMBER_KEYWORDS:
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        return None if valid else "a number"
    if name in _IGNORED_SIZE_KEYWORDS:
        valid = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        return None if valid else "a non-negative integer"
    if name in _IGNORED_STRING_KEYWORDS:
        return None if isinstance(value, str) else "a string"
    if name in _IGNORED_BOOLEAN_KEYWORDS:
        return None if isinstance(value, bool) else "a boolean"
    if name == "examples":
        return None if isinstance(value, list) else "an array"
    return None  # "default" carries an instance value of any JSON shape.


def _mapping_value_schema(spec: dict[str, object]) -> dict[str, object] | None:
    """Return the ``additionalProperties`` schema this node renders as ``dict[str, T]``."""

    value = spec.get("additionalProperties")
    if not isinstance(value, dict):
        return None
    if set(spec) & _SHAPE_SELECTORS:
        return None
    # Membership, not the value: a present ``"type": null`` is an invalid type,
    # not an absent keyword, so it must not select the mapping rendering.
    if "type" in spec and _effective_type(spec["type"]) != "object":
        return None
    return value


def _ignored_keyword_diagnostics(
    spec: dict[str, object],
    json_pointer: str,
    *,
    renders_additional_properties: bool = False,
) -> tuple[DiagnosticPayload, ...]:
    """Accept annotation- and validation-only keywords, naming what is ignored."""

    diagnostics: list[DiagnosticPayload] = []
    for name in sorted(set(spec) & _IGNORED_KEYWORDS):
        value = spec[name]
        keyword_pointer = _pointer(json_pointer, name)
        if name == "additionalProperties" and isinstance(value, dict):
            if renders_additional_properties:
                # Compiled into the mapping's value type, so nothing is ignored.
                continue
            diagnostics.append(
                _diagnostic(
                    "unsupported-construct",
                    "schema-valued 'additionalProperties' is not supported in this schema context",
                    keyword_pointer,
                )
            )
            continue
        problem = _constraint_shape_problem(name, value)
        if problem is not None:
            diagnostics.append(
                _diagnostic(
                    "invalid-constraint",
                    f"JSON Schema keyword {name!r} must be {problem}",
                    keyword_pointer,
                )
            )
            continue
        detail = (
            "it does not affect the generated type"
            if name in _IGNORED_ANNOTATION_KEYWORDS
            else "the generated type does not enforce it"
        )
        diagnostics.append(
            _diagnostic(
                "ignored-constraint",
                f"JSON Schema keyword {name!r} is accepted and ignored: {detail}",
                keyword_pointer,
                severity=_WARNING,
            )
        )
    return tuple(diagnostics)


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
    diagnostics.extend(
        _ignored_keyword_diagnostics(
            spec,
            json_pointer,
            renders_additional_properties=(
                not definition_context and _mapping_value_schema(spec) is not None
            ),
        )
    )
    structural = set(spec) - set(_SCHEMA_ANNOTATION_KEYS) - _IGNORED_KEYWORDS

    if "$ref" in structural:
        for name in sorted(structural - {"$ref"}):
            diagnostics.append(_keyword_diagnostic(name, json_pointer, ambiguous=True))
        return tuple(diagnostics)

    combinator = next((name for name in _SUPPORTED_COMBINATORS if name in structural), None)
    if combinator is not None:
        for name in sorted(structural - {combinator}):
            diagnostics.append(_keyword_diagnostic(name, json_pointer, ambiguous=True))
        return tuple(diagnostics)

    # Both keywords select a closed set of literal values and admit only a
    # ``type`` beside them; whichever comes first wins, and the other reads as
    # a competing shape.
    for selector in ("enum", "const"):
        if selector not in structural:
            continue
        for name in sorted(structural - {selector, "type"}):
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
        if "type" in spec and type_field != "object":
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


def _is_null_schema(branch: object) -> bool:
    return (
        isinstance(branch, dict)
        and branch.get("type") == "null"
        and not set(branch) - _SCHEMA_ANNOTATION_KEYS - {"type"}
    )


def _render_combinator(
    keyword: str,
    branches: object,
    definition_exists: Callable[[str], bool],
    json_pointer: str,
) -> tuple[str, tuple[str, ...], tuple[DiagnosticPayload, ...], bool]:
    """Render the two supported combinator spellings, both of which name one type."""

    keyword_pointer = _pointer(json_pointer, keyword)
    if isinstance(branches, list) and keyword == "allOf" and len(branches) == 1:
        return _render_type(branches[0], definition_exists, _pointer(keyword_pointer, "0"))
    if isinstance(branches, list) and keyword == "anyOf" and len(branches) == 2:
        null_indexes = [index for index, branch in enumerate(branches) if _is_null_schema(branch)]
        if len(null_indexes) == 2:
            return (
                "object",
                (),
                (
                    _diagnostic(
                        "unsupported-construct",
                        "an 'anyOf' whose branches are both {\"type\": \"null\"} names no type "
                        "to make optional",
                        keyword_pointer,
                    ),
                ),
                False,
            )
        if len(null_indexes) == 1:
            null_index = null_indexes[0]
            value_index = 1 - null_index
            inner, refs, value_diagnostics, allows_none = _render_type(
                branches[value_index],
                definition_exists,
                _pointer(keyword_pointer, str(value_index)),
            )
            # The null branch selects optionality rather than a type, so it never
            # reaches ``_render_type``; its annotations are validated here so a
            # malformed one is not the single place the check does not run.
            null_diagnostics = _annotation_diagnostics(
                branches[null_index],
                _pointer(keyword_pointer, str(null_index)),
                _SCHEMA_ANNOTATION_KEYS,
            )
            diagnostics = (
                null_diagnostics + value_diagnostics
                if null_index < value_index
                else value_diagnostics + null_diagnostics
            )
            return (inner if allows_none else f"{inner} | None", refs, diagnostics, True)
    problem = (
        "only a single-branch 'allOf' is supported, not multi-branch composition"
        if keyword == "allOf"
        else 'only an \'anyOf\' of one schema and {"type": "null"} is supported, not a union'
    )
    return ("object", (), (_diagnostic("unsupported-construct", problem, keyword_pointer),), False)


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

    for combinator in _SUPPORTED_COMBINATORS:
        if combinator in spec:
            inner, refs, nested, allows_none = _render_combinator(
                combinator, spec[combinator], definition_exists, json_pointer
            )
            return (inner, refs, tuple(diagnostics) + nested, allows_none)

    if "enum" in spec:
        enum_values = spec["enum"]
        enum_pointer = _pointer(json_pointer, "enum")
        members: list[str] = []
        nullable = False
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
                if not _enum_type_matches(value, spec.get("type")):
                    diagnostics.append(
                        _diagnostic(
                            "enum-type-mismatch",
                            f"enum value {value!r} does not match type {spec.get('type')!r}",
                            value_pointer,
                        )
                    )
                members.append(literal)
                nullable = nullable or value is None
        if not members:
            return ("object", (), tuple(diagnostics), False)
        return (f"Literal[{', '.join(members)}]", (), tuple(diagnostics), nullable)

    if "const" in spec:
        const_value = spec["const"]
        const_pointer = _pointer(json_pointer, "const")
        literal = _enum_value(const_value)
        if literal is None:
            diagnostics.append(
                _diagnostic(
                    "unsupported-const-value",
                    "const must be a string, integer, boolean, or null",
                    const_pointer,
                )
            )
            return ("object", (), tuple(diagnostics), False)
        if not _enum_type_matches(const_value, spec.get("type")):
            diagnostics.append(
                _diagnostic(
                    "const-type-mismatch",
                    f"const value {const_value!r} does not match type {spec.get('type')!r}",
                    const_pointer,
                )
            )
        return (f"Literal[{literal}]", (), tuple(diagnostics), const_value is None)

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
            # ``prefixItems`` constrains the items positionally; it is reported
            # where it appears, so the item type is not unconstrained here.
            if "prefixItems" not in spec:
                diagnostics.append(
                    _diagnostic(
                        "unconstrained-array-items",
                        "missing items is represented as object by policy",
                        json_pointer,
                        severity=_WARNING,
                    )
                )
            return ("list[object]", (), tuple(diagnostics), False)
        if isinstance(spec["items"], list):
            diagnostics.append(
                _diagnostic(
                    "unsupported-tuple-items",
                    "the draft-07 tuple form of 'items' (an array of positional schemas) "
                    "is not supported",
                    _pointer(json_pointer, "items"),
                )
            )
            return ("list[object]", (), tuple(diagnostics), False)
        item_type, refs, nested, _items_allow_none = _render_type(
            spec["items"], definition_exists, _pointer(json_pointer, "items")
        )
        return (f"list[{item_type}]", refs, tuple(diagnostics) + nested, False)

    if isinstance(type_field, str) and type_field in primitives:
        return (primitives[type_field], (), tuple(diagnostics), type_field == "null")

    value_schema = _mapping_value_schema(spec)
    if type_field == "object" or "properties" in spec or value_schema is not None:
        if "properties" in spec and not isinstance(spec["properties"], dict):
            diagnostics.append(
                _diagnostic(
                    "invalid-properties",
                    "properties must be an object",
                    _pointer(json_pointer, "properties"),
                )
            )
        if value_schema is None:
            return ("dict[str, object]", (), tuple(diagnostics), False)
        value_type, refs, nested, _value_allows_none = _render_type(
            value_schema, definition_exists, _pointer(json_pointer, "additionalProperties")
        )
        return (f"dict[str, {value_type}]", refs, tuple(diagnostics) + nested, False)

    if isinstance(type_field, str):
        diagnostics.append(
            _diagnostic(
                "unsupported-type",
                f"schema type {type_field!r} is not supported",
                _pointer(json_pointer, "type"),
            )
        )
        return ("object", (), tuple(diagnostics), False)

    if "type" in spec:
        # A present ``type`` whose value is JSON null reaches here as Python
        # ``None``; presence, not the value, distinguishes it from an absent
        # keyword, so it is diagnosed instead of taking the warning below.
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
    if isinstance(declared_type, list):
        # The one union the compiler renders names a type plus null, so a member
        # agrees with it when it matches that type or is the null it adds. Any
        # other list names no single type to check a member against.
        effective = _effective_type(declared_type)
        if not isinstance(effective, str):
            return False
        return value is None or _enum_type_matches(value, effective)
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
    base_map = {
        "string": "str",
        "integer": "int",
        "number": "float",
        "boolean": "bool",
        "null": "None",
    }
    declared_type = fragment.get("type")
    effective_declared = _effective_type(declared_type)
    declared_is_usable = effective_declared is None or (
        isinstance(effective_declared, str) and effective_declared in base_map
    )
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
            if declared_is_usable and not _enum_type_matches(value, declared_type):
                diagnostics.append(
                    _diagnostic(
                        "enum-type-mismatch",
                        f"enum value {value!r} does not match type {declared_type!r}",
                        value_pointer,
                    )
                )
            rendered_values.append(literal)
        rendered = tuple(rendered_values)

    # The supported nullable union names the type the members are drawn from;
    # the enum already carries the null, so the union adds no base type.
    declared = effective_declared
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


def _alias_names_itself(name: str, type_expr: str, refs: tuple[str, ...]) -> bool:
    """Whether an alias resolves to its own name with nothing in between.

    ``X: TypeAlias = 'X'`` — or ``'X | None'``, which the nullable spellings
    render — denotes no type at all. Recursion that passes through a model or a
    container (``list[X]``) names one and keeps compiling.
    """

    if name not in refs:
        return False
    python_name = _python_identifier(name)
    return type_expr in (python_name, f"{python_name} | None")


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

    # A definition selects its shape in the same precedence ``_render_type``
    # uses, so a shape-selecting keyword is never dropped by the type-driven
    # object branch running first.
    selector = next((name for name in _SHAPE_SELECTOR_ORDER if name in fragment), None)
    if selector == "enum":
        return _build_enum(name, fragment, description, json_pointer, diagnostics)

    if selector is None and (fragment.get("type") == "object" or "properties" in fragment):
        if "properties" not in fragment and _mapping_value_schema(fragment) is None:
            # A dataclass with no fields cannot hold the instance data such a
            # definition accepts, and a $ref to it would type that data away. A
            # mapping value schema does constrain that data; it is rejected on
            # its own keyword, so repeating it here would misname the cause.
            diagnostics += (
                _diagnostic(
                    "unconstrained-object-model",
                    "an object definition without 'properties' generates a model with no "
                    "fields, so it represents none of the data it accepts",
                    json_pointer,
                    severity=_WARNING,
                ),
            )
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
            if _shadows_emitter_binding(property_name):
                # A field binds its name in the class body, so every later
                # annotation there reads the field instead of the import or
                # builtin it names. Rejected like the definition name that
                # shadows the same binding at module scope.
                diagnostics += (
                    _diagnostic(
                        "reserved-field-name",
                        "property name shadows a binding the generated module relies on: "
                        f"{_python_identifier(property_name)!r}",
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
    if _alias_names_itself(name, type_expr, alias_refs):
        type_diagnostics += (
            _diagnostic(
                "self-referential-alias",
                f"alias definition names only itself: {type_expr!r} resolves to no other type",
                json_pointer,
            ),
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


def _typing_import_names(expressions: Iterable[str], *, type_checking: bool) -> list[str]:
    """Typing names the rendered expressions need, in the project's import order."""

    names = ["TYPE_CHECKING"] if type_checking else []
    if any("Literal[" in expression for expression in expressions):
        names.append("Literal")
    return names


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
        typing_names = [
            *_typing_import_names([base_type], type_checking=bool(imports)),
            "TypeAlias",
        ]
        lines += [f"from typing import {', '.join(typing_names)}", ""]
        if imports:
            lines += ["if TYPE_CHECKING:", *imports, ""]
        lines += [f"{python_name}: TypeAlias = {base_type!r}", ""]
        return "\n".join(lines)

    lines += ["from dataclasses import dataclass"]
    typing_names = _typing_import_names([field[1] for field in fields], type_checking=bool(imports))
    if typing_names:
        lines.append(f"from typing import {', '.join(typing_names)}")
    if imports:
        lines += ["", "if TYPE_CHECKING:", *imports]
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


@query
def schema_text(db: Database, path: str) -> str:
    """Exact raw schema text, or a deterministic invalid-UTF-8 marker."""

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
    root_model_keys: list[str] = []
    for root_key in sorted(set(data) - allowed_root_keys):
        if root_key in _ROOT_UNSUPPORTED_CONSTRUCTS:
            diagnostics.append(
                _diagnostic(
                    "unsupported-construct",
                    f"schema keyword {root_key!r} is not supported at the document root",
                    _pointer("", root_key),
                )
            )
        elif root_key in _IGNORED_KEYWORDS:
            diagnostics.extend(_ignored_keyword_diagnostics({root_key: data[root_key]}, ""))
        else:
            root_model_keys.append(root_key)
    if root_model_keys:
        # One rule, stated once: the root carries metadata, models live in a
        # definition section. Listing every root keyword separately said neither.
        rendered = ", ".join(repr(root_key) for root_key in root_model_keys)
        diagnostics.append(
            _diagnostic(
                "unsupported-root-schema",
                "models must be declared under '$defs' or 'definitions': the document root "
                f"is metadata-only, so its schema keywords ({rendered}) describe no model",
                "",
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

    # Canonical order, like every other diagnostic group here: parsed payloads
    # backdate only when their complete values are equal. Emitting in document
    # key order would make a key reorder observably different even when the
    # diagnostics themselves are otherwise unchanged.
    for name in sorted(unique_names):
        location = locations_by_name[name][0]
        diagnostics.extend(_definition_name_diagnostics(name, location))
        diagnostics.extend(_module_name_diagnostics(name, location))
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


def _pure_alias_target(payload: ModelPayload, names: frozenset[str]) -> str | None:
    """The single definition this alias renames, or None.

    Only an alias whose whole expression is another definition's name (bare, or
    with the rendered `| None` nullable spelling) forms an edge: recursion that
    passes through a container or object field names a real type and compiles.
    """
    if payload[1] != "alias":
        return None
    expression = payload[4]
    for ref in payload[6]:
        if ref not in names or ref == payload[0]:
            continue
        python_name = _python_identifier(ref)
        if expression in (python_name, f"{python_name} | None"):
            return ref
    return None


@query
def alias_cycle_diagnostics(db: Database, path: str) -> tuple[DiagnosticPayload, ...]:
    """Error diagnostics for pure alias cycles that span definitions.

    Each member of such a cycle would render as a module whose alias imports
    the next member, closing an import loop with no type in between; the
    single-definition case is already caught as `self-referential-alias`.
    """
    names = frozenset(definition_names(db, path))
    edges: dict[str, str] = {}
    for name in sorted(names):
        target = _pure_alias_target(definition_structure(db, path, name), names)
        if target is not None:
            edges[name] = target

    diagnostics: list[DiagnosticPayload] = []
    resolved: set[str] = set()
    for start in sorted(edges):
        if start in resolved:
            continue
        trail: list[str] = []
        seen_at: dict[str, int] = {}
        current = start
        while current in edges and current not in resolved and current not in seen_at:
            seen_at[current] = len(trail)
            trail.append(current)
            current = edges[current]
        if current in seen_at:
            cycle = trail[seen_at[current] :]
            anchor = min(cycle)
            rotation = cycle.index(anchor)
            ordered = cycle[rotation:] + cycle[:rotation]
            rendered = " -> ".join([*ordered, ordered[0]])
            for member in ordered:
                diagnostics.append(
                    _diagnostic(
                        "alias-cycle",
                        f"alias cycle resolves to no type: {rendered}",
                        definition_pointer(db, path, member),
                    )
                )
        resolved.update(trail)
    return tuple(diagnostics)


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
