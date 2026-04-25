## Kernel Contract — Soundness Envelope

`pyinc` v1 is a correctness-first, in-memory incremental query kernel.

### The Guarantee

pyinc guarantees **from-scratch consistency** — the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and resources —
when and only when the three conditions below hold.

When a recomputed value is semantically equal to the previously stored value,
the record is **backdated** (also called **early cutoff**): its `changed_at`
revision is not advanced, so downstream dependents remain green and avoid
unnecessary recomputation.

### Conditions for From-Scratch Consistency

**1. Value boundary ownership.**
All values crossing cached boundaries (query arguments, query return values,
`Input` values) must be snapshot-safe: either immutable scalars, containers that
`freeze` can deep-convert to owned representations (`list` → `tuple`,
`dict` → `FrozenDict`, `set` → `frozenset`), or values handled by registered
`ValueAdapter` instances.

The kernel stores frozen snapshots internally and exposes fresh owned copies
(`checked`/`fast` modes) or frozen views (`strict` mode) on each read. No
external alias to a value that has crossed the boundary can influence cached
state; each `db.get()` call returns an independent copy.

Cyclic and shared object graphs are supported via the `FrozenGraph` /
`FrozenRef` snapshot variants: `freeze` memoizes mutable containers (`list`,
`dict`, `set`, dataclass) by id and emits a `FrozenGraph(nodes, root)` envelope
when shared identity or back-edges are detected. `thaw` reconstructs identity
faithfully via two-pass allocate-then-fill so a list-with-itself round-trips to
an actual self-referential list. Pure trees pay no overhead — they continue to
return the bare flat snapshot shape.

**2. Tracked ambient reads.**
All reads of external state within a query must go through the Resource API
(`FileResource`, `FileStatResource`, `EnvResource`, `DirectoryResource`) or a
user-defined resource implementing the `label`/`probe`/`load` protocol.

The kernel intercepts the following during query execution and raises
`UntrackedReadError` if they are called outside a resource scope:

- `builtins.open` and `io.open`
- `os.getenv` and `os.environ` access
- `os.listdir` and `os.scandir`
- `Path.iterdir`

Reads not intercepted by this mechanism (see Limitations below) must be declared
via `db.report_untracked_read()`.

**3. Deterministic queries w.r.t. tracked dependencies.**
Given the same tracked inputs, resources, and sub-query results, a query function
must return a semantically equal value. Nondeterminism (timestamps, random
numbers, process state) must either be routed through a Resource or declared via
`report_untracked_read()`.

### Mode-Specific Enforcement

| Mechanism | `strict` | `checked` | `fast` |
|---|---|---|---|
| Values exposed as frozen | Yes | No (owned copies) | No (owned copies) |
| Mutation detection at boundary | `TypeError` on write | Fingerprint before/after | None |
| Untracked read interception | Yes | Yes | Yes |
| Mutable closure/global rejection | Yes | Yes | Yes |
| Semantic equality for cutoffs | Yes | Yes | Yes |
| Backdating on equal recomputation | Yes | Yes | Yes |

### Explicit Limitations

These fall **outside** the soundness envelope. The kernel does not guarantee
from-scratch consistency when any of these apply.

**1. Unintercepted ambient reads.**
`os.open()` (the low-level syscall), C-extension I/O, subprocess output, network
calls, `ctypes` memory access, and similar are not intercepted. These bypass the
guard and silently violate condition 2 unless declared via
`db.report_untracked_read()`.
(See: `test_os_open_bypasses_untracked_read_guard`)

**2. Custom `eq=`/`cutoff=` with side effects.**
If `eq=` or `cutoff=` callbacks perform ambient reads or mutations, the
equivalence check itself becomes a hidden dependency. The kernel cannot detect
this. These callbacks must be deterministic and side-effect-free; the kernel will
continue to function but may make incorrect backdating decisions if they are not.
(See: `test_custom_eq_with_side_effect_does_not_corrupt_graph`)

**3. Mutation in `fast` mode.**
`fast` mode does not detect mutation of boundary values inside queries. The frozen
snapshot stored by the kernel is safe (it was deep-copied at set time), but a
mutating query may observe corrupted intermediate state. Use `checked` or `strict`
for mutation safety.
(See: `test_fast_mode_uses_owned_values_without_mutation_detection`)

