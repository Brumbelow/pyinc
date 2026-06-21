# Plan: Actions, Generators, and Benchmarks

Status: **approved, in progress.** This is the formal phased plan mandated by the
engagement brief. Phase 1 (the keystone action layer) is the first implementation
phase; Phases 2–4 build on it.

---

## 1. Existing-implementation architecture summary

`pyinc` (v2.5.0) is a correctness-first, pure-Python, stdlib-only incremental
computation kernel. Two packages ship in one wheel:

- **`src/pyinc/`** — the stable kernel + `pyinc.integrations`. Zero runtime
  dependencies. Carries the semver contract (`docs/kernel-contract.md`,
  `docs/integration-contract.md`).
- **`src/pyinc_tools/`** — consumer tooling (CLI, LSP, watcher). Builds only on the
  stable `pyinc.integrations` surface; must not widen the kernel contract.

Core concepts: `@query` functions are memoized nodes; `Input`/`Resource` are leaf
nodes; `Database.get` evaluates with a red-green verification algorithm; equal
recomputation **backdates** (early cutoff) so downstream stays green. All values
crossing cached boundaries are **snapshot-safe** (frozen via `freeze`/`thaw`).
Ambient reads are intercepted (`UntrackedReadError`) unless routed through a
`Resource`. **No side effects occur during query evaluation.** Modes
`strict`/`checked`/`fast` govern only boundary exposure + mutation detection.

Reusable building blocks the new work leans on:

- `pyinc.store.FileSystemArtifactStore` — atomic `tempfile`+`os.replace` writes with
  `objects/<d[:2]>/<d[2:]>` fan-out and collision detection (template for the action
  writer).
- `pyinc.value` — `freeze`, `thaw`, `semantic_equal`, `serialize_snapshot`,
  `fingerprint_snapshot` (SHA-256, versioned).
- Three-layer integration pattern (`docs/integration-authoring.md`): tuple payload
  queries → composition queries → frozen-dataclass entrypoints.
- `Resource.probe/load` (`FileResource`/`DirectoryResource` use SHA-256 probes).
- Contract-lock `__all__` tests; property-based from-scratch-consistency tests;
  `tests/test_examples.py` executes examples.

## 2. Phases (one per brief item)

- **Phase 1 — Action and declared-output model** (keystone).
- **Phase 2 — GraphQL introspection-driven incremental generator** (validates the model).
- **Phase 3 — Security detection-content compiler** (flagship file-to-file vertical).
- **Phase 4 — Benchmark and correctness harness** (no perf claim without it).

## 3. Package / module / file layout

```
src/pyinc/__init__.py          # + additive `is_query_active`
src/pyinc/actions/             # NEW — pyinc.actions namespace, own __all__
  __init__.py errors.py paths.py artifacts.py manifest.py reconciler.py
src/pyinc/integrations/graphql_schema.py     # Phase 2
src/pyinc/integrations/detection_rules.py    # Phase 3
src/pyinc/integrations/__init__.py           # + stable re-exports (Phases 2/3)
docs/action-contract.md                      # Phase 1 (cross-linked)
docs/plans/2026-06-20-actions-generators-benchmarks.md   # this file
examples/action_reconcile_demo.py            # Phase 1
examples/graphql_codegen_demo.py             # Phase 2
examples/detection_compile_demo.py           # Phase 3
tests/test_actions.py tests/test_graphql_schema.py
tests/test_detection_rules.py tests/test_bench.py
tests/fixtures/graphql/  tests/fixtures/detection/
bench/ (__init__.py run.py scenarios.py adapters.py report.py results/)  # Phase 4
pyproject.toml                 # + [project.optional-dependencies] bench = ["joblib"]
```

## 4. Public API sketches (Phase 1; ownership boundaries)

All `@dataclass(frozen=True)`, snapshot-safe. See `docs/action-contract.md` for
semantics.

```python
digest_bytes(content: bytes) -> str                      # SHA-256 hex
ToolIdentity(name, version, schema_version=1, executable_digest=None, config_digest=None)
ActionIdentity(action_id, output_root, tool)
DesiredArtifact(path, content, metadata=())              # path normalized; digest derived
DesiredArtifactSet(action, artifacts)                    # dedup + sorted on construction
ActionPlan(creates, updates, deletes, unchanged)         # sorted relpath tuples
ActionResult(writes, deletions, unchanged, digests)
ActionManifest(action_id, output_root, tool, entries, manifest_version=1)

class FilesystemReconciler:
    def __init__(self, output_root, *, state_dir): ...
    def plan(self, desired) -> ActionPlan         # read-only; no writes
    def apply(self, desired) -> ActionResult      # rejected if is_query_active()
default_state_dir(output_root, action_id) -> Path # <root>/../.pyinc-actions/<id>
```

