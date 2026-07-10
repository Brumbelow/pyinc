"""``pyinc_codegen`` — a JSON-Schema -> typed-Python compiler.

The first useful file->file compiler built on pyinc. It consumes pyinc's PUBLIC
API only (``pyinc`` top-level: ``@query``, ``BinaryFileResource``, the
``@action`` output layer) and never reaches into kernel internals. Stdlib-only:
JSON Schema is parsed with ``json`` plus dict walking — no third-party schema
library.

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
