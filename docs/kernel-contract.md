## Kernel Contract — Soundness Envelope

`pyinc` is a correctness-first, in-memory incremental query kernel. This
document defines the guarantee it makes and the exact conditions under which the
guarantee holds; it is the stable semver contract for `src/pyinc`.

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

Dataclasses thaw to dictionaries because the kernel does not import and
reconstruct arbitrary user classes. A dataclass, frozen wrapper, or composite
containing one cannot therefore be used as a mapping key or set member unless a
`ValueAdapter` reconstructs a hashable value; `freeze` rejects such positions
before they can produce a snapshot that later fails to thaw.

The kernel stores frozen snapshots internally. `strict` exposes immutable
frozen views; `checked` and `fast` expose owned thawed values. No external alias
to a value that crossed the boundary can influence the stored snapshot.

Cyclic and shared object graphs are supported via the `FrozenGraph` /
`FrozenRef` snapshot variants: `freeze` memoizes mutable containers (`list`,
`dict`, `set`, dataclass) by id and emits a `FrozenGraph(nodes, root)` envelope
when shared identity or back-edges are detected. `thaw` reconstructs identity
faithfully via two-pass allocate-then-fill so a list-with-itself round-trips to
an actual self-referential list. Pure trees pay no overhead — they continue to
return the bare flat snapshot shape.

**2. Tracked ambient reads.**
All reads of external state within a query must go through the Resource API
(`FileResource`, `BinaryFileResource`, `FileStatResource`, `EnvResource`,
`DirectoryResource`) or a user-defined `Resource`. The public hooks are `read`,
`probe`, `load`, `probe_and_load`, `identity`, and `label`; built-ins derive
probe/value pairs from one observed state.

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
`report_untracked_read()`. Query bodies and equality/cutoff policies must have
fingerprintable implementations and snapshot-safe captures. Dynamically scoped
local classes are rejected; define stable implementation types at module scope.

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
This limitation does not relax identity validation: policy captures and callable
instance state still have to be snapshot-safe.
(See: `test_custom_eq_with_side_effect_does_not_corrupt_graph`)

**3. Mutation in `fast` mode.**
`fast` mode does not detect mutation of boundary values inside queries. The frozen
snapshot stored by the kernel is safe (it was deep-copied at set time), but a
mutating query may observe corrupted intermediate state. Use `checked` or `strict`
for mutation safety.
(See: `test_fast_mode_uses_owned_values_without_mutation_detection`)

**4. Durable cross-run cache (trusted, under stated conditions).**
The kernel is in-memory, but a durable `ArtifactStore` checkpoint is trusted for
from-scratch consistency across processes and runs when **all** of the following
hold:

(i) every `Input` the checkpoint depends on is set before `load_checkpoint`,
uses the same explicit non-empty key across runs, and has the same equality or
cutoff policy; compatible aliases resolve to one logical input and a database
rejects aliases with divergent policies;
(ii) resources satisfy the probe contract across runs — a resource's probe
changes whenever its `load` result changes, and probe values are snapshot-safe
and process-independent;
(iii) adapters for any adapted snapshot type are registered in the loading
process with unchanged `freeze`/`thaw` implementations.

Under these conditions `load_checkpoint(key)` followed by `db.get(query)`
returns the value a fresh recomputation on the same declared state would, in all
three modes. The mechanisms that earn this:

- **Query identities are recomputed live in the loading process.** A query's
  identity pins the interpreter (implementation, version tuple, `-O` optimize
  flag, platform / `os.name` / UTF-8 mode, API/ABI tag, multiarch/platform tag,
  extension suffix, build string, and pointer width) and the full function-definition
  payload — a canonical typed code-object encoding, defaults, keyword defaults,
  comparator policies, and the definitions of transitively captured queries,
  functions, and modules — so a body or policy edit anywhere in the captured
  graph, or a build-configuration change, produces a different identity and the
  stale record simply misses. This encoding never depends on object reference
  counts and supports nested code and slice constants.
- **Inputs and dependency edges verify exactly.** Warmed records carry their
  real dependency edges; each input and sub-query dependency is re-checked
  against the live graph by digest before the record is trusted. Input policy
  digests independently include the interpreter/build identity.
- **Resources are re-probed or re-executed live.** A checkpoint dependency that
  is a resource is re-probed against the real world; a sub-query dependency that
  cannot be warmed is re-executed from its pinned code (the execute-to-verify
  frontier) and its result digest compared to the manifest. Resource identity
  pins the implementations of `probe`, `load`, `probe_and_load`, and `identity`
  in addition to resource configuration and the interpreter/build identity.
