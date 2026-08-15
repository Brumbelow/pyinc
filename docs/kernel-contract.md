# Kernel Contract — Soundness Envelope

`pyinc` is a correctness-first, in-memory incremental query kernel. This
document defines the guarantee it makes and the exact conditions under which the
guarantee holds; it is the stable semver contract for `src/pyinc`.

## The Guarantee

pyinc guarantees **from-scratch consistency** — the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and resources —
provided the three conditions below hold. Outside those conditions no
guarantee is made.

When a recomputed value is canonically equal to the previously stored value,
the record is **backdated** (also called **early cutoff**): its `changed_at`
revision is not advanced, so downstream dependents remain green and avoid
unnecessary recomputation. The default equality decision is
**canonical-encoding equality** over the stored snapshots: two values are
equal exactly when their canonical snapshot encodings are byte-identical, so
`1`, `1.0`, and `True` are three different values, `0.0` differs from `-0.0`,
and a canonical NaN equals a canonical NaN. The same relation decides default
input updates, resource probe comparisons, checkpoint probe hints, and the
token comparison behind a `cutoff=` policy, and it is identical in `strict`,
`checked`, and `fast`; an `eq=` policy decides under its own relation instead,
over the detached operands described below.

## Conditions for From-Scratch Consistency

**1. Value boundary ownership.**
All values crossing cached boundaries (query arguments, query return values,
`Input` values) must be snapshot-safe: either immutable scalars, containers
that `freeze` can deep-convert to owned snapshot representations, or values
handled by registered `ValueAdapter` instances. `freeze` converts
`list` → `FrozenList`, `dict` → `FrozenDict`, `set` → `FrozenSet`
(kind `"set"`), `frozenset` → `FrozenSet` (kind `"frozenset"`), and
dataclasses → `FrozenRecord`; tuples are a native member of the `Snapshot`
union and are frozen element-wise.

Dataclasses thaw to dictionaries because the kernel does not import and
reconstruct arbitrary user classes. A dataclass, a wrapper that thaws to a
mutable container, or a composite containing one cannot therefore be used as a
mapping key or set member; `freeze` rejects such positions before they can
produce a snapshot that later fails to thaw. A `ValueAdapter` that
reconstructs a hashable value is necessary to carry such a value into a hash
position, but it is not sufficient, for two independent reasons. First, an
adapted value whose payload contains a `FrozenGraph` is refused, registered
adapter or not: the shared or cyclic state such a payload rebuilds stays
mutable after the value is inserted, so nothing at the boundary can establish
that its hash is stable. (That refusal reuses the general message and asks for
a `ValueAdapter` even where one is registered; the fix is to keep the cycle out
of the payload, not to register another adapter.) That check runs where
`freeze` builds a hash position out of a live mapping or set, so it is the
live route that is closed; a snapshot already carrying such a key is not
refused — `freeze`, `serialize_snapshot`, and `deserialize_snapshot` all
accept a hand-assembled or byte-decoded one. Second, an independent gate —
the snapshot validator every entry point runs — rejects hash positions that
are distinct under the snapshot encoding but collapse under Python `==`/`hash`
after thaw. `FrozenDict` keys and `FrozenSet` members such as `1` beside
`1.0`, `True` beside `1`, or `0.0` beside `-0.0` raise `UnsupportedValueError`
from `freeze`, `thaw`, `serialize_snapshot`, and `deserialize_snapshot` alike,
with the collapsing pair named in the error; two positions carrying the same
adapter key are refused as soon as their payloads collapse, because the
encoding cannot ask the adapter whether the values it rebuilds would still
differ. Where the encoding cannot see the answer the gate is deliberately
conservative rather than nondeterministic: every canonical NaN is one class,
so a live `dict` keyed by `(1, nan)` and `(1.0, nan)` with distinct NaN
objects — two keys to Python, which hashes a NaN by identity — is rejected,
both halves of the key having collapsed at once: `1` with `1.0` under the
numeric rule, the two NaNs under the one-class rule. Keys that stay apart
somewhere, `(2, nan)` beside `(3, nan)`, are accepted. A container that would
silently change cardinality on thaw is refused at the boundary instead of
being stored, whether `freeze` built it from live values or it arrived
hand-assembled or decoded from bytes — for every adapter that satisfies the
semantic round-trip law below. An adapter that thaws two distinct payloads
into equal values breaks that law, and this gate, which reads payload
encodings rather than running the adapter, cannot see it coming.

`freeze()` and `thaw()` are boundary utilities, not a general object
serializer. Passing an adapter registry to `freeze()` does not embed executable
reconstruction logic in the snapshot; the matching registry must also be
available to `thaw()`. Without an adapter, a dataclass's class identity is not
reconstructed.

The kernel stores frozen snapshots internally. `strict` exposes the immutable
`Frozen*` views themselves (a query receives, for example, a `FrozenDict` where
the other modes hand it a `dict`); `checked` and `fast` expose owned thawed
values. No external alias to a value that crossed the boundary can influence
the stored snapshot. This holds in both directions: `freeze` returns a
snapshot the kernel owns outright — an already-frozen wrapper is cloned rather
than passed through by identity, a tree-shaped one into a structurally
identical, identically fingerprinted copy and an aliased or cyclic one into
the same canonical graph encoding the equivalent raw structure produces — so
nothing done through the object you passed in can influence a stored record, a
stored resource probe, or a mapping key or set member. Leaf scalars and
all-leaf tuples are still shared with the clone, deliberately: no `Frozen*`
shell is, and nothing reflective can rebind a leaf.