Ownership boundary: queries own *values* (desired artifacts); the reconciler owns
*the filesystem effect* and the manifest. The two never overlap — `apply()` is
refused inside a query via `pyinc.is_query_active()` (additive, read-only).

Generators (Phases 2/3) follow the three-layer shape: payload queries return tuple
payloads of `(relpath, bytes)`; a non-`@query` entrypoint builds a
`DesiredArtifactSet`; the caller reconciles outside queries.

## 5. Query graphs

**GraphQL (Phase 2):**
```
schema.json bytes (FileResource, cutoff: parsed-JSON)
  → decoded introspection payload
  → per-type normalized fragment  ──code-shape cutoff──→ per-type client/model artifact
                                  └─doc-shape cutoff───→ per-type doc artifact
  → root query/mutation fields → operation/probe artifacts
  → aggregate index artifacts (depend on per-type fragments)
  → DesiredArtifactSet → reconcile
```

**Detection (Phase 3):**
```
rule files, mappings, macros, attack, suppressions, backends, tests (FileResources)
  → normalized rule fragments
  → resolved macro/mapping deps (a rule depends only on what it references)
  → detection IR
  → per-backend render model → rendered query artifact (per rule × backend)
  → rule-test results → per-rule coverage fragment
  → aggregate bundle / coverage matrix / docs index
  → DesiredArtifactSet → reconcile
```

## 6. State / manifest format (action layer)

State directory (outside output root): `manifest.json` + `lock`.
`manifest.json` is canonical JSON (sorted keys, sorted entries, compact
separators, trailing newline):

```json
{"action_id":"...","entries":[["a.py","<sha256>"],["b.py","<sha256>"]],
 "manifest_version":1,"output_root":"...","tool":{"config_digest":null,
 "executable_digest":null,"name":"...","schema_version":1,"version":"..."}}
```

`lock` is an `O_EXCL`-created file containing `pid=<n>`; presence = held.

## 7. Exact acceptance tests per phase

- **Phase 1** (`tests/test_actions.py`): initial create; identical-rerun zero-write +
  stable mtime; one-changed-input rewrites only affected; tampered output repaired;
  removed declaration deletes only owned stale; foreign preserved; dry-run plan correct
  + changes nothing; duplicate claim fails; absolute/`..`/empty/symlink-escape rejected;
  injected write failure cleans temps + no manifest + no premature deletion; deterministic
  manifest bytes; apply-in-query rejected; concurrent writer rejected; property edit-sequence
  vs from-scratch (tree + owned set).
- **Phase 2** (`tests/test_graphql_schema.py`): cold gen file set; identical rerun zero
  writes; whitespace/key-order zero writes; description-only regenerates only that doc
  artifact (+ unavoidable doc aggregate); one field-signature edit regenerates exactly the
  documented dependents; type/operation removal deletes only owned artifacts; generated
  Python parses (`ast.parse`); incremental == from-scratch over an edit sequence;
  malformed introspection → deterministic diagnostics; contract-lock `__all__`.
- **Phase 3** (`tests/test_detection_rules.py`): cold gen (backends/bundle/matrix/docs/test
  results); identical rerun zero writes; unused-mapping edit zero writes; used-mapping edit
  regenerates exactly affected + unavoidable aggregates; rule removal deletes only owned
  outputs; provenance names rule + mapping/macro + backend; malformed/unsupported →
  deterministic diagnostics; incremental == from-scratch over edit sequence; contract-lock.
- **Phase 4** (`tests/test_bench.py`): adapter correctness-gate smoke; CSV→Markdown report
  generation; `N/A` handling — **no timing assertions**.

## 8. Backward-compatibility & semver

All additive → minor bump (target 2.6.0). New: `pyinc.actions` namespace,
`pyinc.is_query_active`, new integration re-exports, `bench` optional-dep group. No
existing signature/behavior changes. Top-level `pyinc.__all__` is membership-checked
only (safe to extend); `integrations.__all__` exact-set lock
(`tests/test_python_source.py`) updated additively in Phases 2/3. If a phase hits a
genuine invariant conflict, stop and report rather than weaken.

## 9. Documentation changes per phase

