from __future__ import annotations

from pyinc import Database, Input, query

NUMBER = Input[int]("number")


@query
def double(db: Database) -> int:
    return NUMBER.read(db) * 2


def main() -> None:
    db = Database()
    db.set(NUMBER, 3)
    print(f"initial result: {db.get(double)}")

    db.set(NUMBER, 5)
    stale = db.inspect(double)
    fresh = db.inspect_fresh(double)

    print(
        "inspect: "
        f"decision={stale.last_decision} verified_at={stale.verified_at} changed_at={stale.changed_at}"
    )
    print(
        "inspect_fresh: "
        f"decision={fresh.last_decision} verified_at={fresh.verified_at} changed_at={fresh.changed_at}"
    )


if __name__ == "__main__":
    main()