Cyclic and shared object graphs are supported via the `FrozenGraph` /
`FrozenRef` snapshot variants: `freeze` memoizes mutable containers (`list`,
`dict`, `set`, dataclass) by id and emits a `FrozenGraph(nodes, root)` envelope
when shared identity or back-edges are detected. `thaw` reconstructs identity
faithfully via two-pass allocate-then-fill so a list-with-itself round-trips to
an actual self-referential list. Pure trees pay no overhead — they continue to
return the bare flat snapshot shape.

Memoization is by **mutable container** identity, so the graph support above
covers exactly those four types. Values crossing through a `ValueAdapter`, a
`tuple`, or a `frozenset` are not memoized and cannot be the target of a
back-edge: `freeze` rejects such a graph with `UnsupportedValueError` rather
than emitting a node, and `FrozenAdapterValue` is not a legal `FrozenGraph`
node. A cyclic adapted object (for example `obj.child.parent is obj`) must
therefore route its cycle through a `list`, `dict`, `set`, or dataclass — or be
decomposed by the adapter into one.

**2. Tracked ambient reads.**
All reads of external state within a query must go through the Resource API
(`FileResource`, `BinaryFileResource`, `FileStatResource`, `EnvResource`,
`DirectoryResource`, `ResolvedPathResource`) or a user-defined `Resource`. The public hooks are `read`,
`probe`, `load`, `probe_and_load`, `identity`, and `label`; built-ins derive
probe/value pairs from one observed state. On a warm request the kernel may
first check for an unchanged world with `probe` alone and calls
`probe_and_load` only when that probe misses or the record cannot answer, so a
resource's `probe` and the probe component of its `probe_and_load` must agree
on an unchanged world; stored probe/value pairs always originate from one
`probe_and_load` observation.

The kernel intercepts the following during query execution and raises
`UntrackedReadError` if they are called outside a resource scope:

- `builtins.open` and `io.open`
- `os.getenv` and `os.environ` access
- `os.listdir` and `os.scandir`
- `Path.iterdir`

