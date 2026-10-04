"""``pyinc_codegen``: a JSON Schema to typed Python compiler.

The first useful file-to-file compiler built on pyinc. It uses only pyinc's
public API (the ``pyinc`` top level: ``@query``, ``BinaryFileResource`` and the
``@action`` output layer) and stays out of kernel internals. It needs only the
standard library: it parses JSON Schema with ``json`` and walks the dicts.

See ``docs/codegen-guide.md`` for the supported subset and the public-API-only
boundary.
"""

from .codegen import generate, generate_outputs, schema_analysis
from .models import (
    Diagnostic,
    DiagnosticSeverity,
    FieldModel,
    SchemaAnalysis,
    SchemaGenerationError,
    SchemaModel,
)

__all__ = [
    "Diagnostic",
    "DiagnosticSeverity",
    "FieldModel",
    "SchemaAnalysis",
    "SchemaGenerationError",
    "SchemaModel",
    "generate",
    "generate_outputs",
    "schema_analysis",
]