- **Bytes verify against their content addresses.** Every snapshot loaded from
  the store is rejected unless `sha256` of its raw bytes equals the digest it was
  keyed by, and the manifest itself is re-hashed against the checkpoint key
  before anything is parsed out of it.

Anything that cannot be verified is skipped and re-executed rather than trusted:
query subgraphs reached only through a runtime import or dynamic dispatch (their
code is not pinned into any identity), records marked untracked via
`report_untracked_read()`, corrupted or missing store bytes, and adapter
mismatches. A tampered, truncated, wrong-version, or wrong-kernel-fingerprint
manifest is rejected loudly with a typed `CheckpointError` subclass.

Residual limitations that stay outside the envelope: the module-monkey-patch
gap of limitation 5 applies across runs exactly as it does in-process; and a
checkpoint does not survive an interpreter or build-configuration change — such
records miss safely (they re-execute) rather than being trusted.

An outbound `ArtifactStore` (`InMemoryArtifactStore` / `FileSystemArtifactStore`)
optionally accepts every snapshot the kernel freezes, keyed by its
`fingerprint_snapshot` digest, via `Database(store=...)`. Bytes are produced by
`serialize_snapshot` and consumed by `deserialize_snapshot`; both round-trip the
full snapshot grammar including `FrozenGraph` / `FrozenRef`. On top of this,
`Database.save_checkpoint(store=None) -> str` serialises the current query and
resource records — their snapshot bytes, call snapshots, resource parameters,
dependency edges, and per-adapter implementation digests — into a
content-addressed manifest (schema v4), returning a key prefixed with `"ck"`.
Adapter digests include `freeze`/`thaw` code, snapshot-safe instance
configuration, and the interpreter/build identity. Saving rejects an adapter whose captures or state cannot be
pinned; loading under such an adapter safely misses and re-executes instead of
thawing checkpoint bytes across an unverifiable implementation boundary.
Records whose cached value no longer matches the live graph (a "dirty" save with
no intervening `get`) are omitted rather than persisted stale, so a reload never
warms a value a fresh run would not produce. `Database.load_checkpoint(key,
store=None)` re-hashes the manifest against the requested key, validates every
record, dependency, input policy, probe, and referenced content address before
atomically staging any records, and rejects a foreign manifest schema or
kernel-fingerprint version loudly. The next `db.get(query)` verifies dependencies
as described above and reuses the
stored result without re-executing when everything checks out, or re-executes
the affected query otherwise. Both methods accept an optional `store=` kwarg for
call-site store injection; the store passed to `load_checkpoint` is also used for
subsequent snapshot loading if the Database was not constructed with a `store=`
argument.

Within a process, `Database` is thread-safe for concurrent use both across
independent instances and on a single shared instance. Each `Database` holds
a `threading.RLock` that serialises every public state read and mutation,
including queries, resources, inputs, statistics, profiles, dependency graphs,
resets, checkpoint operations, and subscriptions. The ambient-read
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
fingerprint version (`K2;`, retained in v3 because its byte grammar is unchanged).
They are stable across CPython minor versions and safe to persist via
`serialize_snapshot` /
`deserialize_snapshot` into an `ArtifactStore`. Any change to the encoder
counts as a cache-key break and must be accompanied by a bump of the kernel
identity prefix so older fingerprints are rejected rather than silently
reused.

`FileSystemArtifactStore` accepts only digest-shaped keys, serializes each
digest with an OS-native process lock, and publishes flushed same-directory
temporary files atomically. POSIX uses no-follow directory-relative operations;
reopens the expected parent and verifies its filesystem identity immediately
before publication; and rejects a directory rename it observes. POSIX cannot
portably exclude a hostile rename in the final interval between that check and
the mutation, so store roots must not be concurrently renamed by non-cooperating
processes.
Windows pins every non-reparse directory component with a handle that denies
delete sharing, publishes from the temporary-file handle, and deletes only
through a validated file handle. Lock files retain the same protected directory
handle chain for the lock's lifetime. Unsafe object, directory, or lock paths
surface a typed `ArtifactStoreError`; lock timeouts surface
`ArtifactStoreLockError`.

