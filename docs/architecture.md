# pyfoundinc Architecture

## Kernel

`pyfoundinc` is a pull-based incremental query runtime. The database stores query memos, inputs, and resources as revisioned node records with:

- a stable node key
- a frozen boundary snapshot
- a semantic digest
- dependency edges captured dynamically at runtime
- `changed_at` and `verified_at` revisions
- the last decision: `executed`, `reused`, or `backdated`

Evaluation is top-down. `db.get()` verifies dependencies first, then either reuses the memo or re-executes the query. If a re-executed query returns a semantically equal value, the record is backdated so downstream nodes stay clean.

## Value Membrane

Values crossing cached boundaries are frozen snapshots.

- `strict`: expose frozen values directly.
- `checked`: expose thawed copies and verify that queries did not mutate them.
- `fast`: expose thawed copies without mutation checks.

Hidden reads are not allowed in the core. Raw `open()` inside a query raises `UntrackedReadError` unless the access is routed through a resource object or explicitly marked via `db.report_untracked_read(...)`.

## Scope

Version 1 targets:

- module-defined `@query` functions
- explicit `Input` leaves
- explicit file, env, and directory resources
- explanation/provenance for reuse vs recompute

Version 1 does not include:

- notebook integration
- push observers
- schedulers or worker pools
- content-addressed artifact storage
- arbitrary mutable object graphs across cached boundaries