Reads not intercepted by this mechanism (see limitation 1) must be declared
via `db.report_untracked_read()` ([Escape Hatches](#escape-hatches)).

**3. Deterministic queries w.r.t. tracked dependencies.**
Given the same tracked inputs, resources, and sub-query results, a query function
must return a semantically equal value. Nondeterminism (timestamps, random
numbers, process state) must either be routed through a Resource or declared via
`report_untracked_read()`. Query bodies and equality/cutoff policies must have
fingerprintable implementations and snapshot-safe captures. Dynamically scoped
local classes are rejected; define stable implementation types at module scope.

Custom `eq=`/`cutoff=` policies must also be **substitutive** for every
dependent computation: when a policy reports two values unchanged, each
dependent must produce a semantically equal result from either value. A
coarser policy is permitted, but the guarantee it buys is correspondingly
coarser — backdating keeps dependents at results computed from the earlier
representative, so from-scratch consistency then holds *modulo the declared
equivalence* rather than on exact values.
(See: `test_non_substitutive_cutoff_keeps_dependents_at_the_earlier_representative`)

## Mode-Specific Enforcement

| Mechanism | `strict` | `checked` | `fast` |
|---|---|---|---|
| Values exposed as frozen | Yes | No (owned copies) | No (owned copies) |
| Mutation detection at boundary | `TypeError` on write | Fingerprint before/after | None |
| Untracked read interception | Yes | Yes | Yes |
| Mutable closure/global rejection | Yes | Yes | Yes |
| Semantic equality for cutoffs | Yes | Yes | Yes |
| Backdating on equal recomputation | Yes | Yes | Yes |

## Failing Resource Loads

A resource whose `load` (or `probe_and_load`) raises is an **observation**, not
the absence of one. The kernel stores a *failure record* for that resource node,
carrying the probe observed alongside the failure, and lets the ordinary
probe-comparison machinery drive invalidation:

- The reading query records its dependency edge on the failing resource before
  the exception propagates, so a later `get()` re-checks that node instead of
  treating the reader as dependency-free.
- The exception surfaces **inside the query body**, where the query's own
  `try`/`except` can see it. A refresh that raises while a dependent is being
  verified never escapes `get()`.
- An unchanged failing probe does not move the revision, so a query that handled
  the failure stays green across repeated requests. A changed probe — or a
  transition between success and failure in either direction — invalidates the
  readers.
- A failure record never satisfies a read with a value — it holds none. The
  first read in a request re-runs the load, so the exception is a live one; the
  reads that follow it *within that request* re-raise the exception that load
  produced, exactly as a successful load's value is reused for the rest of the
  request. A failing resource costs one load per request, not one per reader.
  The exception is dropped when that request ends — nothing outside it may
  re-raise it — so a node that keeps failing never pins the frames, or the
  allocations, of the load that raised. A `request_span` moves the request
  boundary with it: a failing load's exception is re-raised by reads
  throughout the span and dropped when the outermost span closes, exactly as
  it is for a single `get`.
  (See: `test_failing_resource_loads_once_per_request_across_a_fan_out`,
  `test_repeated_failing_reads_within_one_query_body_load_once`,
  `test_failing_load_exception_is_reused_only_inside_its_own_request`,
  `test_failing_load_frames_are_released_when_the_request_ends`)
- Behaviour is identical in `strict`, `checked`, and `fast`.

Optional external state is therefore from-scratch consistent: a query that
returns a default when a file is missing returns the file's contents once it
appears, and the default again once it is removed, matching a fresh `Database`
at every step.
(See: `test_appearing_resource_invalidates_the_query_that_handled_its_absence`,
`test_disappearing_resource_raises_inside_the_query_body`,
`test_optional_resource_queries_match_fresh_recomputation`)

Two boundaries apply:

- **The probe must be total.** This rests on `probe()` modelling failure instead
  of raising — `FileResource.probe` returns `("missing",)` for an absent file. A
  resource whose `probe` *also* raises is outside the contract, and what the
  kernel does then depends on what it already knows about that node.

  - **No record yet:** the read is the node's first, nothing is recorded, the
    exception propagates unchanged, and a query that catches it is cached as if
    it had no dependency at all — a later `get()` in that same process does not
    re-check it, and neither does anything that depends on it (it is still
    refused a checkpoint, see below).
  - **Record present:** an earlier success, or an earlier recorded failure,
    describes a world the kernel can no longer confirm, so the node is reported
    as *changed*: the queries that read it directly re-execute and the exception
    surfaces inside their query bodies again. The record is also marked
    unconfirmed, which retires its stored probe until a real observation
    rewrites the record: a world that returns to exactly the state that probe
    describes — an undo, a branch switch back — re-loads instead of reusing, and
    the readers that consumed the raise re-execute rather than staying green on
    a value only the failure explains. A permission denial that neither the
    probe nor the load survives is the ordinary way to reach this state; the
    shipped file and listing resources read a path whose kind changed as a
    missing file or an absent listing rather than raising.
  - **Revision accounting:** entering that unconfirmed state **moves the
    revision**, exactly as a recorded failure does. A direct reader that handles
    the exception is otherwise the end of the story: it would return at the
    revision its own dependents had already verified, so nothing above it would
    ever learn that the world moved, and a transitive dependent would keep a
    pre-failure value permanently. The bump is per *transition* — one on the way
    in, one on the way out — plus one for each re-executed query whose
    recomputed value actually changed, never one per observation or request, so
    `revision` settles while a resource stays unprobeable instead of churning on
    every `get()`, and a resource that heals and breaks again bumps again. That
    stays consistent with a fresh `Database` throughout, and it settles as soon
    as a load succeeds again; while the probe keeps raising, the queries that
    read it directly re-run every request, and *their* dependents re-run only
    when the handled value actually differs.

  The probe contract extends to failures for the same reason it covers values: a
  resource whose `load` can raise *different* exceptions for one probe value must
  fold that distinction into the probe. Invalidation compares probes only, never
  exception messages, which are frequently nondeterministic.
  (See: `test_failed_resource_loads_are_recorded_only_when_the_probe_is_total`,
  `test_file_replaced_by_a_directory_matches_a_fresh_database`,
  `test_directory_replaced_by_a_file_matches_a_fresh_database`,
  `test_missing_file_replaced_by_a_directory_matches_a_fresh_database`,
  `test_module_replaced_by_a_package_matches_a_fresh_database`,
  `test_directory_restored_after_a_kind_swap_matches_a_fresh_database`,
  `test_handled_unrecordable_failure_invalidates_a_transitive_reader`,
  `test_handled_unrecordable_failure_propagates_more_than_one_hop`,
  `test_permanently_unrecordable_failure_settles_the_revision`)
- **Failures are not checkpointed.** A failure record holds no value, and a
  reader that handled a failure is only reproducible while the load keeps
  failing. The failure record and every record that transitively depends on it
  are omitted from a checkpoint, so they re-execute against live state after
  `load_checkpoint`. A failure the kernel could not record is excluded the same
  way, and it has to be: the resource record an unprobeable raise contradicted
  still carries the probe and digest from before that raise, which verify against
  a world that healed back into exactly that state, and the reader that consumed
  the raise carries a handled-failure value no record explains. Both are dropped,
  and with them every record above them.
  (See: `test_checkpoints_omit_failed_resource_records_and_their_readers`,
  `test_checkpoints_omit_readers_of_an_unrecordable_failure`)

`inspect()` and `explain()` show the failure node with decision `failed` and a
reason naming the exception; it counts in `DatabaseStatistics.resource_count`
like any other resource node. A load that raised is not counted as a
`resource_load`, and re-running a load on an unchanged failing probe is not
counted as a `resource_probe_hit`. Unless the resource overrides
`probe_and_load` to observe both from one read, the probe stored with a failure
is taken just after it, so a `failed` node can display a probe describing an
already-healed world; the next request re-runs the load and clears it.

A failure the kernel could **not** record writes no record at all, so there is
nothing for `inspect()` to relabel: the node keeps the decision, probe, digest,
and `changed_at` of its last real observation, which is now older than the
database `revision`. Read a resource node that way — as the last observation the
kernel could describe, not as a claim about the world right now. Invalidation
does not consult that stale `changed_at`: the unconfirmed mark reports the node
changed on every refresh and retires its probe until a real observation replaces
it.

## Request Spans

`db.request_span()` is a context manager that holds one request open across
several top-level `get` / `inspect` / `inspect_fresh` / `read_resource`
calls, so once-per-request work — resource validation above all — happens
once for the whole batch.

Entering a span declares that the world the database reads from does not
change until the span closes; a caller that changes it mid-span must declare
the change with `db.request_inputs_changed()`, which rolls the span onto a
fresh request so the next read of each node re-validates instead of answering
from the span's earlier observation. The declaration is instance-wide: a
change committed by any thread rolls the span. `db.set(...)` and
`db.set_many(...)` declare their own changes — inside a span an input update
that actually changed something rolls the request exactly as
`request_inputs_changed()` does, so later gets re-derive from the new inputs,
while an update the equality decision ignores rolls nothing.

Outside a span `db.request_inputs_changed()` is a no-op — every top-level
call already opens its own request. Spans are reentrant: an inner span, or
one opened inside a `get`, joins the enclosing request, and only the
outermost close ends it.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, FileResource, Input, query

NOTES = FileResource()
SUFFIX = Input[str]("span.suffix")


@query
def line_count(db: Database, path: str) -> int:
    return len(NOTES.read(db, path).splitlines())


@query
def summary(db: Database, path: str) -> str:
    return f"{line_count(db, path)}{SUFFIX.read(db)}"


with TemporaryDirectory() as directory:
    path = str(Path(directory, "notes.txt"))
    Path(path).write_text("first\nsecond\n", encoding="utf-8")

    db = Database()
    db.set(SUFFIX, " lines")
    assert db.get(summary, path) == "2 lines"

    before = db.statistics()
    with db.request_span():
        assert db.get(line_count, path) == 2       # validates the file once
        assert db.get(summary, path) == "2 lines"  # answers from that pass
        assert db.inspect(summary, path).last_decision == "reused"
        db.set(SUFFIX, " rows")                    # a real change rolls the span
        assert db.get(summary, path) == "2 rows"   # re-validates against it
    after = db.statistics()

    # Four calls, two requests: one per declared world, not one per call.
    assert after.total_requests - before.total_requests == 2
    assert after.resource_probe_hits - before.resource_probe_hits == 2
```

Without the span those same four calls open four requests and re-probe the
file on three of them.

## Explicit Limitations

These fall **outside** the soundness envelope. The kernel does not guarantee
from-scratch consistency when any of these apply.

**1. Unintercepted ambient reads.**
The condition 2 guard covers an enumerated set of entry points, not a category of
behaviour. Everything else that observes external state bypasses it and silently
violates condition 2 unless declared via `db.report_untracked_read(reason)`:
`os.open()` (the low-level syscall), C-extension I/O, subprocess output, network
calls, `ctypes` memory access, and similar.
(See: `test_os_open_bypasses_untracked_read_guard`,
`test_condition_two_entry_points_stay_guarded`)

Three of those gaps sit close enough to the guarded set to be named individually.

- **File metadata.** The guard sees file *contents* and directory *listings*; it
  does not see `stat`. `os.stat`, `os.lstat`, `os.access`, `Path.stat`,
  `Path.exists`, `Path.is_file`, `Path.is_dir`, `Path.resolve`, and the
  `os.path` helpers built on them (`exists`, `isfile`, `getsize`, `getmtime`)
  each reach the live filesystem from inside a query and return normally. A
  query that asks whether a file exists, or how large or how recently modified
  it is, rather than opening it, is therefore silently untracked: no dependency
  edge is recorded, so the query is reused unchanged after that file appears,
  disappears, or is rewritten, while a fresh `Database` reports the new state.
  Route the observation through `FileStatResource`, whose probe covers
  existence, size, and mtime, through `ResolvedPathResource` when the
  observation is where a path canonicalizes to, or declare it with
  `db.report_untracked_read(reason)`.
  (See: `test_file_metadata_reads_bypass_untracked_read_guard`,
  `test_stat_only_query_is_never_invalidated_by_the_file_it_stats`,
  `test_report_untracked_read_restores_consistency_for_a_stat_only_query`,
  `test_file_stat_resource_tracks_metadata_changes`,
  `test_resolved_path_resource_tracks_symlink_retargeting`)
- **The byte-oriented environment.** `os.getenv` and `os.environ` are
  intercepted; `os.getenvb` and `os.environb` — the same process environment
  under a second name where `os.supports_bytes_environ` holds — are not.
  (See: `test_byte_environment_views_bypass_untracked_read_guard`)
- **The working directory.** `os.getcwd` and `Path.cwd` are not intercepted, so
  a query whose result varies with the process working directory is untracked.
  Pass absolute paths as query arguments instead.
  (See: `test_working_directory_reads_bypass_untracked_read_guard`)

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
The kernel is in-memory, but a durable `ArtifactStore` checkpoint (see
[Checkpoint Save and Load](#checkpoint-save-and-load) for the API) is trusted
for from-scratch consistency across processes and runs when **all** of the
following hold:

- **(i)** every `Input` the checkpoint depends on is set before
  `load_checkpoint`, uses the same explicit non-empty key across runs, and has
  the same equality or cutoff policy; compatible aliases resolve to one logical
  input, and a database rejects aliases with divergent policies;
- **(ii)** resources satisfy the probe contract across runs — a resource's
  probe changes whenever its `load` result changes, and probe values are
  snapshot-safe and process-independent;
- **(iii)** adapters for any adapted snapshot type are registered in the
  loading process with unchanged `freeze`/`thaw` implementations;
- **(iv)** the checkpoint key and the store it loads from come from a trusted
  channel. Content addressing proves that bytes match the key they were asked
  for by — it does not authenticate where the key came from. A coherent
  attacker-selected key names a coherent attacker-selected manifest, so keys
  and store contents must be produced by a prior trusted `save_checkpoint`,
  not accepted from an untrusted input (see `SECURITY.md`).

Under these conditions `load_checkpoint(key)` followed by `db.get(query)`
returns the value a fresh recomputation on the same declared state would, in all
three modes. The mechanisms that earn this:

- **Query identities are recomputed live in the loading process.** A query's
  identity pins the interpreter/build identity (see
  [Interpreter and Build Identity](#interpreter-and-build-identity)) and the
  full function-definition payload — a canonical typed code-object encoding,
  defaults, keyword defaults, comparator policies, and the definitions of
  transitively captured queries, functions, and modules — so a body or policy
  edit anywhere in the captured graph, or a build-configuration change,
  produces a different identity and the stale record simply misses. This
  encoding never depends on object reference counts and supports nested code
  and slice constants.
- **Inputs and dependency edges verify exactly.** Warmed records carry their
  real dependency edges; each input and sub-query dependency is re-checked
  against the live graph by digest before the record is trusted. Input policy
  digests independently include the interpreter/build identity.
- **Resources are re-probed or re-executed live.** A checkpoint dependency that
  is a resource is re-probed against the real world; a sub-query dependency that
  cannot be warmed is re-executed from its pinned code (the execute-to-verify
  frontier) and its result digest compared to the manifest. Resource identity
  is pinned as described under
  [Additional Kernel Properties](#additional-kernel-properties).
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

Residual limitations that stay outside the envelope: the stdlib-module gap of
limitation 5 applies across runs exactly as it does in-process; and a
checkpoint does not survive an interpreter or build-configuration change — such
records miss safely (they re-execute) rather than being trusted.

**5. Ambient module or class monkey-patching.**
Captured modules contribute their `__version__`, a SHA-256 digest of their
source or compiled file bytes, declared `__all__`, their module-level stable
constants read live, and — outside the standard library — the behavior reached
through statically resolvable attribute chains. Re-exported functions and
submodules pin their defining modules transitively; dynamic access to a custom
module is rejected when the behavior cannot be proven. A third-party version
bump, a source-file edit, or a namespace write to a captured module's constant
invalidates cached results that capture that module. Query definitions are
weakly memoized per `Database` for high-cardinality calls, and before a
memoized fingerprint is reused the memo rechecks the runtime build, the
observed definition objects, each captured module's file bytes and constants,
each captured resource's configuration digest, and each statically captured
attribute chain — re-resolving the chain and observing the definitions behind
the functions and wraps-decorated callables it reaches. Rebinding a statically
captured module attribute, or an entry in a directly captured class body,
therefore moves query identity at the next request, warm or fresh alike.

Two shapes stay outside that envelope by design. A chain that lands on a class
or a frozen dataclass instance is compared by that landing's identity, so what
its members hold or read — a plain method, `staticmethod`, `classmethod`,
`property` and `cached_property` alike — moves a fresh fold but not a memoized
one: `foo.Model.flag = True` is seen by a fresh `Database` and not by a warm
one. And a captured standard-library module folds the names of the paths read
off it rather than the behavior behind them, so patching a stdlib function or
class it reaches (`json.dumps = other`) is not detected at all; stdlib types
are pinned by name anchor and runtime build, never by a namespace walk. Route
such mutable state through an `Input` or a custom `Resource`.

**6. LRU eviction under active dependencies.**
If `max_query_nodes` is set low enough that an intermediate query is evicted while
a dependent is still active, the dependent will re-execute the intermediate from
scratch on its next request. This is correct but may degrade performance.
(See: `test_rewiring_with_lru_eviction`)

**7. Catching an exception raised by a child query.**
The edge a failing *resource* read publishes before its exception propagates has
no query-side equivalent: a query's record and dependency edges publish only
after it returns, so a query that catches an exception raised by a sub-query is
cached with no edge to it and is not re-executed when a later change would make
that sub-query succeed. Model a failure the caller means to handle as a returned
value, or route it through a `Resource`.
(See: `test_caught_query_failure_does_not_publish_a_dependency_edge`)

## Escape Hatches

- **`db.report_untracked_read(reason)`** — marks the current query as impure;
  forces re-execution on every request and disables backdating for that node.
  Downstream consumers re-verify but can still backdate if their own results are
  unchanged.
  (See: `test_report_untracked_read_forces_reexecution_on_every_request`,
  `test_impure_child_prevents_parent_backdating_unless_result_unchanged`)

- **`ValueAdapter`** — allows custom types to participate in freeze/thaw by
  implementing `freeze` and `thaw`. Adapters extend the condition 1 value
  boundary, so the boundary's obligations extend to them as laws:

  - **Deterministic, side-effect-free hooks.** `freeze` and `thaw` are pure
    functions of their arguments; neither reads ambient state. Adapter work
    at query boundaries runs under the condition 2 guard, so an intercepted
    read raises `UntrackedReadError` there.
  - **Owned results.** `freeze` returns a payload sharing no mutable state
    with the live value; `thaw` returns a value the caller owns outright.
  - **Semantic round-trip.** For any accepted value, `thaw(freeze(x))` is
    semantically equal to `x` wherever the adapted type is consumed.
  - **Pinned adapter state.** Adapter instance configuration is immutable for
    the registered lifetime, and the kernel enforces the law in-process: each
    adapter's instance configuration is digested at construction, every
    top-level request re-derives those digests, and `AdapterContractError`
    names the adapter key whose digest moved. Implementations and
    configuration participate in checkpoint identity; neither reaches a
    query's definition fingerprint, though an adapted value passed as an
    argument reaches that call's `args_digest` like any other argument. An
    adapter whose configuration cannot be digested at construction — slot
    state, or instance state the snapshot machinery refuses — is exempt from
    the in-process check; checkpoints refuse to trust its records instead.

  (See: `test_adapter_freeze_of_a_query_result_runs_under_the_guard`,
  `test_adapter_thaw_of_query_arguments_runs_under_the_guard`)

- **`eq=` / `cutoff=`** on `Input` and `@query` — allows custom equivalence.
  `eq=` compares detached operands, so nothing a comparator does to them can
  reach the stored snapshot: a recomputed query result reaches the comparator
  as thawed values in `checked` and `fast` and as detached `Frozen*` views in
  `strict` (a graph-shaped result as shared/cyclic views, not as the
  `FrozenGraph` envelope), while an input update reaches it as thawed values
  in every mode. `cutoff=` derives its snapshot-safe tokens from those same
  detached operands, and the tokens are compared under the canonical relation.
  A cyclic operand is handed over as the cycle it is, so a comparator or token
  function that walks it structurally recurses forever: `left == right` over
  cyclic operands raises `RecursionError` identically in `strict`, `checked`,
  and `fast`, and a policy declared on a query that can return a cyclic result
  must be cycle-aware.
  These are mutually exclusive. Cutoff tokens must be snapshot-safe, and the
  declared equivalence must be substitutive for dependents (condition 3) for
  the guarantee to hold on exact values.
  (See: `test_input_cutoff_suppresses_equal_updates`,
  `test_query_cutoff_backdates_and_skips_downstream`,
  `test_custom_eq_over_a_graph_shaped_result_sees_the_graph`,
  `test_structural_eq_over_cyclic_operands_raises_in_every_mode`)

## Output Reconciliation (Actions)

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

## Additional Kernel Properties

- Query identity includes the function-definition payload — supported captured
  values and the full definition payloads of transitively captured queries, so
  a body edit to a dependency query moves the parent's identity — plus the
  interpreter/build identity (see
  [Interpreter and Build Identity](#interpreter-and-build-identity)). A
  `functools.wraps`-decorated callable object is fingerprinted by its
  implementation type, its `__call__` definition and its instance state, with
  `__wrapped__` folded as additive information rather than as a substitute for
  them, and a class carrying a `__wrapped__` attribute is fingerprinted as a
  class; acceptance and state sensitivity are the same whether such a value is
  captured directly or reached as a module attribute. The `Query` handle's own
  attributes and metadata are folded beside the function, so writing one
  reparameterizes the query instead of changing it behind its records.
  Module-level types are pinned through their defining module identity.
- Mutable closure/global captures and local or dynamically unbound type
  objects are rejected, and so are reflective namespace reads — `globals()`,
  `locals()`, `vars()`, `eval` and `exec`, and `getattr`/`setattr`/`delattr`
  or a `__dict__` load beside an `importlib` reference or a `sys.modules`
  access. That rule is a conservative static read of the bytecode of the
  query's own function and of every callable folded into its identity, so a
  legitimate `getattr` beside a module-namespace handle is rejected too. Use
  `pyinc.explain_query_captures(fn)` to preview how each capture will be
  classified before the first `db.get()`; it also reports reflective namespace
  reads, and given a `Query` it covers the handle's own state.
- `Query` is public, and `@query(key=...)` accepts an explicit stable key; the
  default is `module:qualname`. Coroutine and generator queries are rejected at
  decoration time.
- `Resource[KeyT, ValueT, ProbeT]` and `Database.read_resource(...)` are public.
  Resource identity includes the resource's configuration, the implementations
  of every state-observation hook (`probe`, `load`, `probe_and_load`, and
  `identity`), and the interpreter/build identity. The built-in resources
  (condition 2) cover text, binary, environment, stat, directory, and
  path-resolution observation.
- `Database.inspect(...)` exposes the last recorded provenance tree as structured
  data. `Database.explain(...)` formats it for humans. Inspection is
  observational and does not force an extra verification pass;
  `Database.inspect_fresh(...)` runs verification first and then returns the
  provenance tree.
- `Database(max_query_nodes=...)` enables bounded memoization. Eviction happens
  at top-level request boundaries and affects query nodes only. Per-node timing
  uses fixed-size count/total/min/max/last aggregates; eviction also removes the
  node's call snapshot, timing profile, and unused query registry entry.
- Query labels consist of the query key, a short argument digest, and the query
  function's name — nothing else; formatting a graph or profile never calls
  argument `repr` or embeds argument values.
- Query records and dependency rewiring publish only after successful execution.
  A failed or cyclic evaluation keeps an earlier record usable and cannot leave a
  dangling dependency edge.
- The distributed package is PEP 561 typed via `py.typed`.

## Interpreter and Build Identity

Query identities, input policy digests, resource identities, and adapter
digests each embed a common interpreter/build identity. Its components include
the Python implementation, the full version tuple (including the prerelease
level), the `-O` optimize flag, the platform, `os.name`, UTF-8 mode, the
API/ABI tag, the multiarch/platform tag, the extension suffix, the build
string, and the pointer width.

## Thread Safety

Within a process, `Database` is thread-safe for concurrent use both across
independent instances and on a single shared instance. Each `Database` holds
a `threading.RLock` that serialises every public state read and mutation,
including queries, resources, inputs, statistics, profiles, dependency graphs,
resets, checkpoint operations, and subscriptions.

The ambient-read guard is installed globally exactly once and dispatches
per-context via a `ContextVar` stack of active databases — two threads inside
queries on different `Database` instances do not stomp each other's
enforcement, and raw I/O from a thread that is *not* inside any query continues
to work unaffected. If many threads share a single `Database`, work serialises
on the per-instance lock; if they hold separate `Database` instances they run
in parallel.

## Snapshot Serialization and Store Keys

The kernel derives deterministic content keys from the `Snapshot` union
(scalars, `FrozenList`, `FrozenDict`, `FrozenSet`, `FrozenRecord`,
`FrozenAdapterValue`, `FrozenGraph`, `FrozenRef`, and tuples of the same). The
length-prefixed, type-tagged byte grammar is stable across supported CPython
minor versions. Its digest helper is internal and is intentionally not exported
from `pyinc`; consumers use the `ArtifactStore` and checkpoint APIs rather than
constructing store keys themselves.

The public `serialize_snapshot` and `deserialize_snapshot` functions round-trip
the full snapshot grammar, including `FrozenGraph` / `FrozenRef`. Serialized
snapshots contain data only; adapted values still require the matching adapter
registry when they are thawed. A byte-grammar change is a cache-key break, so
older persisted records are rejected rather than silently reused.

## Checkpoint Save and Load

An outbound `ArtifactStore` (`InMemoryArtifactStore` /
`FileSystemArtifactStore`) optionally accepts every snapshot the kernel
freezes, keyed by its internally derived content digest, via `Database(store=...)`.
Snapshot bytes use the encoding described in
[Snapshot Serialization and Store Keys](#snapshot-serialization-and-store-keys).

On top of this, `Database.save_checkpoint(store=None) -> str` serialises the
current query and resource records — their snapshot bytes, call snapshots,
resource parameters, dependency edges, and per-adapter implementation digests —
into a content-addressed manifest (schema v6), returning a key prefixed with
`"ck"`. Adapter digests include `freeze`/`thaw` code, snapshot-safe instance
configuration, and the interpreter/build identity. Saving rejects an adapter
whose captures or state cannot be pinned; loading under such an adapter safely
misses and re-executes instead of thawing checkpoint bytes across an
unverifiable implementation boundary. Records whose cached value no longer
matches the live graph (a "dirty" save with no intervening `get`) are omitted
rather than persisted stale, so a reload never warms a value a fresh run would
not produce.

`Database.load_checkpoint(key, store=None)` validates every record, dependency,
input policy, probe, and referenced content address before atomically staging
any records; the manifest and byte-level checks that gate this are part of the
trust envelope described under limitation 4. The next `db.get(query)` verifies
dependencies as described there and reuses the stored result without
re-executing when everything checks out, or re-executes the affected query
otherwise. Both methods accept an optional `store=` kwarg for call-site store
injection; the store passed to `load_checkpoint` is also used for subsequent
snapshot loading if the `Database` was not constructed with a `store=`
argument.

## FileSystemArtifactStore

`FileSystemArtifactStore` accepts only digest-shaped keys, serializes each
digest with an OS-native process lock, and publishes flushed same-directory
temporary files atomically.

On POSIX it uses no-follow directory-relative operations, reopens the expected
parent and verifies its filesystem identity immediately before publication, and
rejects a directory rename it observes. POSIX cannot portably exclude a hostile
rename in the final interval between that check and the mutation, so store
roots must not be concurrently renamed by non-cooperating processes.

On Windows it pins every non-reparse directory component with a handle that
denies delete sharing, publishes from the temporary-file handle, and deletes
only through a validated file handle. Lock files retain the same protected
directory handle chain for the lock's lifetime.

Unsafe object, directory, or lock paths surface a typed `ArtifactStoreError`;
lock timeouts surface `ArtifactStoreLockError`.

## Push Observers

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

- `"backdated"` — the recomputation was backdated (see
  [The Guarantee](#the-guarantee));
- `"reused"` — dependencies were unchanged so no recomputation happened;
- `db.set(...)` / `db.set_many(...)` — input mutation alone does not
  execute any query. Observers fire on the next `get` that triggers
  dependent re-execution.

Dispatch model:

- Events are buffered on the outermost request scope and delivered **after**
  the kernel lock is released. A callback may therefore re-enter the
  database (e.g. call `db.get(...)`) without risk of deadlock. Inside a
  `request_span` the outermost request scope is the span itself: events
  buffered by gets inside the span are delivered when the outermost span
  closes — on a clean close and when the span body raises — and an inner
  span's close delivers nothing.
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

## Public Surface

Everything `pyinc` exports, and nothing else, carries the semver contract.
The offline documentation check compares these tables against
`pyinc.__all__` in both directions.

Core:

| Name | What it is |
|---|---|
| `Database` | The incremental query database: `get`, `set`, `set_many`, `inspect`, `inspect_fresh`, `explain`, `observe`, `request_span`, `request_inputs_changed`, checkpoint save/load. |
| `Input` | A declared, keyed input whose values enter through `db.set`. |
| `query` | Decorator declaring a pure incremental query. |
| `Query` | The declared-query object `@query` returns; readable from other queries and from `db.get`. Handle attributes are part of query identity: writing one moves the query's identity, so records stored under the old one no longer answer. |
| `Resource` | Base class for tracked external values; see the Resource hooks above. |
| `FileResource` | Text-file resource: content-hash probe, decoded string value. |
| `BinaryFileResource` | Byte-file resource: content-hash probe, raw bytes value. |
| `FileStatResource` | Stat-signature resource for existence/shape checks without content reads. |
| `FileStatSnapshot` | The frozen stat observation `FileStatResource` produces. |
| `EnvResource` | Environment-variable resource. |
| `DirectoryResource` | Directory-listing resource. |
| `ResolvedPathResource` | Symlink-aware path canonicalization as a tracked value. |

Values and snapshots:

| Name | What it is |
|---|---|
| `freeze` | Deep-convert a value into its canonical immutable snapshot. |
| `thaw` | Rebuild the mutable form of a snapshot. |
| `semantic_equal` | The kernel's canonical equality decision: two values are equal when their frozen snapshots' canonical encodings match. |
| `serialize_snapshot` | Encode a canonical snapshot into the stable `K2` byte grammar. |
| `deserialize_snapshot` | Decode and validate `K2` bytes back into a snapshot. |
| `FrozenList` | Immutable list view crossing cached boundaries. |
| `FrozenDict` | Immutable mapping view crossing cached boundaries. |
| `FrozenSet` | Immutable set view crossing cached boundaries. |
| `FrozenRecord` | Immutable dataclass snapshot preserving type identity. |
| `FrozenGraph` | Canonical encoding of a cyclic or shared object graph. |
| `FrozenRef` | Back-edge marker inside a `FrozenGraph` node table. |
| `FrozenAdapterValue` | Snapshot produced by a registered `ValueAdapter`. |
| `ValueAdapter` | Adapter making a foreign type snapshot-safe. |

Actions and stores:

| Name | What it is |
|---|---|
| `action` | Decorator declaring a filesystem-reconciling action over declared outputs. |
| `Action` | The declared-action object: `reconcile` and `plan`. |
| `Output` | One declared file output: relative path plus content. |
| `ReconcileResult` | What a reconcile did: created, updated, repaired, deleted, unchanged, plus `dry_run`. |
| `ArtifactStore` | Interface for durable content-addressed artifact storage. |
| `InMemoryArtifactStore` | Process-local store for tests and ephemeral use. |
| `FileSystemArtifactStore` | Durable on-disk store with advisory locking. |

Inspection and observation:

| Name | What it is |
|---|---|
| `DatabaseStatistics` | Node, edge, and work counters for one database. |
| `InspectionNode` | One node in an `inspect`/`explain` report: decision, reason, dependencies. |
| `DependencyGraphNode` | One labeled node in the exported dependency graph. |
| `QueryProfile` | Per-query execution/reuse/backdate counts from profiling. |
| `CaptureInfo` | One entry in an `explain_query_captures` report: a captured name, a reflective namespace read, or one piece of handle state. |
| `explain_query_captures` | Preview how a query's captures will be classified before first `get`; also reports reflective namespace reads and, given a `Query`, its handle state. |
| `Subscription` | Handle returned by `Database.observe`; closes the subscription. |
| `QueryChangeEvent` | Delivered to observers when a subscribed query's result changes. |
| `ObserverCallback` | Callback type receiving `QueryChangeEvent`s. |
| `ObserverErrorHook` | Callback type receiving exceptions raised by observers. |

Errors:

| Name | What it is |
|---|---|
| `PyIncError` | Base error for pyinc; every error below is catchable as this. |
| `MutationError` | A query mutated one of its boundary inputs. |
| `UntrackedReadError` | Code performed an undeclared external read. |
| `UnsupportedValueError` | A value cannot cross a cached boundary safely. |
| `AdapterContractError` | A registered adapter's instance configuration changed after `Database` construction. |
| `CycleError` | Query evaluation encountered a dependency cycle. |
| `InputKeyError` | An input key is invalid or conflicts within a database. |
| `CheckpointError` | Base error for durable-checkpoint failures. |
| `CheckpointVersionError` | A checkpoint uses an unsupported manifest or kernel version. |
| `CheckpointManifestError` | A checkpoint manifest is malformed or internally inconsistent. |
| `CheckpointIntegrityError` | Checkpoint bytes do not match their content address. |
| `ActionError` | Base error for output reconciliation failures. |
| `ActionPathError` | An action output path is unsafe or ambiguous. |
| `ActionManifestError` | An action ownership manifest is malformed or untrusted. |
| `ActionLockTimeoutError` | An action cannot acquire its filesystem lock in time. |
| `ArtifactStoreError` | Base error for artifact-store failures. |
| `ArtifactStoreKeyError` | An artifact key is malformed or unsafe. |
| `ArtifactStoreLockError` | An artifact-store lock cannot be acquired. |

## Verification

The from-scratch consistency guarantee is exercised by property-based
differential tests that compare incremental results against fresh-database
recomputation for the same declared state, across all three modes and
with/without LRU eviction. Finite tests are evidence, not a formal proof —
which is one reason the guarantee is stated with explicit conditions and
limitations:

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

The durable cross-run guarantee (limitation 4) is checked by the same
fresh-recomputation equivalence, extended to the checkpoint path:

- `test_checkpoint_reload_matches_fresh_recomputation` (property test in
  `tests/test_properties.py`) — reloads a checkpoint across all three modes and
  with/without LRU eviction, comparing `load_checkpoint` + `get()` against a
  fresh, cache-free run over the same edit sequence, and exercises the
  dirty-graph save path directly.
- `tests/test_checkpoint_cross_process.py` — a subprocess matrix that saves in
  one interpreter and reloads in another, checking that identities and digests
  line up across processes.
- `tests/test_checkpoint_trust.py` — the adversarial store and trust suite:
  bit-flipped and truncated snapshot bytes, tampered and wrong-version
  manifests, changed query/adapter/resource implementations, and
  runtime-import-reached dependencies each fall back to safe re-execution or a
  loud `ValueError` rather than serving a stale or tampered value.
