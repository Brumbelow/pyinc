from __future__ import annotations

import time

from pyinc import Database, query


@query
def read_clock(db: Database) -> int:
    db.report_untracked_read("time.monotonic_ns()")
    return time.monotonic_ns()


def main() -> None:
    db = Database()
    first = db.get(read_clock)
    second = db.get(read_clock)
    node = db.inspect(read_clock)

    print(f"first={first}")
    print(f"second={second}")
    print(f"last_decision={node.last_decision}")
    print(f"untracked_reasons={node.untracked_reasons}")


if __name__ == "__main__":
    main()
