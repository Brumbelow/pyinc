from __future__ import annotations

from pathlib import Path

from pyfoundinc import Database
from pyfoundinc.integrations.python_source import file_analysis, file_analysis_payload

if __name__ == "__main__":
    sample = Path(__file__).with_name("sample_module.py")
    if not sample.exists():
        sample.write_text("import os\n", encoding="utf-8")
    db = Database(mode="strict")
    print(file_analysis(db, sample))
    print(db.explain(file_analysis_payload, str(sample)))
