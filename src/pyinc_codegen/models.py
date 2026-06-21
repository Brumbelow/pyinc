"""Public frozen result types for ``pyinc_codegen`` plus their tuple payloads.

Following the pyinc integration pattern: kernel-cached query nodes pass
snapshot-safe *tuple* payloads; the frozen dataclasses below are decoded only at
the public boundary (``schema_analysis``). The ``*Payload`` aliases match each
dataclass's field order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

# (code, message)
DiagnosticPayload: TypeAlias = tuple[str, str]
# (name, type_expr, required, description)
FieldPayload: TypeAlias = tuple[str, str, bool, str]
# (name, kind, fields, enum_values, base_type, description, refs, diagnostics)
ModelPayload: TypeAlias = tuple[
    str,
    str,
    tuple[FieldPayload, ...],
    tuple[str, ...],
    str,
    str,
    tuple[str, ...],
    tuple[DiagnosticPayload, ...],
]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str


@dataclass(frozen=True)
class FieldModel:
    name: str
    type_expr: str
    required: bool
    description: str


@dataclass(frozen=True)
class SchemaModel:
    """One generated model: an ``object`` (dataclass), an ``enum`` (Literal
    alias), or an ``alias`` (type alias)."""

    name: str
    kind: str  # "object" | "enum" | "alias"
    fields: tuple[FieldModel, ...]
    enum_values: tuple[str, ...]
    base_type: str
    description: str
    refs: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class SchemaAnalysis:
    path: str
    models: tuple[SchemaModel, ...]
    diagnostics: tuple[Diagnostic, ...]


def _decode_diagnostic(payload: DiagnosticPayload) -> Diagnostic:
    code, message = payload
    return Diagnostic(code=code, message=message)


def _decode_field(payload: FieldPayload) -> FieldModel:
    name, type_expr, required, description = payload
    return FieldModel(name=name, type_expr=type_expr, required=required, description=description)


def _decode_model(payload: ModelPayload) -> SchemaModel:
    name, kind, fields, enum_values, base_type, description, refs, diagnostics = payload
    return SchemaModel(
        name=name,
        kind=kind,
        fields=tuple(_decode_field(item) for item in fields),
        enum_values=enum_values,
        base_type=base_type,
        description=description,
        refs=refs,
        diagnostics=tuple(_decode_diagnostic(item) for item in diagnostics),
    )
