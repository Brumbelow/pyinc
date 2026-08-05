from __future__ import annotations

from pyinc import Database, UnsupportedValueError, explain_query_captures, query


def main() -> None:
    box = {"value": 1}

    @query
    def read_box(db: Database) -> int:
        return box["value"]

    print("static capture preflight diagnostics:")
    for info in explain_query_captures(read_box):
        print(
            f"- {info.name}: accepted={info.accepted} "
            f"kind={info.kind} reason={info.rejection_reason or '<none>'}"
        )

    try:
        Database().get(read_box)
    except UnsupportedValueError as exc:
        print(f"runtime failure: {exc}")


if __name__ == "__main__":
    main()
