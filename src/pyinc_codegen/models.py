"""Public result types for :mod:`pyinc_codegen` and cached tuple payloads."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias


class DiagnosticSeverity(StrEnum):
    """Severity of a schema diagnostic."""

    ERROR = "error"
    WARNING = "warning"


# (code, message, severity, JSON Pointer)
DiagnosticPayload: TypeAlias = tuple[str, str, str, str]
# (name, type_expr, required, description, type expression already allows None)
FieldPayload: TypeAlias = tuple[str, str, bool, str, bool]
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
    """A problem found while analysing a schema.

    ``json_pointer`` is an RFC 6901 pointer into the source document. The empty
    string identifies the whole document.
    """

    code: str
    message: str
    severity: DiagnosticSeverity
    json_pointer: str


@dataclass(frozen=True)
class FieldModel:
    name: str
    type_expr: str
    required: bool
    description: str


@dataclass(frozen=True)
class SchemaModel:
    """One analysed model: an object, enum, or alias."""

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

    @property
    def errors(self) -> tuple[Diagnostic, ...]:
        """Diagnostics that prevent generation."""

        return tuple(
            diagnostic
            for diagnostic in self.diagnostics
            if diagnostic.severity is DiagnosticSeverity.ERROR
        )


class SchemaGenerationError(ValueError):
    """Raised when error diagnostics make generation unsafe.

    The complete non-generating analysis remains available on ``analysis`` so
    callers can present structured diagnostics without parsing this exception's
    message.
    """

    def __init__(self, analysis: SchemaAnalysis) -> None:
        self.analysis = analysis
        self.diagnostics = analysis.errors
        count = len(self.diagnostics)
        noun = "error" if count == 1 else "errors"
        details = "; ".join(
            f"{diagnostic.json_pointer or '/'} [{diagnostic.code}] {diagnostic.message}"
            for diagnostic in self.diagnostics
        )
        message = f"Cannot generate from {analysis.path!r}: {count} schema {noun}"
        if details:
            message = f"{message}: {details}"
        super().__init__(message)


def _decode_diagnostic(payload: DiagnosticPayload) -> Diagnostic:
    code, message, severity, json_pointer = payload
    return Diagnostic(
        code=code,
        message=message,
        severity=DiagnosticSeverity(severity),
        json_pointer=json_pointer,
    )


def _decode_field(payload: FieldPayload) -> FieldModel:
    name, type_expr, required, description, _allows_none = payload
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