**4. Cross-process or cross-run persistence.**
The kernel is in-memory. Code fingerprints include the Python implementation
and version tuple but not all possible build configuration differences. The
kernel does not yet trust a durable cache for from-scratch consistency.

In v2.0.0, an outbound `ArtifactStore` (`InMemoryArtifactStore` /
`FileSystemArtifactStore`) optionally accepts every snapshot the kernel
freezes, keyed by its `fingerprint_snapshot` digest, via
`Database(store=...)`. Bytes are produced by `serialize_snapshot` and consumed
by `deserialize_snapshot`; both round-trip the full snapshot grammar including
`FrozenGraph` / `FrozenRef`. The durable checkpoint API completes cross-run
cache reuse: `Database.save_checkpoint(store=None) -> str` serialises all
current node records and their snapshot bytes to the store, returning a
content-addressed key (SHA-256 prefixed with `"ck"`).
`Database.load_checkpoint(key, store=None)` reads the manifest back, verifies
that all declared input digests and resource probe hints still match the
current database state, and pre-warms the record cache so that the next
`db.get(query)` reuses stored results without re-executing the function. Stale
or unverifiable records are silently skipped and the affected queries
re-execute (from-scratch consistency is maintained). Both methods accept an
optional `store=` kwarg for call-site store injection; the store passed to
`load_checkpoint` is also used for subsequent snapshot loading if the Database
was not constructed with a `store=` argument.

Within a process, `Database` is thread-safe for concurrent use both across
independent instances and on a single shared instance. Each `Database` holds
a `threading.RLock` that serialises state mutations across public entry
points (`get`, `set`, `set_many`, `inspect`, `explain`). The ambient-read
guard is installed globally exactly once and dispatches per-context via a
`ContextVar` stack of active databases — two threads inside queries on
different `Database` instances do not stomp each other's enforcement, and
raw I/O from a thread that is *not* inside any query continues to work
unaffected. If many threads share a single `Database`, work serialises on the
per-instance lock; if they hold separate `Database` instances they run in
parallel.

`fingerprint_snapshot(snapshot)` is a deterministic, stable function of the
`Snapshot` union (scalars, `FrozenList`, `FrozenDict`, `FrozenSet`,
`FrozenRecord`, `FrozenAdapterValue`, `FrozenGraph`, `FrozenRef`, tuples of
the same). Digests are an injective-by-construction length-prefixed,
type-tagged encoding finalized with sha256, prefixed with the kernel
fingerprint version (`K2;` in v2.0.0). They are stable across CPython minor
versions and safe to persist via `serialize_snapshot` /
`deserialize_snapshot` into an `ArtifactStore`. Any change to the encoder
counts as a cache-key break and must be accompanied by a bump of the kernel
identity prefix so older fingerprints are rejected rather than silently
reused.

**5. Cycle-adjacent partial state.**
When a `CycleError` is raised, the dependency graph may contain partial state from
the aborted evaluation. The database remains functional for non-cyclic queries
after the error.
(See: `test_cycle_error_does_not_corrupt_database_for_subsequent_queries`)

**5b. Ambient module monkey-patching.**
Captured modules contribute their `__version__`, source-file digest (sha256
for `.py`, `(size, mtime_ns)` for compiled files), and declared `__all__` to
the code fingerprint — a third-party version bump or source-file edit
invalidates cached results that capture that module. An in-process
monkey-patch of an existing attribute (e.g. `sys.modules["foo"].X = 42`
without reloading or touching the file) is **not** detected. Route such
mutable state through an `Input` or a custom `Resource`.

**6. LRU eviction under active dependencies.**
If `max_query_nodes` is set low enough that an intermediate query is evicted while
a dependent is still active, the dependent will re-execute the intermediate from
scratch on its next request. This is correct but may degrade performance.
(See: `test_rewiring_with_lru_eviction`)

### Escape Hatches

- **`db.report_untracked_read(reason)`** — marks the current query as impure;
  forces re-execution on every request and disables backdating for that node.
  Downstream consumers re-verify but can still backdate if their own results are
  unchanged.
  (See: `test_report_untracked_read_forces_reexecution_on_every_request`,
  `test_impure_child_prevents_parent_backdating_unless_result_unchanged`)