- P1: new `docs/action-contract.md` (cross-linked from `kernel-contract.md` + README);
  `kernel-contract.md` actions-outside-evaluation note; `integration-authoring.md`
  file-generating-integration section; `architecture.md` action-layer section; README
  Actions section; CHANGELOG.
- P2/P3: `integration-contract.md` per-integration surface + supported-feature/limitation
  docs; README integration list; examples; CHANGELOG.
- P4: `bench/README` + generated report docs; CHANGELOG.

## 10. Risks / unresolved / deferred

- Cross-platform atomicity + symlink-escape edge cases — covered by targeted +
  fault-injection tests.
- `joblib.Memory` models only arg-based scenarios; others `N/A`.
- Deferred / out of scope: real Sigma/YARA/SIEM compliance, network fetching, general
  template engine, subprocess execution, repo-intelligence/LSP/watcher features.
- Open: whether `bench/` ships in the wheel (default: sdist + repo only).

## 11. Expected commits (one per phase)

0. `docs: add phased plan for actions/generators/benchmarks`
1. `feat(actions): declared-output action and reconciliation layer`
2. `feat(integrations): GraphQL introspection-driven incremental generator`
3. `feat(integrations): detection-content compiler`
4. `feat(bench): reproducible benchmark + correctness harness`

## 12. Baseline gate results + git status

Environment: Python 3.13.12, Linux (WSL2). Recorded on the clean tree before Phase 1.

- `git status`: clean working tree, branch `main`, up to date with origin; HEAD `63358f9`.
- `python -m mypy src tests`: **Success: no issues found in 47 source files.**
- `python -m ruff check src tests`: **All checks passed!**
- `python -m pytest -q`: **all green.** The pre-change suite is **1169 passed**;
  after Phase 1's additive tests the full suite is **1194 passed** (25 added:
  `tests/test_actions.py` + one `tests/test_examples.py` case). No existing test was
  modified, skipped, or weakened.

## 13. Dependency diagram

```
Phase 0 (this plan + baselines)
        │
Phase 1 (action / reconciliation layer)   ← keystone
      ╱        ╲
Phase 2          Phase 3
(GraphQL gen)  (detection compiler)
      ╲        ╱
       Phase 4
   (benchmark + correctness)
```

---

## Answers to the brief's explicit design questions

1. **Where does the action layer belong?** In the `pyinc` wheel as `pyinc.actions`,
   not `pyinc_tools` or a separate wheel — the new integrations must import it, which
   forbids inverting the kernel→tools dependency. Additive; separate contract doc.
2. **Additive without touching evaluation?** Yes. Queries return snapshot-safe desired
   artifacts; the reconciler is a pure consumer. The one kernel touch is the read-only
   additive `is_query_active()`.
3. **Ownership & safe stale deletion?** Per-action JSON manifest of owned
   `(path, digest)`; delete only previously-owned, in-root, realpath-contained regular
   files absent from the new set, and only after all writes commit.
4. **Crash/failure semantics (multi-file)?** Per-file atomic (`tmp`+`os.replace`); temps
   cleaned on failure; manifest published last; failed run deletes nothing and publishes
   nothing; next run converges from on-disk content digests. Not transactional across
   files — and says so.
5. **Concurrency?** Portable `O_EXCL` lock file; concurrent run rejected with
   `ActionLockError`; manifest writes atomic → no silent corruption.
6. **GraphQL semantic-for-code vs docs?** Code artifacts use a code-shape cutoff
   (kind/field/arg/nullability/return/enum/input), invariant to `description`; doc
   artifacts use a doc-shape cutoff that includes descriptions.
7. **Detection grammar?** Small JSON format (TOML accepted read-only). Leaf
   `{field, op, value}` with ops `equals/not_equals/in/contains/startswith/endswith/
   exists/gt/gte/lt/lte/regex`; combinators `all/any/not`; `{macro}`; field mappings.
   Unknown constructs → explicit diagnostics. Not Sigma/YARA-compliant.
8. **Aggregates when one constituent changes?** Aggregates are separate nodes over
   per-constituent normalized payloads; they legitimately rewrite when their bytes
   change. Tests assert the exact artifact set and separate unavoidable-aggregate
   rewrites from unrelated per-constituent rewrites.
9. **Honest benchmarks?** Every timed incremental result is compared byte-for-byte to a
   fresh cache-free `Database` run; no timing without a passing correctness assertion;
   baselines' capability differences documented; unsupported scenarios `N/A`; no
   universal speed claims.
