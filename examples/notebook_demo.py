from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pyinc import Database
from pyinc.integrations import notebook_analysis

# `notebook_analysis_payload` is a module-local helper rather than part of the
# package's stable surface: it is deliberately absent from
# `pyinc.integrations.__all__`, and the integration contract's "Composition and
# experimental helpers" section places such names outside that contract — an
# experimental helper, by that section's own name. It is imported here because
# `db.inspect` takes a query, and the public entrypoint
# `notebook_analysis` is a plain function.
from pyinc.integrations.notebook import notebook_analysis_payload


def _notebook(cells: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "nbformat": 4,
        "nbformat_minor": 5,
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python"},
        },
        "cells": cells,
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def main() -> None:
    db = Database()

    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "scratch.ipynb"
        nb = _notebook(
            [
                {
                    "cell_type": "markdown",
                    "source": "# Daily ETL\n\nQuick exploration of the input table.",
                },
                {
                    "cell_type": "code",
                    "source": "import pandas as pd\n\ndef load(path):\n    return pd.read_csv(path)\n",
                    "outputs": [],
                    "execution_count": None,
                },
            ]
        )
        _write(path, nb)

        first = notebook_analysis(db, str(path))
        print(f"kernel_name={first.kernel_name}")
        print(f"language={first.language}")
        print(f"cell_count={len(first.cells)}")
        for cell in first.cells:
            imports = tuple(imp.module for imp in cell.imports)
            defs = tuple(d.name for d in cell.definitions)
            print(
                f"cell {cell.index}: type={cell.cell_type} "
                f"heading={cell.heading!r} imports={imports} definitions={defs}"
            )

        first_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

        # Re-running the notebook produces new outputs and execution_count
        # entries but leaves cell sources unchanged. The parsed payloads read
        # neither field, so each of them lands an equal value and is backdated,
        # and downstream consumers stay valid.
        nb["cells"][1]["outputs"] = [
            {"output_type": "stream", "name": "stdout", "text": "loaded 1024 rows\n"}
        ]
        nb["cells"][1]["execution_count"] = 3
        _write(path, nb)

        second = notebook_analysis(db, str(path))
        second_changed = db.inspect(notebook_analysis_payload, str(path)).changed_at

        print(f"output_only_edit_backdated={second_changed == first_changed}")
        print(f"analysis_unchanged={first == second}")


if __name__ == "__main__":
    main()
