from __future__ import annotations

from pyinc import Database, Input, QueryChangeEvent, query

COUNT = Input[int]("count")


@query
def doubled(db: Database) -> int:
    return COUNT.read(db) * 2


def main() -> None:
    db = Database()
    db.set(COUNT, 1)

    events: list[QueryChangeEvent] = []
    subscription = db.observe(events.append, doubled)

    db.get(doubled)  # cold execute, no prior value to change
    db.get(doubled)  # reused, no event
    db.set(COUNT, 1)  # equal input, no revision bump
    db.get(doubled)  # reused, no event

    db.set(COUNT, 7)
    db.get(doubled)  # executed, value moved 2 → 14, fires

    db.set(COUNT, 1)
    db.get(doubled)  # executed, value moved 14 → 2, fires

    subscription.unsubscribe()
    db.set(COUNT, 99)
    db.get(doubled)  # executed but unsubscribed, no event

    print(f"event_count={len(events)}")
    for evt in events:
        print(f"event: decision={evt.decision} changed_at={evt.changed_at}")

    print(f"final_decision={db.inspect(doubled).last_decision}")


if __name__ == "__main__":
    main()