**5. Ambient module or class monkey-patching.**
Captured modules contribute their `__version__`, a SHA-256 digest of their
source or compiled file bytes, declared `__all__`, stable scalar constants, and
the behavior reached through statically resolvable attribute chains. Re-exported
functions and submodules pin their defining modules transitively; dynamic access
to a custom module is rejected when the behavior cannot be proven. A third-party
version bump or source-file edit invalidates cached results that capture that
module. Query definitions are weakly memoized per `Database` for high-cardinality
calls, with runtime-build and captured-module observations rechecked before reuse
so the memo cannot hide those changes. An in-process
monkey-patch of an existing module or module-owned class attribute (for example,
`sys.modules["foo"].X = 42` or `foo.Model.flag = True` without reloading or
touching the file) is **not** detected. Route such mutable state through an
`Input` or a custom `Resource`.

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
  implementing `freeze` and `thaw`.

- **`eq=` / `cutoff=`** on `Input` and `@query` — allows custom equivalence.
  `eq=` compares thawed values directly; `cutoff=` compares snapshot-safe tokens.
  These are mutually exclusive. Cutoff tokens must be snapshot-safe.
  (See: `test_input_cutoff_suppresses_equal_updates`,
  `test_query_cutoff_backdates_and_skips_downstream`)

### Output Reconciliation (Actions)

Queries are pure and never write. The separate **action layer** (`@action`,
`Output`, `ReconcileResult`; see [action-contract.md](action-contract.md))
reconciles a query-derived desired-output set against the filesystem: atomic
writes, content-hash change/tamper detection, ownership-ledger orphan deletion,
and dry-run planning. Reconciliation runs at top level only — never inside a
query — so it does not change query semantics, the value membrane,
untracked-read enforcement, or the `strict`/`checked`/`fast` modes. The
kernel's from-scratch guarantee lifts to the filesystem: an incremental
sequence of reconciles yields the same output files as a single reconcile from
a fresh `Database` into an empty directory.

### Additional Kernel Properties

- Query identity includes the function definition payload, including supported
  captured values, the full definition payloads of transitively captured queries
  (a body edit to a dependency query moves the parent's identity), and the build
  configuration (Python implementation and version, `-O` optimize flag, platform,
  `os.name`, UTF-8 mode, full prerelease tuple, and ABI/architecture identity).
  Mutable closure/global captures and local/dynamically
  unbound type objects are rejected. Module-level types are pinned through their
  defining module identity. Use
  `pyinc.explain_query_captures(fn)` to preview how each capture will be
  classified before the first `db.get()`.
- `Query` is public, and `@query(key=...)` accepts an explicit stable key; the
  default is `module:qualname`. Coroutine and generator queries are rejected at
  decoration time.
- `Resource[KeyT, ValueT, ProbeT]` and `Database.read_resource(...)` are public.
  Resource identity includes configuration and the implementations of every
  state-observation hook plus the runtime/build identity. Built-ins observe probe/value pairs from one state and
  include text, binary, environment, stat, and directory resources.
- `Database.inspect(...)` exposes the last recorded provenance tree as structured
  data. `Database.explain(...)` formats it for humans. Inspection is
  observational and does not force an extra verification pass;
  `Database.inspect_fresh(...)` runs verification first and then returns the
  provenance tree.
- `Database(max_query_nodes=...)` enables bounded memoization. Eviction happens
  at top-level request boundaries and affects query nodes only. Per-node timing
  uses fixed-size count/total/min/max/last aggregates; eviction also removes the
  node's call snapshot, timing profile, and unused query registry entry.
- Query labels contain only the query key and a short argument digest; formatting
  a graph or profile never calls argument `repr` or embeds argument values.
- Query records and dependency rewiring publish only after successful execution.
  A failed or cyclic evaluation keeps an earlier record usable and cannot leave a
  dangling dependency edge.
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

The durable cross-run guarantee (limitation 4) is mechanically verified by the
same fresh-recomputation equivalence, extended to the checkpoint path:

- `test_checkpoint_reload_matches_fresh_recomputation` (property test in
  `tests/test_properties.py`) — reloads a checkpoint across all three modes and
  with/without LRU eviction, comparing `load_checkpoint` + `get()` against a
  fresh, cache-free run over the same edit sequence, and exercises the
  dirty-graph save path directly.
- `tests/test_checkpoint_cross_process.py` — a subprocess matrix that saves in
  one interpreter and reloads in another, proving identities and digests line up
  across processes.
- `tests/test_checkpoint_trust.py` — the adversarial store and trust suite:
  bit-flipped and truncated snapshot bytes, tampered and wrong-version
  manifests, changed query/adapter/resource implementations, and
  runtime-import-reached dependencies each fall back to safe re-execution or a
  loud `ValueError` rather than serving a stale or tampered value.
