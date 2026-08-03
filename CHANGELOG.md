# Changelog

All notable changes to this project will be documented in this file. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `Database.request_span()`, a public context manager holding one kernel
  request open across several `get`/`inspect`/`inspect_fresh`/`read_resource`
  calls, and `Database.request_inputs_changed()` to declare mid-span input
  changes. Entering a span declares that the world the database reads from
  is stable until the span closes; `set`/`set_many` declare their own
  changes, a change committed by any thread rolls the span, buffered
  observer events are delivered when the outermost span closes even if the
  span body raises, and a failing load's exception lives to span end.
  `pyinc.integrations.request_inputs_changed()` rolls a held span, and
  `WorkspaceSession` holds a span for each public method.

- `pyinc-tools analyze` can report diagnostics and gate a CI job: `--format
  text` prints one `path:line:col: severity code message` line per diagnostic,
  `--diagnostics-only` emits just the diagnostics array instead of the full
  result, and `--fail-on` exits with status `3` when a diagnostic reaches the
  given severity. Diagnostics are sorted by location so output is stable, and
  the report is always printed before the exit status is decided.
- PEP 440 arbitrary equality (`===`) is evaluated in version specifiers,
  requirement evaluation, and dependency checking. It compares the version
  exactly as written — no normalization, padding, or case folding — so it is
  decided without parsing and works against versions that do not conform to
  PEP 440.
- Semantic tokens classify a `from ... import ...` use by the workspace
  declaration it resolves to, instead of leaving it unstyled. Imports that
  resolve outside the workspace, or ambiguously, remain unclassified.
- A resource whose `load` or `probe_and_load` raises now gets a *failure
  record* carrying the probe observed alongside the failure, and the reading
  query records its dependency edge on that node before the exception
  propagates. A later `get()` therefore re-checks the resource instead of
  treating the reader as dependency-free. `inspect()` and `explain()` show the
  node with decision `failed` and a reason naming the exception, and it counts
  in `DatabaseStatistics.resource_count`. An unchanged failing probe does not
  move the revision, so a query that handled the failure stays green across
  requests; a changed probe, or a transition between success and failure in
  either direction, invalidates its readers. Behaviour is identical in
  `strict`, `checked`, and `fast`.
- `ClassModel.truncated_bases` names every base that resolved to a workspace
  class but sat past the `MAX_BASE_DEPTH` cap and so was not walked, reported
  as written at the stopped edge and deduplicated in first-encounter order.
  Members inherited eight or more levels above a class are still omitted, but
  no longer silently.
