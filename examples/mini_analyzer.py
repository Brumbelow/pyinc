from __future__ import annotations

import tempfile
from pathlib import Path

from pyinc import Database
from pyinc.integrations.python_source import (
    file_analysis,
    workspace_analysis,
)


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        sample = root / "app.py"
        sample.write_text(
            "import json\nfrom helper import answer\n\ndef render():\n    return answer\n",
            encoding="utf-8",
        )
        (root / "helper.py").write_text("answer = 42\n", encoding="utf-8")

        db = Database(mode="strict")
        file_result = file_analysis(db, str(sample))
        workspace_result = workspace_analysis(db, str(root))

        print(f"file={Path(file_result.path).name}")
        print(f"imports={tuple(item.module for item in file_result.imports)}")
        print(f"definitions={tuple(item.name for item in file_result.definitions)}")
        print(f"workspace_modules={tuple(item.module for item in workspace_result.modules)}")
        print(f"diagnostics={sum(len(item.diagnostics) for item in workspace_result.modules)}")


if __name__ == "__main__":
    main()