- **`ValueAdapter`** — allows custom types to participate in freeze/thaw by
  implementing `supports`, `freeze`, and `thaw`.

- **`eq=` / `cutoff=`** on `Input` and `@query` — allows custom equivalence.
  `eq=` compares thawed values directly; `cutoff=` compares snapshot-safe tokens.
  These are mutually exclusive. Cutoff tokens must be snapshot-safe.
  (See: `test_input_cutoff_suppresses_equal_updates`,
  `test_query_cutoff_backdates_and_skips_downstream`)

### Additional Kernel Properties

- Query identity includes the function definition payload, including supported
  captured values. Mutable closure/global captures are rejected. Use
  `pyinc.explain_query_captures(fn)` to preview how each capture will be
  classified before the first `db.get()`.
- Resource identity includes resource configuration (e.g., encoding for
  `FileResource`).
- `Database.inspect(...)` exposes the last recorded provenance tree as structured
  data. `Database.explain(...)` formats it for humans. Inspection is
  observational and does not force an extra verification pass;
  `Database.inspect_fresh(...)` runs verification first and then returns the
  provenance tree.
- `Database(max_query_nodes=...)` enables bounded memoization. Eviction happens
  at top-level request boundaries and affects query nodes only.
- The distributed package is PEP 561 typed via `py.typed`.

### Push Observers

`Database.observe(callback, query, *args, **kwargs)` registers a callback that
fires when the identified query node's stored value changes. It returns a
`Subscription` whose `unsubscribe()` method detaches the callback; repeated
unsubscribes are no-ops.

Fires exactly when:

- the node was (re-)executed during a top-level `get` / `inspect` /
  `inspect_fresh` call, **and**
- the resulting decision was `"executed"` — either a cold execution (no
  prior record) or a true recompute where the new value did not match the
  previous one under `eq=` / `cutoff=` / semantic equality.

Does **not** fire on:

- `"backdated"` — recomputation produced a semantically equal value;
- `"reused"` — dependencies were unchanged so no recomputation happened;
- `db.set(...)` / `db.set_many(...)` — input mutation alone does not
  execute any query. Observers fire on the next `get` that triggers
  dependent re-execution.

Dispatch model:

- Events are buffered on the outermost request scope and delivered **after**
  the kernel lock is released. A callback may therefore re-enter the
  database (e.g. call `db.get(...)`) without risk of deadlock.
- For each event, the callback list for that node is snapshotted once at
  dispatch time under the state lock, then dispatched lock-free. A
  subscription added during dispatch will not see the current batch; one
  removed during dispatch will still receive events already snapshotted.
- Exceptions raised by a callback are routed to the
  `observer_error_hook` passed to `Database(...)` (default: a one-line
  stderr log) and do not suppress sibling callbacks for the same event.
- Subscriptions survive LRU eviction of their node: if the evicted node is
  later re-executed, the callback fires normally.

`QueryChangeEvent` is a frozen dataclass carrying the node's `query_id`,
`args_digest`, decision (`"executed"`), and the `changed_at` / `verified_at`
revisions at the time of execution.

### Verification

The from-scratch consistency guarantee is mechanically verified by property tests
that compare incremental results against fresh-database recomputation for the same
declared state, across all three modes and with/without LRU eviction:

- `test_incremental_results_match_fresh_recomputation` (basic query graph)
- `test_resource_backed_queries_match_fresh_recomputation` (file resources)
- `test_workspace_queries_match_fresh_recomputation` (integration-level)
- `test_multi_level_rewiring_matches_fresh_recomputation` (diamond + multi-level switching)

The rewiring torture suite (`test_diamond_dependency_with_rewiring`,
`test_multi_level_switching`, `test_sharing_pattern_backdates_when_rewired_result_is_equal`,
`test_swapping_pattern_two_queries_exchange_deps`, `test_rewiring_with_lru_eviction`)
verifies that dynamic dependency changes correctly drop stale edges and maintain
correctness across sharing, switching, and swapping patterns.

The mutation adversarial suite (`test_external_alias_mutation_after_boundary_crossing`,
`test_deeply_nested_mutation_detection`, `test_mutation_of_query_return_value_does_not_corrupt_memo`,
`test_two_queries_reading_same_input_get_independent_copies`) verifies that the
value membrane protects cached state from external mutation, deep mutation, and
cross-query aliasing.