- Code generation compiles four constructs that were previously
  `unsupported-construct` errors: `const` and inline `enum` render as
  `typing.Literal[...]`, schema-valued `additionalProperties` in property
  position renders as `dict[str, T]` (recursively, and a referenced definition
  joins the model's reference graph), and the two combinator spellings that
  name exactly one type — single-branch `{"allOf": [S]}` and
  `{"anyOf": [S, {"type": "null"}]}` in either branch order — render as `S` and
  as `S` made optional. `Literal` is imported only when a rendered type uses
  it. Multi-branch composition remains an error.
- Code generation accepts annotation- and validation-only keywords wherever a
  schema node is accepted — `format`, `pattern`, `minimum`, `maximum`,
  `exclusiveMinimum`, `exclusiveMaximum`, `multipleOf`, `minLength`,
  `maxLength`, `minItems`, `maxItems`, `uniqueItems`, `deprecated`, `readOnly`,
  `writeOnly`, boolean `additionalProperties`, `examples`, and `default` — with
  a non-blocking `ignored-constraint` warning naming the keyword. Previously a
  single `"format": "email"` made an entire document fatal. A JSON `default`
  still does not become a dataclass default.
- New code-generation diagnostic codes: `ignored-constraint` (warning),
  `invalid-constraint` (error), `unconstrained-object-model` (warning),
  `unsupported-const-value` (error), `const-type-mismatch` (error),
  `unsupported-tuple-items` (error), and `self-referential-alias` (error).
- Notebook code cells that fail to parse as Python are re-parsed after
  neutralizing IPython syntax: line magics, shell escapes, help forms, and
  capture assignments are replaced by equal-width placeholders, so the rest of
  the cell is still analyzed and every range still names its real notebook line
  and column. A first-line cell magic claims the whole cell, and its body is
  dropped unless the magic runs that body as Python. Recognition is lexical and
  only at the start of a logical line, so magic-shaped lines inside string
  literals or bracketed continuations are left alone.
- New notebook diagnostic code `notebook-non-python-cell`, for a cell that
  still does not parse after neutralization; it carries a source range like
  `syntax-error` does.
- Nesting caps for the three structured-configuration integrations, each
  reported as an ordinary diagnostic rather than an exception: XML at 256
  element levels, JSON at 200 object/array levels, and TOML at 100 container
  levels.
- A workspace demo page, `docs/demo.md`: recordings of `pyinc-tools` watching
  a pinned checkout of pytest, linked from the README. The clips ship as
  1.8 MB of media in the sdist, and neither the page nor the README carries
  measured timings — the recordings stand on their own.

### Changed

- The default backdate decision in `checked` and `fast` now matches `strict`
  in three corners it previously diverged on: a recomputed dataclass whose
  type name (its qualified name) changed while its fields stayed equal, and a
  dataclass replaced by a dict of the same shape, both count as changes in
  every mode (previously
  backdated in `checked`/`fast` because thawing dropped the type identity),
  and default comparisons no longer invoke `ValueAdapter` `thaw`/`freeze`
  hooks in any mode. Queries with an `eq=` or `cutoff=` policy are
  unaffected.
- `pyinc-tools analyze --fail-on` combined with `--watch` is rejected as a
  usage error (exit status `2`), because watch mode never terminates normally.
  The default remains `--fail-on none`, so existing invocations keep their
  previous output and exit status.
- `===` clauses now evaluate instead of reporting `ambiguous`.
- Documented three previously unstated limits: cyclic-graph support covers
  mutable containers only, so values crossing through a `ValueAdapter`, `tuple`,
  or `frozenset` cannot be the target of a back-edge; CSV dialect and header
  sniffing inspect only the first 8192 characters; and re-export and base-class
  following both stop at depth 8. Re-export following reports `ambiguous`
  through `follow_depth`/`trail`; base-class following now names the bases it
  stopped at in `ClassModel.truncated_bases`.
- Inheritance flattening resolves a member name to the definition at the
  shortest inheritance distance from the starting class, rather than to
  whichever definition the depth-first walk reached first. Ties at equal
  distance still go to the earlier depth-first left-to-right arrival, and a
  class reached again at a strictly shallower distance is re-walked with the
  larger remaining budget. Every flattened `ClassMember` field —
  `defining_path`, `defining_class`, `range`, `annotation`, `signature` — is
  now fixed by the inheritance graph, base declaration order, and the depth
  cap, never by traversal order. This changes which definition wins where a
  base and a derived class both declare a name: for `class A3: m`,
  `class A2(A3)`, `class A1(A2)`, `class B1: m`, `class D(A1, B1)`,
  `class_model(D)` previously resolved `m` to `A3` and now resolves it to
  `B1`. The rule is not C3, so it can differ from CPython's MRO, which
  resolves that example to `A3`; this is stated as a limit in
  `docs/integration-contract.md`.
- A failing resource costs one load per request rather than one per reader.
  The first read in a request re-runs the load; reads that follow it within
  that request re-raise the exception that load produced. The retained
  exception and its traceback are dropped when the request ends, so a
  permanently failing node no longer pins the load frame or anything it
  allocated.
- A failure record, and every record that transitively depends on it, is
  omitted from a checkpoint and re-executes against live state after
  `load_checkpoint`. The same exclusion covers a failure the kernel could not
  record: the resource record an unprobeable raise contradicted, and the
  reader that consumed such a raise, plus everything above it.
- A load that raised is not counted as a `resource_load`, and re-running a
  load on an unchanged failing probe is not counted as a `resource_probe_hit`.
- The XML element walk, the JSON section walk, and the TOML section walk are
  iterative. Payloads and their order are unchanged.
- Configuration nesting past a cap is rejected rather than analyzed. XML at
  257 element levels, JSON at 201 container levels, and TOML at 101 container
  levels (the implicit root table is the first level and each array-of-tables
  header costs two, so 50 nested `[[…]]` headers cross the cap) now yield an
  empty payload and one diagnostic naming the limit. Below each cap, payloads
  and cutoff tokens are byte-identical to the previous release. The caps keep
  every accepted document within the snapshot depth the value layer supports;
  TOML's is half of JSON's because its cutoff encoding spends two snapshot
  levels per table.
- The JSON integration's pre-parse depth scan moves its query fingerprints.
- Code generation selects a schema node's shape by one precedence everywhere —
  `$ref`, then `allOf`/`anyOf`, then `enum`, then `const`, then `type`.
- Root-schema violations are reported as one `unsupported-root-schema` error at
  the document root naming every keyword collected, instead of one error per
  keyword, and the message states the rule that models must be declared under
  `$defs` or `definitions`. An ignored keyword at the root no longer blocks
  generation at all.
- A definition that declares `type: object` with no `properties` records a
  non-blocking `unconstrained-object-model` warning; the emitted `.py` is
  unchanged, byte for byte. The warning is keyed on the absence of the keyword,
  so `{"type": "object", "properties": {}}` stays silent.
- Malformed ignored-keyword values are `invalid-constraint` errors naming the
  expected shape, the draft-07 tuple form of `items` is `unsupported-tuple-items`
  rather than `invalid-schema-node`, `unconstrained-array-items` is no longer
  reported when `prefixItems` is present, and rejection messages for unsupported
  combinators name the specific rule instead of accompanying a generic
  `unconstrained-schema` warning.
- Cells whose only problem was IPython syntax report their imports and
  definitions with no diagnostic, where they previously reported one
  `syntax-error` and nothing else. A cell mixing notebook syntax with genuinely
  broken Python is reported as `notebook-non-python-cell`.
- `docs/kernel-contract.md` gained a "Failing Resource Loads" section, and its
  "Explicit Limitations" now names three ambient-read gaps individually: file
  metadata (`os.stat`, `Path.exists`, `Path.is_file`, `os.path.getsize` and
  `getmtime`), the byte-oriented environment (`os.getenvb`, `os.environb`), and
  the working directory (`os.getcwd`, `Path.cwd`). These are not intercepted;
  route them through `FileStatResource` or `db.report_untracked_read`.
- The durable checkpoint manifest version is `5`. A manifest written by 3.0.0
  records version `4` and `load_checkpoint` now rejects it loudly. The record
  layout is identical either way, so nothing else would have caught the
  difference: 3.0.0 recorded no dependencies for a query whose resource read
  raised a caught exception, and such a record warms under this release
  reporting "dependencies unchanged" while a fresh database re-derives it from
  the resource. Re-save affected checkpoints.
- `db.set` and `db.set_many` decide default (no `eq=`, no `cutoff=`) input
  equality on the stored canonical snapshots — the same operands and the same
  decision recomputation uses. They previously compared thawed values, which
  drops `FrozenRecord` type identity: setting `GridPoint(1, 2)` and then
  `{"x": 1, "y": 2}`, or a same-shaped different dataclass, counted as an
  equal update and was ignored, while the stored snapshot was replaced anyway,
  so a warm `strict`-mode dependent kept a dataclass-derived result no fresh
  database produces. This completes the "default comparisons no longer invoke
  `ValueAdapter` `thaw`/`freeze` hooks in any mode" change: the default input
  path now runs no adapter hook beyond freezing the incoming value. Inputs
  declared with `eq=` or `cutoff=` are unaffected and keep comparing the
  values as written.
- A recomputation producing an equal NaN-bearing value backdates like every
  other unchanged value. The default decision compares stored snapshots and a
  NaN never equals itself, so a query returning `float("nan")` re-ran its
  dependents on every request even though its canonical digest was unchanged;
  the decision now falls back to the record digests, which normalize NaN to
  one bit pattern. Because the fallback only adds equality, the shapes where
  the two disagree the other way — `True` against `1`, `1` against `1.0` —
  decide as before.
- A JSON object key containing a lone surrogate is a `json-decode-error`
  diagnostic rather than an exception out of `json_analysis`, because such a
  key reaches the cached payload verbatim as its own section name and again in
  every descendant's dot path. Values are unaffected: they reach the payload
  through `repr`, which escapes a surrogate. Payloads and cutoff tokens for
  every document without a lone surrogate are byte-identical.

### Performance

- The default backdate comparison runs on the canonical stored snapshots
  themselves instead of exposing both values and re-freezing them, so the
  warm recompute path no longer pays a deep thaw, validation walk, or
  re-freeze per comparison. Queries with an `eq=` or `cutoff=` policy keep
  the previous path and continue to receive mode-exposed values.
- Warm resource validation answers an unchanged-probe check from `probe()`
  alone and runs `probe_and_load` only on a probe miss, so unchanged file
  reads no longer decode their contents and no longer allocate the decoded
  value they would have discarded. Stored probe/value pairs still originate
  from a single atomic observation, and a content change under a stable stat
  signature is still detected.
- `WorkspaceSession` holds one kernel request span per public method, so a
  warm `analyze_workspace` validates each resource once per call instead of
  once per internal request.

### Fixed

- `~=` implements its PEP 440 definition: the upper bound is a prefix match,
  not an ordered comparison, so prereleases and dev releases of the excluded
  next release (`3.0a1` against `~=2.2`) no longer satisfy — an installed
  prerelease of the next major can no longer report a compatible-release
  requirement as satisfied. Wildcard matching compares the epoch, so `1!1.1`
  no longer satisfies `==1.1.*`. Both verified against `packaging` across
  epoch, post, dev, and prerelease shapes.
- `applicable_requirements` checks an installed version with pre-releases
  allowed, through the same helper dependency checking uses, so the two
  entrypoints agree that an installed `2.0.0rc1` satisfies `>=1.20` instead
  of reporting `version_mismatch` and `satisfied` for the same requirement in
  the same environment. `evaluate_version_specifier` keeps resolver-style
  exclusion unless the specifier opts in.
- Marker comparisons route wildcard literals through PEP 440 prefix matching:
  `python_version == "3.*"` is true on any 3.x interpreter and
  `python_version != "2.7.*"` no longer evaluates false, so requirements
  guarded by such markers are applicable again.
- `document_diagnostics` orders module-name diagnostics canonically, so a
  `$defs` key reorder — which backdates the canonicalized schema text — can
  no longer leave an incremental database returning a differently ordered
  diagnostics tuple than a fresh one.
- A definition named after a binding the generated module itself uses
  (`str`, `Literal`, `dataclass`, and the rest of the emitter's closed set)
  is rejected with the blocking `reserved-definition-name` diagnostic instead
  of emitting code whose imports silently shadow `typing` and builtins under
  type checking.
- `{"type": null}` produces the blocking `invalid-type` error at the `/type`
  pointer like any other invalid type value, instead of being conflated with
  an absent key and generating with only an `unconstrained-schema` warning.
- The polling watcher no longer loses updates. Its baseline reflects the
  content the mirror was actually synced from, so a file edited between
  session construction and the first poll — the whole initial analysis runs
  in that window — is detected and refreshed, and a failed refresh returns
  its paths to pending so the next tick retries instead of dropping the
  change forever.
- An LSP notification handler that fails with an unexpected error — a mirror
  write hitting a full disk, a client opening a directory URI — is logged and
  the server keeps serving, matching the request path's guard, instead of
  terminating the process with a traceback.
- A value change two levels above an untracked query invalidates its
  transitive dependents. A recomputation that lands a changed value now moves
  the revision the way input and resource changes always did; before, the
  parent of a `report_untracked_read` query re-executed and stored its new
  value at exactly the revision its own dependents had already verified, so a
  grandparent kept reporting `dependencies unchanged` and `db.get()` diverged
  from a fresh `Database`. An untracked query that re-executes to a
  byte-identical value does not move the revision, so warm requests over a
  stable graph still settle.
- Strict mode exposes cyclic and shared query results — and `read_input`
  values — through the same immutable container views it already used for
  query arguments, instead of leaking the raw graph envelope, which crashed
  `len()` and iteration with `TypeError`. Those views also freeze back:
  passing one into `db.set` or as a query argument re-encodes it to the
  identical canonical snapshot (same fingerprint, same cache node) where it
  previously hit `RecursionError`.
- The environment guard installed by `Database` matches `os._Environ` again:
  both `|` union directions and `|=` work (previously `TypeError` for any
  code in the process once a `Database` had ever been constructed), and the
  `encodekey`/`decodekey`/`encodevalue`/`decodevalue` helpers are reachable.
  Every other attribute — including the raw backing mapping — raises
  `AttributeError`, and union reads inside a query still require a `Resource`
  scope.
- An `@action` whose output layout migrates between a file and a directory
  (`pkg` ↔ `pkg/model.py`) reconciles instead of wedging permanently on its
  own ledger. Manifest entries that conflict with the new layout are deleted
  as orphans of the previous layout — before publication, under the usual
  tamper policy, pruning only directories the deletion left empty — and
  `plan()` reports those deletions without mutating. Previously every
  reconcile and every `plan()` raised `ActionPathError` until the manifest was
  edited by hand.
- Requirement lines carrying pip per-requirement options parse correctly:
  `pip-compile --generate-hashes` output no longer folds `--hash=...`
  continuation lines into the version text, which corrupted the specifier and
  misreported every requirement in a hashed lockfile. An undecidable
  specifier is reported `ambiguous` by `applicable_requirements`, matching
  dependency checking, instead of `version_mismatch`.
- The LSP server serializes writes to its output stream, so a watcher-thread
  diagnostics notification can no longer interleave with a main-loop response
  and corrupt `Content-Length` framing. The published-diagnostics bookkeeping
  is guarded by the same lock.
- The session lookups behind `textDocument/declaration` and the rename
  preflight take the session lock like every other entry point, so they no
  longer read the workspace mirror mid-refresh, and they raise `RuntimeError`
  after `close()` like the rest of the session surface.
- `WorkspaceSession` diagnostics no longer leak the temporary workspace-mirror
  path through their message text. A kernel `Diagnostic` has no path field, so an
  integration that needs to name a file interpolates it into the message; under a
  session that file was the mirror copy, in a randomly named temporary directory.
  `source-decode-error`, `cycle`, `missing-requirements-file`, and the `-r path
  outside project` error now name the real workspace path, matching the already
  correct `path` field. Affected messages are therefore identical across runs,
  which `pyinc-tools analyze --format text` and LSP `publishDiagnostics` both
  depend on.
- A query that catches an error from a resource read is now from-scratch
  consistent across the file appearing and disappearing. Reading an optional
  configuration file — the ordinary `try: ... except FileNotFoundError:`
  pattern — recorded no dependency at all, so after the file was created
  `db.get()` kept returning the default while a fresh `Database` returned the
  file's contents, and `inspect()` reported `dependencies=()` with reason
  `dependencies unchanged`. Deleting a file the query had already read was the
  mirror-image failure: `FileNotFoundError` propagated out of `db.get()` from
  inside the invalidation machinery, before the query body ran, so the query's
  own `except` clause never saw it. Both now match a fresh `Database`.
- An observation that raises without leaving a record retires the record's
  stored probe, so a world that returns to exactly the state that probe
  describes — an undo, a branch switch back — re-loads instead of reusing it,
  and the queries reading it are invalidated rather than answering at a
  revision their own dependents already verified past. Before, replacing a
  tracked file with a directory and then restoring it left every transitive
  dependent holding a stale value permanently.
- `json_analysis` can no longer raise `UnsupportedValueError`, and
  `config_analysis` can no longer raise it either, out of their cutoff
  functions for an over-deep document — previously reachable on a post-edit
  recomputation with enough stack. `xml_analysis` can no longer raise
  `RecursionError` for a deeply nested document.
- Stack-exhaustion diagnostics carry fixed text rather than `RecursionError`'s
  message, which varies with where the stack blew and was flowing into a cached
  payload.
- Base-class flattening no longer loses members depending on traversal order.
  A class first reached at the depth cap was recorded as visited without
  contributing anything, so a later, shallower reach of that same class was
  skipped — which could report a subclass as having strictly fewer members than
  a base it inherits from.
- `{"type": "object", "const": V}` in a definition reports `const-type-mismatch`
  instead of dropping the `const` and emitting an empty dataclass. Annotations
  on an `anyOf` null branch are validated like annotations anywhere else, and an
  `anyOf` whose branches are both `{"type": "null"}` is rejected rather than
  compiling to a bare `None` type.
- A workspace path that swaps kind — a tracked file replaced on disk by a
  same-named directory, or the reverse — no longer wedges mirror sync. The
  conflicting mirror entry, and any conflicting entry between it and the
  mirror root, is cleared before the new one is materialized, so the refresh
  succeeds instead of raising and leaving its paths pending for every later
  watcher tick to retry. A mirrored child whose parent is a file again is
  dropped: the traversal reports that as absence rather than an unsafe path
  component, while a symlinked parent — which reports the same errno under
  `O_NOFOLLOW` — is still rejected. An overlay write lands over a mirror
  directory the swap left behind.
- The polling watcher stops for a closed session, not for any `RuntimeError`.
  A `RecursionError` — a `RuntimeError` subclass — or a `RuntimeError` raised
  while collecting the snapshot or refreshing now reaches the watcher's error
  handler and the loop keeps polling, instead of silently retiring the watcher
  thread as though the session had closed.
- `WorkspaceSession`'s request lock is released even when tearing down its
  integrations request scope or its kernel request span raises, and the span
  closes even when the scope exit raises. A failure below the session can no
  longer leave the lock held — which would have deadlocked every later call on
  that session, `close()` included — or leave a kernel request open past the
  stability it declares.
- A property named after a binding the generated module itself uses (`str`,
  `dict`, `Literal`, and the rest of the emitter's closed set) is rejected with
  the blocking `reserved-field-name` diagnostic at the property. The field bound
  that name for the rest of its own class body, so the annotations after it
  silently stopped naming the builtin or the `TYPE_CHECKING` import they spell —
  `zone: str` read the model's own `str` field — and the emitted module failed
  type checking with nothing reported by analysis.
- `enum` and `const` members are checked against the nullable union declared
  beside them instead of being reported as disagreeing with it. Only a string
  `type` was understood, so every member of
  `{"type": ["string", "null"], "enum": ["red", null]}` — including the ones
  that matched — produced an `enum-type-mismatch` or `const-type-mismatch`
  error, and a definition-level enum additionally rejected the union as an
  `unsupported-enum-type`. A member matching neither the type the union names
  nor the null it adds is still an error.
- A definition whose alias resolves straight back to its own name — through a
  bare `$ref`, a single-branch `allOf`, or a nullable `anyOf` — is reported as
  `self-referential-alias` instead of emitting `Loop: TypeAlias = 'Loop | None'`,
  which no type checker can resolve. Recursion through a model or a container
  (`Tree` → `list[Tree | None]`) names a type and still generates.
- An `@action` run that stops between publishing its outputs and publishing
  its ledger no longer wedges every later run. A recorded output whose parent
  path is now a file cannot exist, and a recorded output whose path is now a
  directory holding nothing but files of the desired layout was already
  released by the stopped run: both are recognized in preflight, so the next
  locked `reconcile()` — and `plan()` — converge the set instead of raising
  `ActionPathError` for every desired set until the manifest was edited by
  hand. Recovery never deletes to repair, so a directory holding any other
  entry still refuses under the tamper policy, and files a stopped run
  published but never recorded stay unowned: a rollback or teardown that
  would have to remove them is still refused until a reconcile of the
  published layout records them.
- `plan()` reports the prune refusal `reconcile()` enforces. A directory that
  the previous layout must leave empty is checked for unowned entries during
  preflight and the refusal names the blocking entry, so a dry run no longer
  reports a clean migration that the next reconcile abandons after deleting
  its orphans — and that reconcile now refuses before deleting anything.
- `freeze` detects sharing across the whole boundary value instead of one
  wrapper at a time. A `strict`-mode result like `(items, items)` — one list
  reached twice through a raw tuple — stores a `FrozenGraph`, but re-freezing
  the exposed view returned the tree `(FrozenList, FrozenList)`, so the view
  did not round-trip to its own snapshot or fingerprint, through `db.set` or
  as a query argument. Only the `[items, items]` spelling worked, because a
  list spine keeps the aliasing inside one wrapper. Fingerprints for every
  shape that already round-tripped are unchanged, and a tuple carrying no
  `Frozen*` wrapper pays nothing.
- A JSON document carrying a lone-surrogate escape (`"\ud800"`) no longer
  fails an incremental recomputation a fresh database completes. `json.loads`
  accepts the escape and the snapshot grammar refuses it, but the cutoff's
  defensive clause named only `ValueError` and `freeze` raises
  `UnsupportedValueError`, a `PyIncError`. The clause now names it and
  degrades to the raw text as intended. The TOML and XML cutoffs gained the
  same name defensively; neither parser can produce a lone surrogate.
- `fingerprint_snapshot` and `serialize_snapshot` reject an integer wider than
  CPython's int-to-str conversion limit with `UnsupportedValueError` naming
  the digit limit, instead of letting a raw `ValueError` out of the encoder.
  The `K2` grammar is unchanged.

## [3.0.0] - 2026-07-12

### Release validation

- RC candidate: `v3.0.0rc1` at `6296106725e372a428dfeca5e45390f8cd2821fa`
- [x] Clean installations from the published RC artifacts passed.
- [x] The benchmark/correctness report was reviewed; every pyinc result matched a fresh run.
- [x] Final promotion approved.

## [3.0.0rc1] - 2026-07-12

### Added

- Stable keyed `Input` and `Query` identities, optional `@query(key=...)`, a
  public generic `Resource` contract, `Database.read_resource`, and
  `BinaryFileResource`.
- Zero-based `SourcePosition` / `SourceRange` geometry, public `DocumentMap`
  encoding conversion, plus lexical `SymbolId`, `Scope`, `Binding`, and
  `ScopeTree` resolution shared by Python navigation and refactoring features.
- Code-generation diagnostic severities and JSON Pointers, with
  `SchemaGenerationError` preventing reconciliation when an error diagnostic
  exists.
- `pyinc-tools --version`, LSP 3.18 position-encoding negotiation, Python 3.14
  support, and installed-wheel validation in CI.
- `python -m pyinc_tools` and `python -m pyinc_tools.cli` module execution.
- Task-oriented getting-started and LSP references, plus an offline
  documentation checker for links, anchors, executable examples, CLI output,
  and the documented stable integration surface.
- A correctness-first benchmark workflow that uploads five isolated-run
  `samples.csv`, summarized `benchmark.csv`, `benchmark.md`, and provenance-rich
  `metadata.json` artifacts.
- Automated GitHub Releases after PyPI publication and a manual 12-environment
  workflow that validates exact published artifacts and compares PyPI and
  GitHub Release hashes.

### Changed

- Replaced marshal-based code identity with canonical typed code-object
  encoding, including slice and nested-code constants, definition defaults,
  immutable and transitive captures, comparator policies, resource/adapter
  implementations, and relevant interpreter/build flags.
- Unpinnable equality/cutoff policy captures and local or dynamically unbound
  class captures are rejected instead of collapsing to name- or type-only
  identities. Input policies, resources, and adapters now independently include
  interpreter/build identity at their checkpoint trust boundaries.
- Checkpoints now use fully prevalidated manifest schema v4. v1-v3 checkpoint
  manifests are intentionally rejected; the `K2` user-value encoding remains
  unchanged.
- `set_many` is all-or-nothing, query execution commits records and dependency
  rewiring only after success, all public database state operations are locked,
  profiles use bounded timing aggregates, and evicted nodes leave no profile or
  registry state behind.
- `ReconcileResult.written` is replaced by `created`, `updated`, and
  `repaired`. Action manifests use root-bound schema v2 and a SHA-256 name
  derived from the full tool identity.
- Python source is decoded with PEP 263/BOM rules and AST byte columns are
  converted to Unicode-code-point ranges at the parser boundary. Unsupported
  attribute chains now return no result instead of speculative locations or
  edits.
- `pyinc_tools` diagnostics, locations, highlights, edits, links, lenses,
  semantic tokens, and hierarchy results now expose direct `SourceRange` (or
  `SourcePosition`) fields. `WorkspaceSession.find_references` and rename use
  resolved `SymbolId` values; name-only access and v2 coordinate aliases are
  removed. File symlinks are rejected by the workspace mirror.
- `pyinc_tools` now separates shared models, document geometry, pure analysis,
  edit generation, workspace mirroring/watching, and JSON-RPC framing behind
  the lock-owning `WorkspaceSession` façade. Tools consume the stable public
  integration surface instead of private resolver internals.
- `pyinc_tools` carries its identifier-lexing helper instead of importing the
  kernel-private `pyinc._python_lexing` module, keeping both consumer packages
  on public `pyinc` / `pyinc.integrations` contracts.
- Generated model packages use deferred annotations and type-checking-only
  imports for cyclic local references. Definition/module collisions are
  checked after Unicode normalization, snake conversion, and case folding.
- Documentation now has one purpose per guide or contract, uses PyPI-safe
  navigation, describes the exact from-scratch-consistency guarantee and
  frozen container types, and keeps protocol operation details in a compact
  LSP reference.
- Benchmark correctness, fixed row coverage, deterministic work counts, and
  node ceilings are release gates. Wall timings are informational medians with
  min/max ranges and no `tracemalloc` instrumentation; generated reports are no
  longer checked into the repository.

### Fixed

- Filesystem artifact publication and action reconciliation are serialized
  across processes. Writes are flushed, atomically published from the same
  directory through no-follow filesystem handles where available, and
  conflicting artifact bytes are refused.
- Action preflight now rejects malformed manifests, unsafe or ambiguous paths,
  symlink escapes, non-regular owned targets, malformed digests, and conflicting
  file/directory declarations before mutation.
- Unsafe or non-regular action and artifact lock paths now surface typed
  `ActionPathError` / `ArtifactStoreError` failures rather than raw OS errors.
- Workspace mirrors use content hashes, filter source/configuration inputs,
  honor exclusion globs, retain recursively referenced requirements files
  regardless of suffix, surface requirements-chain diagnostics, and reject
  escaping symlinks.
- XML analysis rejects every `DOCTYPE` and entity declaration before parsing,
  including external-entity and entity-expansion payloads.

### Security

- Release builds and validation run without OIDC publishing privileges. A
  separate minimal trusted-publishing job receives only the verified sdist and
  wheel artifacts.
- Checkpoint records are validated completely before any cache warming, and
  resource implementation changes invalidate reuse even when probes happen to
  match.
- Artifact-store keys are restricted to lowercase SHA-256 digests (optionally
  checkpoint-prefixed with `ck`), preventing path traversal and platform path
  injection.
- Tag publication waits for reusable CI, CodeQL, and benchmark gates. The
  GitHub Release job receives only `contents: write`, reuses the exact verified
  distributions, and publishes their `SHA256SUMS` file.

### Migration

- This is a clean API and persistence break. See
  [`docs/migration-v3.md`](docs/migration-v3.md) before upgrading and discard
  v2 checkpoint/action ledger state as described there.

## [2.6.0] - 2026-07-05

### Added

- **`symbol_resolution.class_model` surface.** A new integration entrypoint
  `class_model(db, root, path, qualified_name)` returns a `ClassModel(path,
  qualified_name, members, unresolved_bases)` — the declaration-only member set
  of a workspace class. `ClassMember` (`method` / `class_variable` /
  `instance_variable`, each carrying `defining_path` / `defining_class`) covers
  class-body variables, methods, and `self.NAME` instance attributes collected
  from methods whose first parameter is literally `self`. The model is
  **flattened over workspace base classes** depth-first, left-to-right,
  first-definition-wins (a derived member shadows a base member of the same
  name), bounded by `MAX_BASE_DEPTH = 8` with a cycle guard, with base files
  queried one at a time (`class_models_for_file`) so an edit to one base
  invalidates per file. This is intentionally **not** C3 MRO. Bases that do not
  resolve to a workspace class (stdlib / installed / missing / ambiguous /
  starred) contribute no members and surface in `unresolved_bases`. `ClassMember`
  and `ClassModel` join the stable `pyinc.integrations` surface. No kernel
  contract change.
- **Instance-member completion in `pyinc-tools` LSP.** Completion now serves
  member lists that previously required type inference, all off the new
  `symbol_resolution.class_model` surface (still declaration-driven — no runtime
  types). `self.` / `cls.` inside a method complete the enclosing class's
  instance / class view; a bare name whose *declared* annotation (bare `Name`,
  one-hop `mod.Foo`, or whole-string forward reference) names a workspace class
  completes that class's instance view; and a bare `Foo.` class owner now serves
  the **flattened** class view, so `Derived.` and `self.` alike show members
  inherited from workspace base classes. Subscripted / union / deep-dotted /
  callable annotations, chained owners (`obj.attr.`), closures over the
  receiver, and non-workspace bases contribute nothing. No kernel or
  `pyinc.integrations` contract change beyond the `class_model` surface above.
- **Completion / signatureHelp polish in `pyinc-tools` LSP.** Three
  refinements to the already-shipped completion and signature-help features:

  - **Dotted attribute owners in completion.** `pkg.sub.<caret>` now completes
    when the dotted owner is exactly a workspace module (its exports), and
    `pkg.sub.C.<caret>` / `M.C.<caret>` complete a class's members when the
    owner is `<workspace-module>.<class>`. Owner resolution is longest-match
    first and routes module lookup through an exact `workspace_symbol_index`
    match so ambiguous resolutions never produce results; single-component
    owners keep the existing `resolve_symbol` path. Instance chains
    (`obj.attr.<caret>`) and stdlib/installed owners still yield nothing.
  - **Attribute-call signatureHelp.** `M.foo(` and `M.C(` now surface a
    signature: a single-dot owner that is a bare `Name` is resolved through the
    file's imports to a workspace module and then the attribute inside it (the
    same bare-`Name`-LHS idiom `callHierarchy/outgoingCalls` and
    `inlayHint` use — now a shared `_resolve_attr_on_module` helper). Deep
    chains (`pkg.sub.foo(`) and subscripted calls stay `null`.
  - **Default values in signature labels.** Signature-help labels now render
    parameter defaults (`name: ann = default` / `name=default`), extracted
    from the defining file's source. `symbol_resolution.Parameter` is
    unchanged — the contract type carries no default — so this is a
    consumer-side read; completion `detail` and hover are untouched.

  Also exports the pre-existing `CompletionItem` / `CompletionItemKind` types
  from `pyinc_tools`. No kernel or `pyinc.integrations` contract change.
- **`textDocument/linkedEditingRange` in `pyinc-tools` LSP.** The server
  now advertises `linkedEditingRangeProvider: true` and handles
  `textDocument/linkedEditingRange` requests. For the symbol under the
  cursor it returns the set of ranges in the *current file* that an editor
  should mirror as the user types (so editing one updates them all live),
  together with a `wordPattern` of `[A-Za-z_][A-Za-z0-9_]*` that tells the
  client to stop mirroring once the typed text is no longer a Python
  identifier.

  The mirrored range set is exactly the file-scoped occurrences that
  `textDocument/documentHighlight` already reports — the declaration name
  span (repaired off the synthetic `def` / `class` placeholder that
  `find_references` emits) plus every verified bare-name and
  rightmost-attribute reference — so all ranges cover the same bare
  identifier and are safe to edit simultaneously. This is **in-file only**
  and intentionally lighter than `textDocument/rename`: it never touches
  other files, so workspace-wide renames still go through `rename`. Unknown
  identifiers, whitespace cursor positions, non-workspace targets (stdlib /
  installed / ambiguous / missing), and files outside the workspace return
  `null`.

  New consumer-layer dataclass `LinkedEditingRange(lineno, col_offset,
  end_col_offset)` (1-based `lineno`, 0-based `col_offset` /
  `end_col_offset`, matching the rest of the session dataclasses) and
  entrypoint `WorkspaceSession.linked_editing_ranges_at(path,
  qualified_name) -> tuple[LinkedEditingRange, ...]` (thread-safe via the
  same `_state_lock` used by every other public mutator, since it delegates
  to `find_document_highlights`). Lives entirely on top of the stable
  `pyinc.integrations` public surface (`find_references`) — no kernel
  contract change and no new integration-layer surface. Limitations are
  documented in `docs/pyinc-tools-guide.md`.
- **`unused-import` diagnostic in `pyinc-tools` LSP.** Analysis now flags a
  workspace `from M import name [as alias]` binding when nothing in the file
  uses it. Conservative by design: only `from` imports resolving to a
  workspace module are considered (so `find_references` can verify usage);
  `import M`, stdlib / installed targets, and `from M import *` are left
  alone. `__init__.py` files, self-alias re-exports (`from y import z as z`),
  and bindings another workspace module re-imports from this file (a
  cross-module re-export) are never flagged. The diagnostic is severity Hint
  and carries the LSP `Unnecessary` tag (`tags: [1]`) so editors fade the
  binding; it rides both the push and pull diagnostic channels. New additive
  `AnalysisDiagnostic.tags: tuple[str, ...]` field, folded into the pull-model
  `resultId` signature so a tag change re-issues the report.
- **`textDocument/codeAction` quick fixes in `pyinc-tools` LSP.** The server
  now advertises `codeActionProvider: {codeActionKinds: ["quickfix"]}` and
  answers `textDocument/codeAction` with diagnostics-anchored quick fixes (no
  refactorings). For diagnostics intersecting the request range it offers:
  *Remove unused import* (`unused-import`), *Remove unresolvable import*
  (`missing-import`), and for `unresolved-symbol` a *Remove import of 'name'*
  action plus a *Import 'name' from '<module>'* retarget when exactly one
  workspace module exposes a top-level symbol of that name (single-name
  statements only). Each action echoes its anchor diagnostic and carries a
  `WorkspaceEdit` (`{"changes": {uri: [TextEdit]}}`); `context.only` is
  honored. New consumer-layer dataclasses `CodeAction(title, kind, diagnostic,
  edits)` and `CodeActionEdit(path, start_line, start_character, end_line,
  end_character, new_text)` (0-based, LSP-style) and entrypoint
  `WorkspaceSession.code_actions_for_range(path, start_line, start_character,
  end_line, end_character)`. Reuses the existing import-deletion geometry
  (`_statement_line_span` / `_alias_list_deletion_edits`) and
  `workspace_symbol_index` — no kernel or integration-layer contract change.
- **Durable cross-run cache is now a trusted guarantee.** The
  `save_checkpoint` / `load_checkpoint` flow shipped in v2.0.0 carried only a
  best-effort warm; the checkpoint path now earns from-scratch consistency
  across processes and runs, under the conditions restated in
  `docs/kernel-contract.md` limitation 4 (single-process store access; the
  checkpoint's inputs set before load; resources honouring the probe contract;
  adapters registered with unchanged implementations). The supporting machinery:

  - **Deterministic cross-process query identities.** `Input` carries a per-name
    `seq` ordinal so same-named inputs resolve to the correct node on reload;
    captured queries now fold their *full* definition payload into the parent's
    identity transitively (a body edit to any dependency query moves the
    parent); and the code fingerprint includes the build configuration (`-O`
    optimize flag, platform, `os.name`, UTF-8 mode) alongside the interpreter
    and version tuple.
  - **Execute-to-verify frontier reuse.** A checkpoint dependency that cannot be
    warmed directly is re-executed from its pinned code — resources probed
    against the real world — and its result digest compared to the manifest, so
    a warmed subtree is trusted only when its frontier reproduces.
  - **Adapter-implementation digests.** Each registered adapter's
    `freeze`/`thaw` body is fingerprinted and recorded in the manifest; every
    thaw-into-live path refuses a record whose adapter has changed or vanished
    since the save, even a change to `thaw` alone.
  - **Checkpoint manifest schema v3.** Canonically sorted and content-addressed,
    with the kernel fingerprint version cross-checked at load.

  On upgrade, checkpoint keys written before this branch cannot be loaded:
  `load_checkpoint` rejects their older manifest schema loudly (`ValueError`),
  so callers must drop the old key and `save_checkpoint` afresh. Within v3
  checkpoints, records whose identities shift (interpreter, build
  configuration, or code changes) miss safely — the affected queries
  re-execute on the first `get` (a one-time re-execution wave) rather than
  being trusted. Stored *snapshot* artifacts remain valid either way — the
  `fingerprint_snapshot` encoder (`K2;`) is unchanged, so an existing object
  store need not be rewritten. No `pyproject.toml` version bump accompanies
  this (release hygiene is tracked separately).
- **Completion (`textDocument/completion`) in `pyinc-tools` LSP.** The server
  now advertises a `completionProvider` (`{"triggerCharacters": ["."],
  "resolveProvider": false}`) and serves declaration-driven completion —
  candidates come from real `symbol_resolution` bindings and import resolution,
  never inferred runtime types. Three contexts are recognised: a bare-name
  prefix (current-file module-level symbols, workspace module names, and Python
  keywords), attribute access `M.<prefix>` for a bare-name `M` that resolves to
  a workspace module (its exports) or class (its methods and class variables),
  and import position (`from pkg import <prefix>` → `pkg`'s names; `import
  <prefix>` → workspace module names). Because a mid-edit buffer is usually
  unparseable at the caret (a trailing `owner.`), the server repairs the caret
  line to `pass` before analysis, preserving every top-level import and
  definition for resolution. Items carry `label` / `kind` / `detail` (signature
  label for callables, declared annotation for variables). Strings, comments,
  non-bare-`Name` owners, and stdlib / installed targets yield nothing.
  Consumer entrypoint: `WorkspaceSession.completions_at(path, line,
  character)`. Lives entirely on the stable `pyinc.integrations` surface — no
  kernel change. Documented in `docs/pyinc-tools-guide.md`.
- **Declared-output reconciliation layer (`@action`).** A new, domain-agnostic
  kernel surface for turning query-derived *desired* artifacts into files on
  disk without leaking side effects into queries. `Output(path, content)` is
  snapshot-safe, so a `tuple[Output, ...]` can be a `@query` return and
  participate in caching/backdating; `@action(tool=...)` wraps a pure
  `(db, *args) -> Iterable[Output]` function, and `Action.reconcile(...)` /
  `Action.plan(...)` apply it to the filesystem:

  - writes only outputs whose on-disk bytes differ from the desired bytes
    (the same content-hash rule repairs out-of-band edits to generated files);
  - deletes outputs the action previously owned but no longer declares, using a
    per-`tool` JSON ownership ledger so files the action did not write are never
    touched;
  - writes atomically (temp file + `os.replace`) and skips the manifest write
    when nothing changed, so a no-op reconcile performs zero filesystem writes;
  - supports a dry-run `plan` that reports `written` / `deleted` / `unchanged`
    without touching disk.

  Reconciliation runs at top level only and does **not** change query semantics,
  the value membrane, untracked-read enforcement, or the modes. The kernel's
  from-scratch guarantee lifts to the filesystem (incremental reconciles == a
  fresh run into an empty directory). Exported from `pyinc` as `Output`,
  `ReconcileResult`, `Action`, and `action`; documented in
  `docs/action-contract.md`. Runnable examples:
  `examples/action_reconcile_demo.py` and the end-to-end include-aware `calc`
  fixture (`examples/calc/`, `examples/calc_demo.py`), the canonical worked
  example for a query graph that reconciles outputs to disk.
- **`pyinc_codegen` — JSON-Schema → typed-Python compiler.** A new consumer
  package (`src/pyinc_codegen/`), the first useful file→file compiler built on
  pyinc. It reads a JSON Schema and generates one typed model and one doc file
  per definition plus an aggregate `__init__.py`, emitted through the `@action`
  layer so only changed artifacts are written.

  - Supported subset: local documents; `$defs` and legacy `definitions`; local
    `$ref`; object `properties`; `required` vs optional; arrays; primitives;
    `enum`; nullable unions; `description` (docs only); deterministic
    diagnostics for unsupported constructs.
  - Decomposed for output-granular incrementality: whitespace/key-reorder edits
    backdate (zero writes); a description-only edit rewrites only the doc; a
    property type/requiredness change rewrites the affected model and its
    reference-graph closure (each rewritten only if its bytes change); adding or
    removing a definition touches only that definition's files plus the index.
  - Stdlib-only (JSON parsed with `json` + dict walking) and built on pyinc's
    **public API only** — no JSON-Schema concept lives in `src/pyinc`. Public
    surface: `generate`, `generate_outputs`, `schema_analysis`, and the
    `SchemaModel` / `FieldModel` / `Diagnostic` / `SchemaAnalysis` result types.
    Sample schema and runnable demo in `examples/`; documented in
    `docs/codegen-guide.md`.
- **Benchmark + correctness harness (`bench/`).** A reproducible harness (not
  shipped in the wheel) exercising four targets — synthetic kernel query
  graphs, the calc fixture, JSON-Schema codegen, and action reconciliation —
  across a canonical edit sequence (cold, unchanged, unreferenced edit,
  comment-only edit, localized edit, high-fan-out shared edit, removed
  artifact, tampered output, checkpoint restore). It compares pyinc against
  full recomputation, a naive per-key cache, and `joblib.Memory`, recording
  wall-time, peak memory, dependency-graph size, and cache size, and emits a
  CSV + markdown report under `bench/results/`. Every scenario pairs its timing
  with a correctness assertion that pyinc's incremental output equals a fresh,
  cache-free run; the tampered-output scenarios drive the real action reconcile
  path. `joblib` is a new `bench` optional-dependency group, imported lazily and
  never by `src/pyinc` or `src/pyinc_codegen`. Run with
  `PYTHONPATH=src python -m bench.run`.
- **`pyinc-tools` LSP `serverInfo.version`** bumped from `"2.1.0"` to
  `"2.6.0"` to align with the package version pinned in `pyproject.toml`.

### Fixed

- **Wildcard version-specifier prefix matching in `requirement_evaluation`.**
  `==X.Y.*` / `!=X.Y.*` specifiers trimmed trailing zeros from the spec's
  release before comparing, shortening the prefix — so `==1.0.*` wrongly
  matched any `1.x` release (e.g. `1.5`). The full spec release is now used
  as the prefix, so `==1.0.*` matches only `1.0.x`.

- **Checkpoint warm path could return stale or tampered values.** The v2.0.0
  warm restored records without their dependency edges and trusted whatever
  bytes the store returned, so a warmed cache could serve a value a fresh run
  would not produce. Closed on every front:
  - restored records now carry their real dependency edges and are re-verified
    transitively through them, replacing the old warm-time bypass;
  - resources are re-probed — or their queries re-executed — live at reload
    instead of the stored probe hint being trusted blindly;
  - every snapshot read from the store is rejected unless `sha256` of its raw
    bytes matches the digest it was keyed by, and the manifest is re-hashed
    against the checkpoint key before anything is parsed out of it;
  - any dependency that cannot be resolved or verified — runtime-import-reached
    query subgraphs, untracked (`report_untracked_read`) records, missing or
    corrupt store bytes — refuses the warm and re-executes rather than guessing.
- **Refcount-dependent code fingerprints.** `_code_fingerprint` now marshals
  code objects with `marshal` format 2 instead of the default. Format ≥3 encodes
  interning / `FLAG_REF` state, so a code object's bytes could flip once one of
  its string constants gained a reference at runtime (e.g. a regex literal
  retained by `re`'s cache after first use), making a query's identity depend on
  live refcounts and shift between two keyings in the same process. Format 2
  fully encodes the code object without shared references, so identities are
  stable within a process and reproducible across processes.
- **Dirty-graph saves no longer persist stale records.** `save_checkpoint` omits
  any record whose cached value no longer matches the live graph — a dependency
  moved since the record last executed, with no intervening `get` — because
  persisting it would bake in the dependency's *new* digest while warming the
  *old* value on reload. Such records are simply re-executed after reload.

## [2.5.0] - 2026-06-05

### Added

- **Pull diagnostics (`textDocument/diagnostic` + `workspace/diagnostic`)
  in `pyinc-tools` LSP.** The server now advertises a `diagnosticProvider`
  (`{"identifier": "pyinc-tools", "interFileDependencies": true,
  "workspaceDiagnostics": true}`) and implements the LSP 3.17 pull-diagnostic
  model alongside the existing `textDocument/publishDiagnostics` push channel.

  - `textDocument/diagnostic` runs `analyze_file` on the requested document
    and returns a full report `{"kind": "full", "resultId", "items"}` whose
    `items` are the same `Diagnostic` objects the push channel emits for that
    file (codes `missing-import`, `ambiguous-import`, `undeclared-import`,
    `unresolved-symbol`, `ambiguous-symbol`, plus `pyinc.python_source` parse
    errors). A clean file returns an empty-`items` full report; a pull for a
    URI outside the workspace returns an empty full report instead of
    failing the request.
  - `workspace/diagnostic` runs `analyze_workspace` once and returns
    `{"items": [...]}` with one report per analyzed `.py` file (plus any
    config / requirements file that carries dependency diagnostics), sorted
    by path. Files that are now clean still receive an empty-`items` report
    so clients can clear stale problems. `version` is always `null`.
  - The pull channel is **stateless**: each `resultId` is a SHA-256 over the
    file's diagnostic signatures, so when the client echoes a matching
    `previousResultId` (or `previousResultIds: [{uri, value}]` for the
    workspace request) the server answers with an `unchanged` report rather
    than resending. No server-side per-document bookkeeping is added, so the
    push and pull channels coexist without interference.

  Lives entirely on top of the stable `pyinc.integrations` surface
  (`analyze_file` / `analyze_workspace` already drive the push channel) — no
  kernel contract change and no new integration-layer surface. Documented in
  `docs/pyinc-tools-guide.md`.
- **`textDocument/declaration` in `pyinc-tools` LSP.** The server now
  advertises `declarationProvider: true` and handles
  `textDocument/declaration` requests, completing the goto-* family
  (`definition`, `typeDefinition`, `references`, `declaration`). Returns a
  single-entry `Location[]` pointing at the *binding statement* in the
  current file for the symbol under the cursor.

  This is **distinct** from `textDocument/definition`, which follows
  `import` / `from … import` chains through to the imported target's
  file. The cursor's identifier is looked up in the current file's
  `ModuleSymbolTable` (exact `qualified_name` match wins over a bare-name
  match against the last dotted component); the returned range spans the
  bare-name identifier on the matched `Symbol.lineno` line, located by a
  word-boundary scan. Behaviour by symbol kind:

  - `function` / `class` / `method` / `variable` / `class_variable` — the
    declaration coincides with the definition (the def/class/assignment
    line), so `declaration` and `definition` return the same location.
  - `import_alias` / `from_import_alias` — the declaration is the
    `import` / `from … import` statement in the current file, even when
    the import resolves to a stdlib / installed / missing target. For
    example, clicking on `os` in a file that does `import os` returns the
    `import os` line, where `definition` returns `[]` (stdlib targets
    are not surfaced by the LSP).
  - `wildcard_import_stub` — the local symbol table only records a literal
    `*` entry, not the bare names brought in by the wildcard, so a
    bare-name reference whose source is `from M import *` returns `[]`.

  Unknown identifiers, whitespace cursor positions, and files outside the
  workspace also return `[]`. New consumer-layer dataclass
  `DeclarationLocation(path, lineno, col_offset, end_col_offset)`
  (1-based `lineno`, 0-based `col_offset` / `end_col_offset` matching the
  rest of the session dataclasses) and entrypoint
  `WorkspaceSession.declaration_location_at(path, qualified_name) ->
  DeclarationLocation | None` (thread-safe via the same `_state_lock`
  used by every other public mutator). Lives entirely on top of the
  stable `pyinc.integrations` public surface
  (`module_symbol_table`) — no kernel contract change and no new
  integration-layer surface.

- **Type hierarchy in `pyinc-tools` LSP.** The server now advertises
  `typeHierarchyProvider: true` and implements three new requests:

  - `textDocument/prepareTypeHierarchy` — resolves the identifier under
    the cursor through `symbol_resolution.resolve_symbol`; if the target
    is a workspace `class`, returns a single `TypeHierarchyItem`
    describing the declaring `ClassDef`. The item's `range` spans the
    whole `class` block (including any decorator lines), `selectionRange`
    is the bare class-name span on the header line, and the item's
    `data` field carries `{"path", "qualified_name"}` so subsequent
    `supertypes` / `subtypes` requests do not need to re-resolve.
    Functions, methods, variables, import aliases, `from_import`
    aliases, wildcard-import stubs, and stdlib / installed / ambiguous
    / missing targets all return `null`.
  - `typeHierarchy/supertypes` — parses the item's declaring file,
    locates the `ClassDef` matching the item's qualified name, and
    resolves each entry of its `bases` list. `Subscript` bases
    (`Generic[T]`, `Base[T]`) are unwrapped to their `value` once
    before resolution, so generic base classes still navigate. Bare
    `Name(id=X)` bases resolve `X` through the declaring module's
    imports; `Name.attr` bases resolve the LHS to a workspace module
    and then `attr` inside it (mirroring `find_references`'s
    LHS-bare-Name handling). Deep attribute chains
    (`pkg.subpkg.Foo`), `Starred` bases, and call expressions
    produce no entry. Only workspace `class` targets contribute
    items; stdlib / installed / ambiguous / missing bases are
    dropped. Duplicates by `(path, qualified_name)` are collapsed.
  - `typeHierarchy/subtypes` — walks the workspace once via
    `workspace_analysis` and visits every `ClassDef` recursively
    (qualified-name nesting follows `module_symbol_table`:
    `Outer.Inner`). For each candidate's `bases` list, each base is
    unwrapped (subscript dropped) and resolved through the candidate's
    module imports using the same rules as `supertypes`; a candidate
    is a subtype iff at least one resolved base points at the target
    `(path, qualified_name)`. The target itself is excluded. Only
    direct subtypes are returned — clients drill down by calling the
    endpoint recursively. Output is sorted by
    `(path, qualified_name)`.

  New consumer-layer dataclass `TypeHierarchyItem(name, kind, path,
  qualified_name, detail, range_start_line, range_start_character,
  range_end_line, range_end_character, selection_start_line,
  selection_start_character, selection_end_line,
  selection_end_character)` (all position fields 0-based, LSP-style;
  `kind` typed as `TypeHierarchyItemKind = Literal["class"]`) and
  three new `WorkspaceSession` methods:
  `prepare_type_hierarchy(path, line, character)`,
  `type_hierarchy_supertypes(path, qualified_name)`, and
  `type_hierarchy_subtypes(path, qualified_name)`. All three are
  thread-safe (RLock-guarded via the same `_state_lock` used by every
  other public mutator). Lives entirely on top of the stable
  `pyinc.integrations` public surface (`workspace_analysis`,
  `module_symbol_table`, `resolve_symbol`) — no kernel contract change
  and no new integration-layer surface.

  Limitations are documented in `docs/pyinc-tools-guide.md`. The main
  ones are inherited from the existing resolver: top-level identifiers
  only (`prepareTypeHierarchy`); workspace `class` targets only
  (stdlib / installed base classes are dropped); deep attribute chains
  (`pkg.subpkg.Foo`) in the `bases` list are skipped (use
  `from pkg.subpkg import Foo` or `from pkg import subpkg` to opt in);
  metaclass relationships are not reported.
- **`workspace/willDeleteFiles` in `pyinc-tools` LSP.** The server now
  advertises `workspace.fileOperations.willDelete` with a `**/*.py` file
  filter (alongside the existing `willRename`) and handles
  `workspace/willDeleteFiles` requests. For each `{uri}` entry the server
  walks every Python file in the workspace and emits a `WorkspaceEdit`
  that removes the `import` and `from` statements which currently
  reference the about-to-be-deleted file's module name:

  - `import <deleted_module> [as alias]` — when this is the only alias in
    the statement, the whole statement is removed (the edit range covers
    the full statement line including its trailing newline). When the
    statement has additional surviving aliases (`import a, b` with `a`
    deleted), only the dead alias plus its adjacent comma is removed, so
    the surviving aliases stay intact.
  - `from <deleted_module> import …` — the whole statement is removed
    (every imported name's source module is gone). Both absolute and
    relative `from` lines are covered: relative imports are resolved
    against the importer's own package and matched against the deleted
    module.
  - `from <pkg> import <leaf> [as alias]` where
    `<pkg>.<leaf> == deleted_module` — when this is the only imported
    name in the statement, the whole statement is removed; otherwise
    only the dead leaf plus its adjacent comma is removed.

  Deletions where the path is outside the workspace, isn't a `.py` file,
  or is `__init__.py` (package delete — separate feature) are silently
  skipped; the request returns `null` when no edits are needed.
  Importers that are themselves part of the same delete batch are
  skipped (no point editing a file the client is about to remove).
  Multiple deletions in one request are batched against the *current*
  workspace state.

  New consumer-layer dataclass `FileDeletionEdit(path, start_line,
  start_character, end_line, end_character, new_text)` (all position
  fields 0-based, LSP-style; `new_text` is always `""`) and entrypoint
  `WorkspaceSession.import_edits_for_file_deletions(deletions)` accept an
  iterable of paths and return a tuple of edits sorted by `(path,
  start_line, start_character)`. Lives entirely on top of the stable
  `pyinc.integrations` public surface — no kernel contract change and no
  new integration-layer surface.
- **`workspace/willRenameFiles` in `pyinc-tools` LSP.** The server now
  advertises `workspace.fileOperations.willRename` with a `**/*.py` file
  filter and handles `workspace/willRenameFiles` requests. For each
  `{oldUri, newUri}` pair the server walks every Python file in the
  workspace and emits a `WorkspaceEdit` that updates the `import` and
  `from` statements which currently reference the renamed file's module
  name:

  - `import <old_module> [as alias]` — the dotted-module span is rewritten
    to `<new_module>`. Any `as` clause is preserved.
  - `from <old_module> import …` — the dotted-module span (including any
    leading dots) is rewritten. When the importer's relative anchor
    contains both the old and the new module, the existing `level` is
    preserved and only the relative tail changes; otherwise the statement
    is rewritten to absolute form (`from <new_module> import …`,
    `level == 0`).
  - `from <pkg> import <leaf> [as alias]` where `<pkg>.<leaf> == old_module`
    — the leaf is rewritten to `<new_module>`'s leaf when `old_module` and
    `new_module` share the same parent package. The `as` clause is left
    alone. Cross-directory submodule rewrites of this shape are
    intentionally skipped (they would require either rewriting every
    `<leaf>.attr` usage site or inserting an `as <leaf>` clause, neither of
    which is well-defined here).

  Renames where either path is outside the workspace, isn't a `.py` file,
  is `__init__.py` (package rename — separate feature), or produces an
  unchanged module name are silently skipped; the request returns `null`
  when no edits are needed. Multiple renames in one request are batched
  against the *current* workspace state (no chaining is attempted — a
  swap A↔B produces independent edits for each direction).

  New consumer-layer dataclass `FileRenameEdit(path, start_line,
  start_character, end_line, end_character, new_text)` (all position
  fields 0-based, LSP-style) and entrypoint
  `WorkspaceSession.import_edits_for_file_renames(renames)` accept an
  iterable of `(old_path, new_path)` pairs and return a tuple of
  edits sorted by `(path, start_line, start_character)`. Lives entirely
  on top of the stable `pyinc.integrations` public surface — no kernel
  contract change and no new integration-layer surface.
- **`textDocument/semanticTokens/range` in `pyinc-tools` LSP.** The server now
  advertises
  `semanticTokensProvider: {legend: {tokenTypes: [...], tokenModifiers: [...]},
  full: true, range: true}` (previously `range: false`) and implements the
  `textDocument/semanticTokens/range` request, returning a delta-encoded
  `SemanticTokens.data` payload for the slice of the document covered by the
  requested half-open LSP range `[params.range.start, params.range.end)`. The
  implementation reuses the same full-document AST walk as
  `textDocument/semanticTokens/full` and then filters by token start position:
  a token at `(line, character)` is retained iff its start position is `>=
  params.range.start` and `< params.range.end`. The retained tokens are then
  delta-encoded on their own — the running cursor is reset, so the first
  emitted token's `deltaLine` / `deltaStart` are absolute. No server-side
  per-document state is held; every `range` request is independent of the
  others and of any prior `full` request.

  New consumer-layer entrypoint
  `WorkspaceSession.semantic_tokens_range_for_file(path, start_line=0,
  start_character=0, end_line=None, end_character=0)` returns a tuple of
  `SemanticToken` dataclasses filtered to the same half-open range; omit
  `end_line` to scan from the start position through end-of-file. Coordinates
  are 0-based (LSP-style). Files that fail to parse return `()`; missing
  files raise `FileNotFoundError` from the consumer entrypoint and the LSP
  handler converts that to `{"data": []}`. The new method composes
  `semantic_tokens_for_file`, so it inherits all of that walk's existing
  classification rules and limitations (use-site classification covers only
  bare `ast.Name` lookups against the file's own `ModuleSymbolTable`;
  attribute access, function-local shadowing, and cross-module re-export
  following are out of scope, matching the existing `find_references` /
  `inlayHint` limitations).

  Both the `full` and the `range` LSP handlers share a single
  `_encode_semantic_tokens(tokens)` helper that produces the
  `[deltaLine, deltaStart, length, tokenType, tokenModifiers]` five-tuple
  wire encoding with `tokenModifiers` as a bitmask over the legend
  positions, so the two endpoints are guaranteed to encode equivalent
  tokens identically. `semanticTokens/full/delta` remains intentionally
  unimplemented — it is the only request shape that would require
  server-side per-document state, and re-sending the whole token stream on
  every change is fast enough that the bookkeeping cost is not justified.
  Lives entirely on top of the stable `pyinc.integrations` public surface
  — no kernel contract change and no new integration-layer surface.
- **`textDocument/semanticTokens/full` in `pyinc-tools` LSP.** The server now
  advertises
  `semanticTokensProvider: {legend: {tokenTypes: [...], tokenModifiers: [...]},
  full: true, range: false}` and returns a delta-encoded `SemanticTokens.data`
  array for the requested document. The legend's `tokenTypes` list is
  `["namespace", "class", "function", "method", "parameter", "variable"]`
  and `tokenModifiers` is `["declaration", "async"]`. The implementation
  parses the document (overlay or on-disk) once with `ast.parse` and walks
  the tree emitting one token per:
  - `def` / `async def` header — token type `"function"` (or `"method"` when
    nested inside a `ClassDef` body), modifier `"declaration"` (plus
    `"async"` for `async def`). The name span is located on the def's
    header line using the same word-boundary scan that
    `textDocument/rename` uses, so decorated definitions still report on
    the `def` line, not the decorator line.
  - `class` header — token type `"class"`, modifier `"declaration"`.
  - Each function parameter (posonly / positional / vararg / kwonly /
    kwarg slot, in that order) — token type `"parameter"`, modifier
    `"declaration"`. Parameter names are read from `ast.arg.col_offset`
    (which already points past any leading `*` / `**`).
  - Each bare `ast.Name` use (Load context) whose identifier matches a
    top-level entry in the file's `ModuleSymbolTable`. The token type
    follows the matched symbol's kind: `function`, `class`, `variable` /
    `class_variable` → `"variable"`, and `import_alias` → `"namespace"`.
    Dotted qualified-name entries (methods / nested classes), and
    `from_import_alias` / `wildcard_import_stub` entries are
    intentionally skipped from the use-site lookup — resolving them to
    their real kind would require cross-module hops; the editor's
    default highlighting handles those names. Function-local shadowing
    is not modeled (a local `foo` inside a function that shadows a
    top-level `foo` is still tagged with the top-level kind), mirroring
    the documented `find_references` / `inlayHint` limitation.

  The walk explicitly recurses into decorator lists, default-value
  expressions, parameter annotations, return annotations, and base /
  keyword-argument class headers, so a workspace-resolved decorator
  (`@my_decorator`), default (`= my_default`), or base class
  (`class Derived(Base):`) all light up with the appropriate token
  kind. Files that fail to parse return `{"data": []}`; missing files
  raise `FileNotFoundError` from the consumer entrypoint and the LSP
  handler converts that to `{"data": []}`.

  Tokens are encoded into the LSP wire format inside the LSP handler:
  each token contributes five integers `[deltaLine, deltaStart, length,
  tokenType, tokenModifiers]` where `deltaLine` is relative to the
  previous token's line, `deltaStart` is relative to the previous
  token's start column when both are on the same line (else absolute),
  and `tokenModifiers` is a bitmask over the legend positions. New
  consumer-layer entrypoint `WorkspaceSession.semantic_tokens_for_file(path)`
  returns a tuple of `SemanticToken(line, character, length, token_type,
  token_modifiers)` dataclasses with `line` / `character` 0-based
  (LSP-style); `token_type` is typed as `SemanticTokenType` (a `Literal`
  over the six legend names) and `token_modifiers` as
  `tuple[SemanticTokenModifier, ...]`. New public names re-exported
  from `pyinc_tools`: `SemanticToken`, `SemanticTokenType`,
  `SemanticTokenModifier`. Lives entirely on top of the stable
  `pyinc.integrations` public surface (composes `module_symbol_table`)
  — no kernel contract change and no new integration-layer surface.
- **`textDocument/inlayHint` in `pyinc-tools` LSP.** The server now
  advertises `inlayHintProvider: {resolveProvider: false}` and returns
  `InlayHint[]` for parameter-name hints at call sites inside the
  requested LSP range. The implementation walks the document's AST
  (overlay or on-disk) once with `ast.parse` and collects every
  `ast.Call` whose call-function span starts inside the requested range.
  Each call's callee is resolved through the same bare-`Name` /
  `Name.attr` resolver used by `callHierarchy/outgoingCalls`
  (`_resolve_call_target`), and the callee's signature is looked up
  through `_lookup_callable_signature` so class constructions surface
  `<Class>.__init__`'s parameters with the leading `self` / `cls`
  stripped — matching the convention already used by `signatureHelp`.
  For each positional argument the walker pairs it with the next
  positional parameter slot from `Signature.parameters` (walking
  posonly/positional entries, skipping `**kwargs`, and stopping at the
  first `*args` parameter since it absorbs the rest of the slots) and
  emits an `InlayHint` with `label = "<paramname>:"`, `kind = "parameter"`
  (LSP value `2`), and `paddingRight = True`. Hints are suppressed when
  the argument is itself a bare `Name` whose identifier equals the
  parameter name (the standard no-redundant-hint convention used by
  other Python language servers). Iteration also stops at the first
  `ast.Starred` argument in the call, since `*spread` consumes an
  unknown number of slots and the pairing becomes ambiguous after that
  point. Targets resolved as stdlib / installed / ambiguous / missing,
  calls whose callee shape is not a bare `Name` or `Name.attr`
  (subscripted calls `factory[T](...)`, deep attribute chains
  `pkg.subpkg.foo(...)`, `self.method(...)` / instance-attribute calls,
  lambdas), and files that fail to parse all return `[]`. New
  consumer-layer entrypoint `WorkspaceSession.inlay_hints_for_file(path,
  start_line=0, start_character=0, end_line=None, end_character=0)`
  returns a tuple of `InlayHint(line, character, label, kind,
  padding_left, padding_right)` dataclasses with `line` / `character`
  0-based (LSP-style) and `kind` typed as
  `Literal["parameter", "type"]` — only `"parameter"` is emitted in this
  release; `"type"` is reserved for future variable-type / return-type
  hints. Omit `end_line` to scan the whole file. New public names
  re-exported from `pyinc_tools`: `InlayHint`, `InlayHintKind`. Lives
  entirely on top of the stable `pyinc.integrations` public surface
  (composes `resolve_symbol` and `module_symbol_table` via the existing
  call-target resolver and signature lookup) — no kernel contract
  change and no new integration-layer surface.
- **`pyinc-tools` LSP `serverInfo.version`** bumped from `"2.0.0"` to
  `"2.1.0"` to align with the kernel version pinned in `pyproject.toml`.
- **Call hierarchy in `pyinc-tools` LSP.** The server now advertises
  `callHierarchyProvider: true` and implements all three call-hierarchy
  methods: `textDocument/prepareCallHierarchy`,
  `callHierarchy/incomingCalls`, and `callHierarchy/outgoingCalls`.
  `prepareCallHierarchy` resolves the identifier under the cursor through
  `symbol_resolution.resolve_symbol`; when the target is a workspace
  `function`, `method`, or `class`, it returns a single `CallHierarchyItem`
  whose `range` covers the whole def block (including decorator lines if
  any), whose `selectionRange` is the bare-name span on the header line,
  and whose `data` field carries `{"path", "qualified_name"}` so the
  incoming/outgoing follow-up calls do not need to re-resolve the cursor.
  Variables, import aliases, `from_import` aliases, wildcard-import stubs,
  and stdlib / installed / ambiguous / missing targets return `null`.
  `incomingCalls` runs `find_references(include_declaration=False)` on the
  item's target and groups references by their innermost enclosing
  workspace-known def/class in the same file. The qualifier follows
  `module_symbol_table`'s ClassDef-only nesting (a reference inside
  `class C: def m(self): ...` is attributed to `C.m`); references inside a
  nested function body bubble up to the next enclosing function or class
  method that's in the symbol table, and module-top-level references are
  dropped because there is no caller item to attribute them to.
  `outgoingCalls` parses the declaring file, locates the
  `def` / `async def` / `class` matching the item's qualified name, and
  walks its body for `ast.Call` nodes — without descending into nested
  `FunctionDef` / `AsyncFunctionDef` / `ClassDef` / `Lambda` scopes, each
  of which owns its own outgoing-call list. Bare `Name(id=name)` calls are
  resolved against the declaring module's imports; `Name.attr` calls are
  resolved by first looking up the LHS as a workspace module and then
  resolving `attr` inside that module (mirroring `find_references`'s
  LHS-bare-Name handling). Subscripted calls (`factory[T](...)`), deep
  attribute chains (`pkg.subpkg.foo(...)`), `self.method(...)` /
  instance-attribute calls, and lambda calls produce no callee. New
  consumer-layer entrypoints
  `WorkspaceSession.prepare_call_hierarchy(path, line, character)`,
  `WorkspaceSession.call_hierarchy_incoming_calls(path, qualified_name)`,
  and `WorkspaceSession.call_hierarchy_outgoing_calls(path, qualified_name)`
  return tuples of `CallHierarchyItem`,
  `CallHierarchyIncomingCall(caller, call_sites)`, and
  `CallHierarchyOutgoingCall(callee, call_sites)` dataclasses with 0-based
  LSP-style range fields. New public names re-exported from `pyinc_tools`:
  `CallHierarchyItem`, `CallHierarchyItemKind`, `CallHierarchyCallSite`,
  `CallHierarchyIncomingCall`, `CallHierarchyOutgoingCall`. Lives entirely
  on top of the stable `pyinc.integrations` public surface (composes
  `resolve_symbol`, `module_symbol_table`, and `find_references`) — no
  kernel contract change and no new integration-layer surface.
- **`textDocument/typeDefinition` in `pyinc-tools` LSP.** The server now
  advertises `typeDefinitionProvider: true` and returns `Location[]` for the
  type-definition site(s) of the symbol under the cursor. The implementation
  resolves the cursor's identifier to its declaring `Symbol` via the existing
  `resolve_symbol` pipeline (so the user can stand on either the declaration
  site or a same-name use site inside the declaring module), reads the
  declared annotation (variable / class-variable `annotation`, or function /
  method `signature.return_annotation`), parses it as a Python expression,
  and walks the result for `Name` and `Attribute(value=Name(...), attr=...)`
  nodes. Each name is resolved against the declaring module — bare `Name`
  references through that module's imports, and `lhs.attr` references by
  first resolving `lhs` to a workspace module and then resolving `attr`
  inside that module — so generics (`list[Foo]`), unions (`Foo | Bar`), and
  qualified attribute types (`pkg.Foo`, `helper.Foo | helper.Bar`) all yield
  one location per workspace-resolved type, deduplicated by `(path, lineno)`.
  Whole-string forward references (`x: "Foo"`, `def f() -> "Foo"`) are
  unwrapped exactly once before walking; partial string annotations
  (`x: "Foo" | None`) are not unwrapped and the string portion contributes
  no location. Classes are themselves the type, so clicking on a class name
  returns its own definition location. Stdlib / installed / ambiguous type
  names (`int`, `list`, `typing.Optional`, etc.) are skipped via the
  existing resolver classification; import aliases, `from_import` aliases,
  wildcard-import stubs, unannotated variables and functions, and
  non-workspace targets return `[]`. Attribute chains whose LHS is not a
  bare `Name` (`pkg.subpkg.Foo`) are skipped, mirroring the resolver's
  existing limitation for references. New consumer-layer entrypoint
  `WorkspaceSession.type_definitions_at(path, qualified_name)` returns a
  tuple of `TypeDefinitionLocation(path, lineno, col_offset, end_col_offset)`
  dataclasses with `lineno` as the 1-based AST lineno (the LSP layer
  subtracts 1) and `(col_offset, end_col_offset) = (0, 1)` matching the
  existing `textDocument/definition` shape. New public name re-exported
  from `pyinc_tools`: `TypeDefinitionLocation`. Lives entirely on top of
  the stable `pyinc.integrations` public surface (`resolve_symbol`,
  `module_symbol_table`) — no kernel contract change and no new
  integration-layer surface.
- **`textDocument/codeLens` in `pyinc-tools` LSP.** The server now advertises
  `codeLensProvider: {resolveProvider: false}` and returns one reference-count
  `CodeLens` above every top-level `def` / `async def` / `class` in the
  requested document. For each top-level symbol of kind `function` or `class`
  (dotted-name nested classes and methods are excluded — `find_references`
  does not reliably resolve attribute calls on instances), the implementation
  locates the bare-name identifier range on the definition's header line
  using the same `_locate_def_class_name_offsets` helper that
  `find_document_highlights` uses, then calls `find_references` with
  `include_declaration=False` to count the workspace references and emits a
  `CodeLens` whose `command` is `{title: "<N> reference[s]", command: ""}`
  (no clickable action — matching the convention used by other Python LSP
  servers so the lens text appears above the definition without binding to
  an editor-specific command). Non-workspace targets, unparseable files,
  and files with no qualifying symbols return `[]`, mirroring how other LSP
  requests degrade. Decorated definitions report the lens on the `def` line,
  not the decorator line. New consumer-layer entrypoint
  `WorkspaceSession.code_lenses_for_file(path)` returns a tuple of
  `CodeLens(start_line, start_character, end_line, end_character, title)`
  dataclasses with all four position fields 0-based (LSP-style). New public
  name re-exported from `pyinc_tools`: `CodeLens`. Lives entirely on top of
  the stable `pyinc.integrations` public surface (composes
  `module_symbol_table` and `find_references`) — no kernel contract change
  and no new integration-layer surface.
- **`textDocument/documentLink` in `pyinc-tools` LSP.** The server now
  advertises `documentLinkProvider: {resolveProvider: false}` and returns
  `DocumentLink[]` for the requested document. The implementation walks the
  AST of the document (overlay or on-disk) and pairs every `ast.alias` whose
  enclosing `Import` / `ImportFrom` resolves to a workspace file with a
  link spanning the alias's AST `(col_offset, end_col_offset)` range. For
  `import M` and `import M as alias` the linked span covers the whole
  `M [as alias]` clause and points at the resolved module file; for
  `from M import a, b` each imported name is linked individually to its
  own resolved path — which for a submodule (`from pkg import child`)
  is the submodule file, not `pkg/__init__.py`. Stdlib, installed,
  missing, ambiguous, and wildcard (`from M import *`) targets emit no
  link, matching the LSP's existing scope of navigating only to
  workspace-resolved targets. Files that fail to parse return `[]`,
  mirroring how other LSP requests degrade on syntax errors. Imports
  inside `if TYPE_CHECKING:` / `try: ... except ImportError:` guard blocks
  are linked since `resolved_imports_for_file` walks into both. New
  consumer-layer entrypoint `WorkspaceSession.document_links_for_file(path)`
  returns a tuple of `DocumentLink(start_line, start_character, end_line,
  end_character, target_path)` dataclasses with all four position fields
  0-based (LSP-style) and `target_path` already remapped from the mirror
  root to the real workspace root. New public name re-exported from
  `pyinc_tools`: `DocumentLink`. Lives entirely on top of the stable
  `pyinc.integrations` surface — no kernel contract change and no new
  integration-layer surface.
- **`textDocument/selectionRange` in `pyinc-tools` LSP.** The server now
  advertises `selectionRangeProvider: true` and returns one `SelectionRange`
  chain per requested position, encoded innermost-first via the recursive
  `parent` field. The chain is computed by parsing the document (overlay or
  on-disk) once with `ast.parse`, collecting every AST node whose
  `(lineno, col_offset)`–`(end_lineno, end_col_offset)` span contains the
  cursor, deduplicating identical spans, and reducing the candidates to a
  strict containment chain ordered by length so each parent is strictly
  larger than its child. The cursor offset is computed against a precomputed
  table of line starts so multi-line spans (function bodies, class bodies,
  multi-statement blocks) are mapped correctly. Files that fail to parse,
  positions outside the source, or positions that no AST node covers all
  fall back to a single zero-width range at the cursor so the LSP result
  length always matches `params.positions` length. New consumer-layer
  entrypoint `WorkspaceSession.selection_ranges_at(path, line, character)`
  returns a flat tuple of `SelectionRange(start_line, start_character,
  end_line, end_character)` dataclasses with all four fields 0-based
  (LSP-style); the LSP handler threads that flat tuple into the recursive
  `parent` shape. New public name re-exported from `pyinc_tools`:
  `SelectionRange`. Lives entirely on top of the stable `pyinc.integrations`
  surface — no kernel contract change and no new integration-layer surface.
- **`textDocument/foldingRange` in `pyinc-tools` LSP.** The server now
  advertises `foldingRangeProvider: true` and returns `FoldingRange[]` for the
  requested document. The implementation parses the file's source (overlay or
  on-disk) once with `ast.parse` and walks the tree for foldable spans:
  every `def` / `async def` / `class` block becomes a `region` fold whose
  `startLine` is the header line (or the first decorator line if any
  decorators are attached) and whose `endLine` is the AST `end_lineno`,
  recursing into class bodies so methods fold independently of their
  enclosing class. In addition, runs of consecutive top-level
  `import` / `from … import` statements are coalesced into a single
  `imports` fold spanning the first to the last line of the run; multi-line
  parenthesised imports (`from x import (\n    a,\n    b,\n)`) collapse on
  their own. Single-line definitions and single-line single imports emit no
  fold (a fold of one line is a no-op for the editor). Files that fail to
  parse return `[]`, mirroring how other LSP requests degrade on syntax
  errors. The LSP `kind` field is omitted for generic `region` folds and
  emitted as `"imports"` for the import-group case so older clients that
  only recognise `"imports"` / `"comment"` still work. New consumer-layer
  entrypoint `WorkspaceSession.folding_ranges_for_file(path)` returns a tuple
  of `FoldingRange(start_line, end_line, kind)` dataclasses with `kind` typed
  as `Literal["imports", "comment", "region"]` (1-based AST linenos so the
  shape matches sibling entrypoints like `find_document_highlights`); the
  LSP layer subtracts 1 to produce the LSP 0-based `startLine` / `endLine`.
  New public names re-exported from `pyinc_tools`: `FoldingRange`,
  `FoldingRangeKind`. Lives entirely on top of the stable `pyinc.integrations`
  surface — no kernel contract change and no new integration-layer surface.
- **`textDocument/signatureHelp` in `pyinc-tools` LSP.** The server now
  advertises `signatureHelpProvider: {triggerCharacters: ["(", ","],
  retriggerCharacters: [","]}` and returns a `SignatureHelp` payload for the
  call expression enclosing the cursor. A forward source scanner skips
  comments and string literals (single, double, and triple-quoted) and tracks
  a stack of open brackets; the topmost open `(` whose preceding token is a
  usable identifier identifies the function being called, and the
  accumulated comma count yields `activeParameter`. `def name(` and
  `class Name(` definition headers and Python-keyword-prefixed `(` are
  rejected so the cursor never lands on a non-call site. The detected
  identifier is resolved through the existing
  `symbol_resolution.resolve_symbol` pipeline (so cross-module re-exports
  hop through transparently); only workspace-resolved targets produce a
  signature. Functions surface their declared `Signature` directly; classes
  surface `<Class>.__init__`'s signature with a leading `self`/`cls`
  parameter stripped, or an empty constructor signature when no `__init__`
  is defined. Stdlib/installed/ambiguous targets, attribute calls
  (`obj.method(`), subscripted calls (`factory[T](`), and same-file calls
  whose enclosing `(` is still unclosed (which makes the file unparseable
  for symbol extraction) all return `null`. Each signature reports
  parameters as LSP `[start, end]` substring offsets into the signature
  label so editors can highlight the active parameter precisely. New
  consumer-layer entrypoint `WorkspaceSession.signature_help_at(path, line,
  character)` returns a `SignatureHelp(label, parameters,
  active_parameter)` dataclass with `parameters` typed as
  `tuple[SignatureParameterInfo, ...]`. New public names re-exported from
  `pyinc_tools`: `SignatureHelp`, `SignatureParameterInfo`. Lives entirely
  on top of the stable `pyinc.integrations` public surface — no kernel
  contract change and no new integration-layer surface.
- **`textDocument/documentHighlight` in `pyinc-tools` LSP.** The server now
  advertises `documentHighlightProvider: true` and returns
  `DocumentHighlight[]` ranges for the symbol under the cursor, scoped to the
  current file. The declaration site is reported with `kind: 3` (Write); all
  other occurrences with `kind: 1` (Text). The synthetic
  `(col=0, end_col=1)` placeholder that `find_references` emits for
  `def` / `class` / `async def` declaration lines is repaired by locating the
  real identifier offset on the line (the same repair already used by
  `textDocument/rename`), so editors highlight the actual identifier rather
  than the first character of the line. Cross-file references that
  `find_references` would return are intentionally filtered out — workspace-
  wide highlighting remains `textDocument/references`'s job. Stdlib /
  installed / ambiguous targets return `[]`. New consumer-layer entrypoint
  `WorkspaceSession.find_document_highlights(path, qualified_name)` returns a
  tuple of `DocumentHighlight(lineno, col_offset, end_col_offset, kind)`
  dataclasses with `kind` typed as `Literal["text", "read", "write"]`.
  New public names re-exported from `pyinc_tools`: `DocumentHighlight`,
  `DocumentHighlightKind`. Lives entirely on top of the stable
  `pyinc.integrations.find_references` entrypoint — no kernel contract change
  and no new integration-layer surface.
- **`find_references` (and rename) now follow `import M; M.foo()` attribute
  access.** Previously the resolver was strictly name-local, so attribute
  access on an `import` binding (`import a; a.foo()`,
  `import a as alias; alias.foo()`) returned no references and rename did
  not rewrite the call site — both limitations were documented in
  `docs/pyinc-tools-guide.md` and pinned by a regression test. The
  occurrence walker in `symbol_resolution._collect_name_occurrences` now
  carries the LHS Name's `id` as an internal verification hint on every
  `Attribute(value=Name(...), attr=...)` occurrence (a 5th element added
  to the internal `NameOccurrencePayload`; not part of the public surface),
  and `find_references_payload` routes hint-bearing occurrences through a
  two-step verification: resolve the LHS through its `import_alias` /
  `from_import_alias` to a workspace module, then resolve the attribute
  inside that module so cross-module re-exports (`from c import foo`)
  hop through transparently. Only the rightmost-attribute span is
  reported, so rename rewrites just the attribute portion (the leading
  `M.` / `alias.` is left intact). The same hint flows out of the
  forward-reference string-annotation walker, so `def g(x: 'a.Foo')` is
  also covered. Attribute access whose LHS is itself an Attribute (e.g.
  `import pkg.subpkg; pkg.subpkg.foo()`) is still not counted; that
  remains a documented limitation. No kernel contract change; integration
  public surface unchanged.

### Fixed

- **`FileDeletionEdit` is now re-exported from `pyinc_tools`.** The dataclass
  was added to `pyinc_tools.session` alongside
  `WorkspaceSession.import_edits_for_file_deletions` in the previous PR but
  was missing from `pyinc_tools/__init__.py`'s re-export list, so consumers
  who imported it from the top-level package (matching the precedent set by
  `FileRenameEdit` and every other consumer-layer dataclass) saw an
  `ImportError`. The symbol is now in both the module-level imports and
  `__all__`.

## [2.1.0] - 2026-05-05

### Added

- **Rename now rewrites relative `from … import` lines.** The
  `WorkspaceSession.rename_symbol` import-edit walker resolves `from .pkg
  import name`, `from .. import name`, and `from ..sub.pkg import name`
  forms against each importer's package and rewrites them when the
  resolved absolute module matches `target.defining_module`. The
  `as <alias>` clause is preserved exactly as in the absolute-import case.
  Module-level (`__init__.py`) importers are anchored on the package
  itself; non-package modules are anchored on their parent. Resolves the
  documented v2.0.x rename limitation that relative imports were not
  rewritten. `pyinc_tools`-only change; the kernel and the
  `pyinc.integrations` public surface are unchanged.
- **`textDocument/rename` (and `textDocument/prepareRename`) in `pyinc-tools`
  LSP.** The server now advertises
  `renameProvider: {prepareProvider: true}`. `prepareRename` returns the range
  of the identifier under the cursor and a placeholder when the symbol resolves
  to a workspace target (otherwise `null`). `rename` returns a `WorkspaceEdit`
  with `changes` keyed by document URI. Edits cover (a) every `Name` /
  `Attribute` occurrence already produced by
  `symbol_resolution.find_references`; (b) the `def`/`class`/`async def`
  declaration site (the `find_references` synthetic placeholder is repaired by
  locating the actual identifier offset in the source line); and (c) every
  `from <defining_module> import <bare_old> [as <alias>]` line in the
  workspace, with only the source-name part rewritten so any `as <alias>`
  clause is preserved. Invalid identifiers (`"1bad"`, `""`) and Python
  keywords (`"class"`, `"return"`) yield a JSON-RPC `RequestFailed` (-32803)
  error with a human-readable message; renaming a symbol via an
  `import ... as` alias (e.g. clicking on `aliased` in
  `from a import foo as aliased`) is refused with a `RequestFailed` error
  directing the user to rename the canonical name instead. Same-name and
  non-workspace targets return `null`. The consumer-layer entrypoint
  `WorkspaceSession.rename_symbol(path, qualified_name, new_name)` returns a
  structured `RenameResult(target, edits, status)` carrying the target's
  `ResolvedSymbol`, a tuple of `RenameEdit(path, lineno, col_offset,
  end_col_offset, new_text)`, and one of the statuses `"ok"`,
  `"non_workspace_target"`, `"invalid_identifier"`, `"keyword_identifier"`,
  `"same_name"`, or `"alias_rename_unsupported"`. New public names re-exported
  from `pyinc_tools`: `RenameEdit`, `RenameResult`, `RenameStatus`. Lives
  entirely on top of the stable `pyinc.integrations` surface — no kernel
  contract change.

## [2.0.1] - 2026-04-29

### Added

- **Forward-reference string annotations are now scanned for references.**
  `symbol_resolution.find_references` (and the LSP `textDocument/references`
  it backs) now detects names inside forward-reference strings such as
  `def g(a: 'Foo')`, `x: 'list[Foo]'`, `x: 'pkg.Foo'`, and `'Foo | None'`.
  Internally, `name_occurrences_for_file` performs a second pass over the
  annotation slots `AnnAssign.annotation`, `arg.annotation`, and
  `FunctionDef`/`AsyncFunctionDef.returns`, re-parses string-valued
  `ast.Constant` nodes via `ast.parse(value, mode="eval")`, and emits the
  inner `Name`/`Attribute` references with offsets translated back to file
  coordinates. Each new occurrence flows through the same
  `resolve_symbol_payload` verification used for bare `Name`/`Attribute`
  references, so workspace-only filtering, `MAX_FOLLOW_DEPTH`, and
  `if TYPE_CHECKING:` / `try: except ImportError:` guard handling all
  carry through unchanged. String annotations that span multiple lines,
  are triple-quoted, contain escape sequences, or use implicit string
  concatenation are skipped (offset reconstruction would be ambiguous);
  malformed annotation strings are silently ignored. No payload shape or
  public surface change.

## [2.0.0] - 2026-04-25

This is the v2.0.0 release. v1.2.1 was the last v1 release. Items previously
listed under "Version 1 did not include" in `docs/architecture.md` are
resolved here, except for the still-deferred *schedulers or worker pools*.

### Added

- **`notebook` integration (Jupyter `.ipynb`).** New stable integration
  `pyinc.integrations.notebook` exposes `notebook_analysis(db, path)` and
  `workspace_notebook_analysis(db, root)` plus the dataclasses
  `NotebookAnalysis`, `NotebookCell`, and `NotebookDiagnostic`. Code cells'
  Python source is concatenated and parsed via `ast` to surface module-level
  imports and definitions per cell, with cutoff-based backdating on the
  parsed structure (whitespace-only / output-only edits are backdated and
  do not invalidate downstream consumers). Markdown and raw cells are
  preserved with their first-line heading (markdown) or kind tag.
  Stdlib-only — uses `json` to decode the notebook envelope; no `nbformat`
  dependency. Resolves the v1 architectural non-goal "notebook integration".
- **Push observers in the kernel.** New `Database.observe(callback, query,
  *args, **kwargs) -> Subscription` registers a callback that fires when the
  identified query node's stored value changes (decision `"executed"`).
  Backdated and reused decisions do not fire — the stored value did not move.
  Events are delivered as `QueryChangeEvent` frozen dataclasses carrying
  `query_id`, `args_digest`, `decision`, `changed_at`, and `verified_at`.
  Dispatch runs after the outermost request scope completes and the kernel
  lock is released, so a callback may safely call back into the database;
  callback-level exceptions are routed to an optional
  `Database(observer_error_hook=...)` hook (default: a one-line stderr log)
  and do not suppress sibling callbacks. `Subscription.unsubscribe()` detaches
  a callback and is idempotent. New public names re-exported from `pyinc`:
  `QueryChangeEvent`, `Subscription`, `ObserverCallback`, `ObserverErrorHook`.
  Resolves the v1 architectural non-goal "push observers in the kernel".
- **Mutable object graphs across cached boundaries.** `freeze` / `thaw` now
  memoize shared object identity and reconstruct cyclic structures via the
  new `FrozenGraph(nodes, root)` envelope and `FrozenRef(index)` pointer
  snapshot variants. Previously the boundary raised
  `UnsupportedValueError("Cyclic values cannot cross cached boundaries.")`
  and silently dropped shared identity. Pure-tree inputs continue to produce
  the v1 flat snapshot shape (zero overhead in the common case); only inputs
  with actual sharing or cycles are wrapped in `FrozenGraph`. `thaw` runs a
  two-pass allocate-then-fill so a list-with-itself round-trips to an actual
  self-referential list and shared sub-objects retain identity. Resolves the
  v1 architectural non-goal "arbitrary mutable object graphs across cached
  boundaries". New public names re-exported from `pyinc`: `FrozenGraph`,
  `FrozenRef`.
- **Content-addressed artifact storage.** New `ArtifactStore` Protocol and
  two shipped implementations: `InMemoryArtifactStore` (dict-backed) and
  `FileSystemArtifactStore` (git-style two-character fan-out under
  `<root>/objects/<digest[:2]>/<digest[2:]>` with atomic `tempfile`+`os.replace`
  writes). `Database(store=...)` writes the serialized snapshot bytes for
  every value crossing the membrane, keyed by the `fingerprint_snapshot`
  digest. New `serialize_snapshot(snapshot)` and `deserialize_snapshot(payload)`
  helpers expose the byte form to external callers; both round-trip the full
  snapshot grammar including `FrozenGraph` / `FrozenRef`. Cross-run cache
  reuse is delivered via the durable checkpoint API:
  `Database.save_checkpoint(store=None) -> str` serialises all current query
  and resource node records (plus their dependency edges and snapshot bytes)
  to an `ArtifactStore` and returns a content-addressed checkpoint key
  prefixed with `"ck"`. A subsequent `Database.load_checkpoint(key, store=None)`
  in a fresh process reads the manifest back, verifies that all declared
  input digests and resource probe hints still match, and pre-warms the node
  record cache so that the next `db.get(query)` reuses the stored result
  without re-executing the query function. If any dependency is stale the
  affected query is silently re-executed and the new result is compared
  against the stored snapshot for backdating (from-scratch consistency is
  maintained). Both methods accept an optional `store=` kwarg for call-site
  store injection; `save_checkpoint` also writes all referenced snapshot
  bytes to the store, making it self-contained. The checkpoint key is
  content-addressed: identical database state always produces the same key.
  New public names re-exported from `pyinc`: `ArtifactStore`,
  `InMemoryArtifactStore`, `FileSystemArtifactStore`, `serialize_snapshot`,
  `deserialize_snapshot`. Resolves the v1 architectural non-goal
  "content-addressed artifact storage".
- **`try/except ImportError` import support.** `symbol_resolution` now
  recognises `try: … except ImportError:` and `try: … except
  ModuleNotFoundError:` (and the tuple form `except (ImportError,
  ModuleNotFoundError):`) guard blocks at the module top level and walks
  their bodies for `import` and `from … import` statements. The collected
  symbols appear in `ModuleSymbolTable.symbols` with the existing
  `import_alias` / `from_import_alias` kinds, exactly as if the imports
  were unconditional. The "conditional top-level binding" impurity marker
  is no longer recorded for files whose only conditional blocks are
  recognised import-error guards. `python_source` likewise collects import
  statements and bound names from such blocks, so that
  `import_statements_for_file` and the module binding analysis agree with
  the symbol table. Bare `except:` handlers (and handlers for other
  exception types) still set the impurity marker.
- **Kernel digest format bump (`K2;`).** The `fingerprint_snapshot` encoder
  prefixes its byte form with `K2;` so older `K1;` / unprefixed payloads in
  any external durable cache cannot be silently accepted. In-memory state
  across a process restart is unaffected. This is the standard
  encoder-change-requires-identity-bump path documented in
  `docs/kernel-contract.md`.

### Changed

- **Value boundary preserves shared identity.** When the same mutable
  container appears at two slots of an input value, both reads from the
  thawed copy in `checked` / `fast` mode now refer to the same Python object
  rather than two independent copies. This is consistent with the new mutable
  graph support. The kernel's stored snapshot remains immutable and safe; the
  mode table (strict / checked / fast) is unchanged. Tests that previously
  asserted v1's silent identity-drop behavior have been split: the
  v1-shaped *independent inputs* test continues to verify that two separately
  constructed dicts thaw independently, and a new companion test exercises
  the v2 *shared input* case explicitly.
- **`docs/kernel-contract.md` limitation #4 amended** to describe the
  outbound `ArtifactStore` and the durable `save_checkpoint` /
  `load_checkpoint` flow.
- **`pyinc-tools` LSP `serverInfo.version`** bumped from `"1.2.0"` to
  `"2.0.0"` to align with the kernel.

### Documentation

- Updated `docs/integration-authoring.md` line citations into
  `python_source.py` to the current line numbers.
- Removed the phantom v1.3.0 reference in `docs/pyinc-tools-guide.md`; the
  features described there shipped across v1.2.0 and v1.2.1 and continue in
  v2.0.0.
- Fixed `docs/pyinc-tools-guide.md` to list `try: … except ImportError:` guard
  blocks under "Supported" (they were added to the `symbol_resolution` walker in
  this release) and removed them from the "Not supported" conditional-blocks
  bullet, which now correctly names only `if sys.version_info >= …` style guards.
- Updated `docs/architecture.md` "Scope" section to replace the crossed-out
  development-cycle tracking list with a clean summary of what v2.0.0 resolved.
- Added `examples/checkpoint_demo.py` showing `save_checkpoint` /
  `load_checkpoint` cross-run cache reuse with `FileSystemArtifactStore`: three
  simulated runs demonstrating cold execution, full checkpoint reuse, and partial
  reuse when one input changes.

## [1.2.1] — 2026-04-24

### Added

- **`if TYPE_CHECKING:` import support.** `symbol_resolution` now recognises
  `if TYPE_CHECKING:` and `if typing.TYPE_CHECKING:` guard blocks at the
  module top level and walks their bodies for `import` and `from … import`
  statements. The collected symbols appear in `ModuleSymbolTable.symbols` with
  the existing `import_alias` / `from_import_alias` kinds, exactly as if the
  imports were unconditional. As a result, LSP hover and goto-definition work
  for names that are referenced as bare identifiers (e.g. `x: Foo`) even when
  the binding lives under a `TYPE_CHECKING` guard. The "conditional top-level
  binding" impurity marker is no longer recorded for files whose only
  conditional blocks are `TYPE_CHECKING` guards; other conditional blocks (e.g.
  `if sys.version_info >= …`) still set the marker. Non-import statements inside
  a `TYPE_CHECKING` block (unusual) are silently skipped rather than being
  promoted to the symbol table.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Minor version bump reflects new
  behaviour in the `symbol_resolution` integration, which is part of the stable
  `pyinc.integrations` public surface.
- Remaining `find_references` limitation: forward-reference strings (`'Foo'` in
  annotations) are not scanned during the AST name-occurrence walk, so
  string-annotation usages are not included in reference results.

## [1.2.0] — 2026-04-22

### Added

- **`textDocument/references`.** `pyinc-tools lsp` now advertises
  `referencesProvider` and honors `context.includeDeclaration`. References
  are returned with per-occurrence character ranges (`col_offset` /
  `end_col_offset` from the AST, not the line-0 placeholder used for some
  other requests), so editors can highlight every match precisely.
- **`pyinc.integrations.find_references`.** New stable entrypoint +
  `Reference` and `ReferenceQueryResult` dataclasses. Backed by two new
  composition-layer `@query` functions in `symbol_resolution`:
  `name_occurrences_for_file` (full-AST `Name`/`Attribute` walk) and
  `workspace_name_occurrence_index`. Candidate filtering is bounded by a
  bare-name pre-filter; each surviving candidate is verified through
  `resolve_symbol_payload`, so results respect the existing
  `MAX_FOLLOW_DEPTH = 8` cross-module re-export semantics. Only
  workspace-resolved targets are indexed; `stdlib`/`installed`/`ambiguous`
  targets return an empty tuple with the `ResolvedSymbol` carried on the
  result.
- **`WorkspaceSession.find_references`.** Mirror-path aware wrapper around
  the integration entrypoint; paths in the returned `Reference` tuples are
  remapped to the real workspace root.
- **Threaded live polling.** `PollingWorkspaceWatcher.start(on_change, *,
  interval_s, on_error)` spawns a daemon thread that delivers debounced
  change batches to a caller-supplied callback; `stop(timeout=5.0)` joins
  the thread cleanly. Context-manager support (`with watcher: ...`)
  guarantees `stop()` on exit. Exceptions from `on_change` are forwarded to
  the optional `on_error` hook, or logged to stderr by default, without
  killing the watcher thread. `poll()` remains available for synchronous
  use but raises `RuntimeError` while the thread is running (one driver at
  a time).
- **LSP live polling.** `pyinc-tools lsp` starts a threaded
  `PollingWorkspaceWatcher` in `initialize` by default so external file
  changes (e.g. `git pull`, formatter scripts) publish fresh diagnostics
  without requiring `workspace/didChangeWatchedFiles` from the editor.
  Opt-out via `initializationOptions.pyinc.watcher.enabled=false`; tune via
  `pyinc.watcher.debounceMs` and `pyinc.watcher.intervalMs`. Repeated
  `publishDiagnostics` for an unchanged URI are suppressed via a
  diagnostic-tuple signature cache.
- **CLI `--poll-interval-ms` flag.** Explicit control over watcher poll
  cadence. `pyinc-tools analyze --watch` now drives its loop through the
  threaded watcher API; behavior is unchanged.

### Changed

- **`WorkspaceSession` is thread-safe for its own public surface.** A
  session-level `threading.RLock` now guards `set_overlay`, `clear_overlay`,
  `refresh_paths`, `analyze_file`, `analyze_workspace`,
  `resolve_symbol_reference`, and `find_references`; mutators raise
  `RuntimeError` once `close()` has been called so the watcher thread
  exits cleanly when the session shuts down. The kernel's existing
  `Database` `RLock` is unchanged.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Minor version bump reflects new
  public consumer-layer API surface only. Watcher loops and LSP wiring
  remain architectural non-goals for the kernel itself; all new code lives
  in `pyinc_tools` on top of stable `pyinc.integrations` entrypoints.
- Known limitations for `find_references` in v1.2.0:
  - References via attribute access to a module-level symbol only
    imported as a module (`import a; a.foo()`) are not counted because
    the resolver is name-local. Use `from a import foo` to opt in.
  - Forward-reference strings (`'Foo'` in annotations) are not scanned.
  - Function-local shadowing is not modeled: a local `foo = 1` inside a
    function is still reported as a reference to a module-level `foo`.
    `symbol_resolution` is module/class-scope only per
    `docs/integration-contract.md`.

## [1.1.1] — 2026-04-22

### Added

- **`docs/pyinc-tools-guide.md`.** Consumer-facing guide covering install,
  `pyinc-tools analyze` (one-shot + `--watch`), `pyinc-tools lsp` (stdio +
  advertised capabilities), editor wiring (Neovim, Emacs/eglot, VS Code note),
  the `WorkspaceSession` overlay model, a supported-vs.-not-yet table, and
  troubleshooting. Cross-linked from `README.md`.
- **LSP hardening tests.** Added coverage for single-level wildcard goto-def,
  the `MAX_FOLLOW_DEPTH = 8` boundary, cyclic re-exports returning
  `ambiguous`, ambiguous wildcard lookups, the full eight-kind
  `documentSymbol` surface, and the current `if TYPE_CHECKING:` limitation.

### Notes

- Kernel contract (`src/pyinc`) unchanged. Patch-level release: docs and test
  coverage only.

## [1.1.0] — 2026-04-21

### Added

- **LSP hover and goto-definition.** `pyinc-tools lsp` now advertises
  `hoverProvider` and `definitionProvider`. Hover returns a markdown signature
  for the symbol under the cursor (functions with parameters and return
  annotation, classes, annotated variables, re-exported aliases);
  goto-definition follows cross-module re-exports via
  `symbol_resolution.resolve_symbol` and returns a `Location` in the defining
  module.
- **WorkspaceSession API.** `resolve_symbol_reference(path, qualified_name)`
  wraps `resolve_symbol` with mirror-root → real-root path remapping.
  `source_text(path)` returns the active overlay or on-disk contents for a
  tracked file.

### Notes

- Kernel contract (`src/pyinc`) is unchanged; the minor version bump reflects
  new public API on the `pyinc_tools` consumer layer. LSP wiring and
  push-based watchers remain architectural non-goals for the kernel itself;
  they live in `pyinc_tools` on top of stable `pyinc.integrations`
  entrypoints.

## [1.0.1] — 2026-04-21

### Added

- **Consumer tooling.** New `pyinc_tools` layer with a mirror-workspace
  `WorkspaceSession`, polling/debounce watcher support, `pyinc-tools analyze`,
  and `pyinc-tools lsp`, all kept outside `src/pyinc` so the kernel contract
  stays stable.
- **Examples.** Focused diagnostics/escape-hatch examples for
  `inspect_fresh(...)`, `explain_query_captures(...)`, and
  `report_untracked_read(...)`.

### Changed

- **Docs.** Reconciled the stable v1.x release story across `AGENTS.md`,
  `README.md`, `docs/architecture.md`, and `docs/integration-contract.md`.
- **Runtime diagnostics.** Unsupported ambient-capture failures now point users
  to `pyinc.explain_query_captures(...)` for preflight inspection.

## [1.0.0] — 2026-04-18

The first stable v1 release.

### Added

- **Kernel.** Pull-based red-green verification, backdating (early cutoff),
  `strict` / `checked` / `fast` value-membrane modes, LRU eviction, cycle
  detection, untracked-read guards, `Database.set_many(...)` batch
  invalidation, `Database.dependency_graph(...)` export,
  `Database.inspect(...)` / `Database.explain(...)` provenance, and
  `Database.statistics()` / `Database.query_profile()` observability.
- **Built-in resources.** `FileResource`, `FileStatResource`, `EnvResource`,
  `DirectoryResource`.
- **Twelve shipped integrations** under `pyinc.integrations`:
  `python_source`, `toml_config`, `requirements_txt` (including
  `deep_requirements_analysis` for recursive `-r` following),
  `installed_packages`, `json_config`, `dependency_check`, `env_file`,
  `xml_config`, `csv_data`, `deep_module_resolution`, `requirement_evaluation`
  (PEP 440 specifier satisfaction + PEP 508 marker evaluation), and
  `symbol_resolution` (module- and class-level symbol tables with bounded
  cross-module re-export resolution).
- **Typing.** Inline `py.typed` marker; `mypy --strict` clean.
- **Docs.** `kernel-contract.md`, `integration-contract.md`,
  `integration-authoring.md`, `architecture.md`.

### Notes

- Zero runtime dependencies; pure-Python, stdlib-only.
- Tested on CPython 3.11, 3.12, and 3.13.
- LSP wiring and push-based filesystem watchers are architectural non-goals
  for v1; see `docs/architecture.md` for scope boundary.

[1.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.0
[1.0.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.0.1
[1.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.0
[1.1.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.1.1
[1.2.0]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.0
[1.2.1]: https://github.com/Brumbelow/pyinc/releases/tag/v1.2.1
[2.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.0.0
[2.0.1]: https://github.com/Brumbelow/pyinc/releases/tag/v2.0.1
[2.1.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.1.0
[2.5.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.5.0
[2.6.0]: https://github.com/Brumbelow/pyinc/releases/tag/v2.6.0
[3.0.0rc1]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0rc1
[3.0.0]: https://github.com/Brumbelow/pyinc/releases/tag/v3.0.0
