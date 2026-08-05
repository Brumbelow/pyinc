# Release Assurance Record

`release/assurance.json` is an intentionally fail-closed launch record. It is
not a checklist that can be completed by assertion alone. The release workflow
accepts it only when it identifies the exact version and reviewed candidate
commit and links the evidence described below.

The release commit is the candidate's direct child and may change only this
assurance record. That narrow signed closure commit avoids an impossible
self-reference: a tracked file cannot contain the hash of the same commit that
contains it. All executable code, tests, documentation, version metadata, and
release notes are therefore already fixed in `candidate_commit`; the closure
records evidence without changing the reviewed candidate tree.

Two independent reviews are required: one by an incremental-computation
reviewer and one by a filesystem/security reviewer. Each entry names the human
reviewer, repeats the exact candidate commit, and identifies a published or
repository-resident scope, report, and counterexample corpus for that candidate.
Every evidence reference is an object containing `location` and the lowercase
SHA-256 of its exact bytes. The gate reads repository files or downloads HTTPS
evidence and verifies the bytes. Those bytes must then decode as one of the
strict JSON evidence artifacts below; a correctly hashed README, changelog,
placeholder, or other unrelated document cannot satisfy the gate.

Evidence artifacts use `schema_version: 1`, reject duplicate, missing, or
additional fields, and repeat the release `version` and exact
`candidate_commit`:

- `review-scope` repeats `discipline` and `reviewer`, records `recorded_utc`,
  and supplies a non-empty `method` plus a non-empty `scope` array.
- `review-report` repeats the same review identity, records `completed_utc`,
  requires `result: "pass"` and `open_p0_p1: 0`, and supplies a non-empty
  `summary` and `closure_evidence` array.
- `counterexample-corpus` repeats the same review identity, records
  `recorded_utc`, and names a non-empty, duplicate-free `cases` array. The
  scope and corpus timestamps cannot postdate the completed report.
- `soak-project` repeats the exact project name, records `started_utc` and
  `completed_utc`, requires `result: "pass"`, `p0_p1_failures: 0`, and a
  positive `runs` count, and describes the non-empty `environment` and
  `summary`.

All evidence timestamps are UTC, cannot predate the candidate commit, and
cannot be in the future.

The exact `candidate_commit` must then soak for at least 30 complete days in at
least three external real projects. The start must not predate the candidate's
Git commit time. Each project entry repeats the candidate commit and links its
run/failure record. Every individual project artifact must cover at least 30
complete days inside the declared soak interval; a collection of shorter,
non-overlapping project runs cannot be presented as one 30-day soak.
The record must state zero P0/P1 soak failures and zero remaining P0/P1 release
items. Public GitHub issues carrying either priority label are checked
separately by the release workflow.

The checked-in record remains `blocked` with null evidence until those external
events actually occur. Changing it to `ready` before then would make the file
false; leaving it blocked correctly prevents a tag from publishing.
