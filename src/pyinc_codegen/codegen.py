"""High-level codegen: the ``@action`` that emits files and the public entrypoints.

Output ownership (for precise orphan deletion): each definition ``D`` owns
``<snake(D)>.py`` and ``docs/<snake(D)>.md``; the aggregate index owns
``__init__.py``.
"""

from __future__ import annotations

import os

from pyinc import Database, Output, ReconcileResult, action

from .models import (
    SchemaAnalysis,
    SchemaGenerationError,
    _decode_diagnostic,
    _decode_model,
)
from .schema import (
    _snake,
    definition_model,
    definition_names,
    document_diagnostics,
    index_init,
    model_doc,
    model_python,
)


@action(tool="pyinc-codegen")
def generate_outputs(db: Database, schema_path: str) -> list[Output]:
    """Desired output set: one model + one doc per definition, plus the index."""
    analysis = schema_analysis(db, schema_path)
    if analysis.errors:
        raise SchemaGenerationError(analysis)
    outputs: list[Output] = []
    for name in definition_names(db, schema_path):
        module = _snake(name)
        outputs.append(Output.text(f"{module}.py", model_python(db, schema_path, name)))
        outputs.append(Output.text(f"docs/{module}.md", model_doc(db, schema_path, name)))
    outputs.append(Output.text("__init__.py", index_init(db, schema_path)))
    return outputs


def generate(
    db: Database, schema_path: str | os.PathLike[str], out_dir: str | os.PathLike[str]
) -> ReconcileResult:
    """Generate typed Python models from ``schema_path`` into ``out_dir``,
    reconciling outputs incrementally (only changed files are written).

    Schema validation is completed before the action reads its ownership
    manifest or mutates the output tree. Existing generated files are therefore
    preserved when the new schema is malformed or unsupported.
    """
    path = os.fspath(schema_path)
    analysis = schema_analysis(db, path)
    if analysis.errors:
        raise SchemaGenerationError(analysis)
    return generate_outputs.reconcile(db, path, root=out_dir)


def schema_analysis(db: Database, schema_path: str | os.PathLike[str]) -> SchemaAnalysis:
    """Decode the per-definition models for inspection (non-generating)."""
    path = os.fspath(schema_path)
    models = tuple(
        _decode_model(definition_model(db, path, name)) for name in definition_names(db, path)
    )
    diagnostics = tuple(
        _decode_diagnostic(item) for item in document_diagnostics(db, path)
    ) + tuple(diagnostic for model in models for diagnostic in model.diagnostics)
    return SchemaAnalysis(path=path, models=models, diagnostics=diagnostics)
