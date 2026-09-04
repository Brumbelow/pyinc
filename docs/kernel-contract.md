# Kernel Contract

`pyinc` is a correctness-first, in-memory incremental query kernel. This
document defines the guarantee it makes and the conditions under which the
guarantee holds; it is the stable semver contract for `src/pyinc`.

## The Guarantee

pyinc guarantees **from-scratch consistency**: the result of incremental
evaluation matches a fresh evaluation on the same declared inputs and
resources, provided the three conditions below hold. Outside those conditions
no guarantee is made.

When a recomputed value is canonically equal to the stored value, the record is
**backdated** (early cutoff): its `changed_at` revision does not advance, so
dependents stay green and are not recomputed.

The default equality decision is **canonical-encoding equality** over stored
snapshots: two values are equal exactly when their canonical snapshot encodings
are byte-identical. `1`, `1.0`, and `True` are three different values, `0.0`
differs from `-0.0`, and a canonical NaN equals a canonical NaN. The same
relation decides default input updates, resource probe comparisons, checkpoint
probe hints, and the token comparison behind a `cutoff=` policy, in every mode.
An `eq=` policy decides under its own relation instead.

## Conditions for From-Scratch Consistency

### 1. Value boundary ownership

Every value crossing a cached boundary — query arguments, query results,
`Input` values — must be snapshot-safe: an immutable scalar, a container
`freeze` can deep-convert, or a value handled by a registered `ValueAdapter`.
`freeze` converts `list` → `FrozenList`, `dict` → `FrozenDict`, `set` →
`FrozenSet` (kind `"set"`), `frozenset` → `FrozenSet` (kind `"frozenset"`), and
dataclasses → `FrozenRecord`; tuples are native members of the `Snapshot` union
and are frozen element-wise.

**Canonical order.** A frozen mapping holds its entries in a canonical order
derived from each key's snapshot digest: deterministic across processes and
platforms, but neither insertion order nor sorted order. `thaw` and every
mode's boundary exposure preserve that order, so a mapping iterates in the
stored order in all three modes. `FrozenSet` members are ordered by the same
rule, but that order belongs to the snapshot and to `strict`'s view of it; a
thawed `set` or `frozenset` is an ordinary unordered Python container. Sequences
are not reordered: a `FrozenList` keeps its element order and a `FrozenRecord`
its field declaration order. The canonical order is part of this contract:
every digest, store key, and checkpoint is derived from an encoding that reads
entries in stored order.

**Dataclasses and adapters.** A dataclass thaws to a dictionary, because the
kernel does not import and reconstruct arbitrary user classes; its snapshot is
tagged with the class's `__qualname__` alone, so same-named dataclasses in
different modules share a tag. The kernel's own resource snapshot types are the
exception: every `Database` registers `BUILTIN_ADAPTERS`, so a
`FileStatSnapshot` is rebuilt as itself at every boundary in every mode, and
registering an adapter for one of those types replaces the built-in entry.
`freeze()` and `thaw()` take only the registry they are handed, so thawing a
database-produced `FileStatSnapshot` outside a database means passing
`adapters=dict(BUILTIN_ADAPTERS)`; omitting it raises `UnsupportedValueError`
naming the adapter key.

**Hash positions.** A mapping key or set member must stay a hashable value with
a stable hash after thaw. `freeze` raises `UnsupportedValueError` for three
shapes in such a position. The first is a dataclass, a wrapper that thaws to a
mutable container, or a composite holding one. The second is an adapted value
whose payload contains a `FrozenGraph`, registered adapter or not, since the
state it rebuilds stays mutable after insertion. The third is a pair of
positions that are distinct under the snapshot encoding but collapse under
Python `==`/`hash` after thaw — `1` beside `1.0`, `True` beside `1`, `0.0`
beside `-0.0`, two adapter positions with the same key and collapsing payloads
— with the pair named in the error; `thaw`, `serialize_snapshot`, and
`deserialize_snapshot` refuse that case alike. Every canonical NaN is one class
for this purpose. The gate reads
payload encodings rather than running the adapter, so an adapter that thaws two
distinct payloads into equal values breaks the round-trip law under
[Escape Hatches](#escape-hatches) and is not caught here.

**Exposure by mode.** The kernel stores frozen snapshots. `strict` exposes
`Frozen*` views of them; `checked` and `fast` expose owned thawed values. The
views reject ordinary writes, but `object.__setattr__` still rebinds a field on
a view, so the kernel rebuilds a view for every exposure: rebinding changes the
view alone, and the next request answers from the stored record. A value with
a registered adapter is reconstructed through that adapter in every mode. No
external alias to a value that crossed the boundary can influence the stored
snapshot, in either direction: `freeze` returns a snapshot the kernel owns
outright, cloning an already-frozen wrapper rather than passing it through by
identity. Leaf scalars and all-leaf tuples are shared with the clone, since
nothing reflective can rebind a leaf.

**Shared and cyclic graphs.** `freeze` memoizes mutable containers (`list`,
`dict`, `set`, dataclass) by id and emits a `FrozenGraph(nodes, root)` envelope
when shared identity or a back-edge is detected; `thaw` rebuilds identity with
a two-pass allocate-then-fill, so a list containing itself round-trips. A pure
tree keeps the bare snapshot shape and still pays the deep freeze at every
level. Memoization covers exactly those four container types: a value crossing
through a `ValueAdapter`, a `tuple`, or a `frozenset` cannot be the target of a
back-edge, `freeze` rejects such a graph with `UnsupportedValueError`, and
`FrozenAdapterValue` is not a legal `FrozenGraph` node. A cyclic adapted object
must route its cycle through one of the four container types, or be decomposed
by its adapter into one, within the mode-shaped payloads law under
[Escape Hatches](#escape-hatches).

### 2. Tracked ambient reads

Every read of external state inside a query goes through the Resource API: a
built-in resource (`FileResource`, `BinaryFileResource`, `FileStatResource`,
`EnvResource`, `DirectoryResource`, `ResolvedPathResource`) or a user-defined
`Resource`. The public hooks are `read`, `probe`, `load`, `probe_and_load`,
`identity`, and `label`. On a warm request the kernel may check for an
unchanged world with `probe` alone and call `probe_and_load` only when the
probe misses, so `probe` and the probe component of `probe_and_load` must agree
on an unchanged world; a stored probe/value pair always comes from one
`probe_and_load` observation.

Resource identity includes the resource's configuration, the implementations of
`probe`, `load`, `probe_and_load`, and `identity`, and the interpreter/build
identity. A resource that keeps observation state of its own must define
`identity()` to return the configuration that distinguishes it. A configuration
that changes between two reads leaves the resource undefined: a change written
into a list, dict, or set the resource holds is refused at the read that
observes it, and a value rebound anywhere else makes the capturing query
re-fingerprint on every request, executing cold each time.

A resource hook observes the outside world and only the outside world. The
interception below is lifted for the extent of `probe`, `load`, and
`probe_and_load`, but a hook may not read back into the `Database` it is
observing for ([Reentrancy](#reentrancy)). Database-derived values reach a
resource through its **key**: the reading query reads them, declaring its
edges, and passes them in.

While a query runs, the kernel intercepts these calls and raises
`UntrackedReadError` when they happen outside a resource hook:

- `builtins.open` and `io.open`
- `os.getenv` and `os.environ` access
- `os.listdir` and `os.scandir`
- `Path.iterdir`

Reads this mechanism does not see (limitation 1) must be declared with
`db.report_untracked_read(reason)` ([Escape Hatches](#escape-hatches)). A
module imported for the first time inside a query body runs its module-scope
code inside that query's boundary, so a read performed while it initializes is
treated exactly as one written in the query body. Import at module scope.

### 3. Deterministic queries

Given the same tracked inputs, resources, and sub-query results, a query
returns a semantically equal value. Nondeterminism — timestamps, random
numbers, process state — is routed through a Resource or declared with
`report_untracked_read()`. Query bodies and equality/cutoff policies must have
fingerprintable implementations and snapshot-safe captures: immutable constants
and explicit `Input`, resource, and query handles are accepted; mutable closure
or global data, dynamically scoped local classes, and reflective namespace
reads are rejected before the first execution.

The reflective rule is a conservative static read of the bytecode of the query
function and of every callable folded into its identity. `globals()`,
`locals()`, `vars()`, `eval`, `exec`, and a `__globals__` load are refused on
their own. `getattr`, `setattr`, `delattr`, and a `__dict__` load are refused
beside a handle that can reach a module namespace: a `modules` attribute load,
the string `"modules"` beside one of those builtins, an `import_module`
attribute load, or a global load of `importlib`. Reaching a module namespace is
not itself refused — `sys.modules[name]` and `import_module(name)` with no
reflective builtin beside them are accepted, and neither reaches identity
(limitation 5). `pyinc.explain_query_captures(fn)` previews how each capture is
classified before the first `db.get()`.

`Input` keys and `@query`/`Query` keys are exactly `str` and non-empty; the
default query key is `module:qualname`. A `str` subclass, a `StrEnum` member
included, is rejected at construction with a message naming the plain string
to pass instead, because a key is stored as node identity and formatted into
labels and the checkpoint manifest. `Resource.label()` must likewise return
exactly `str`. Coroutine and generator queries are rejected at decoration time.

Custom `eq=`/`cutoff=` policies must be **substitutive** for every dependent:
when a policy reports two values unchanged, each dependent must produce a
semantically equal result from either value. A coarser policy is permitted, and
the guarantee it buys is correspondingly coarser. Backdating keeps dependents
at results computed from the earlier representative, so from-scratch
consistency then holds *modulo the declared equivalence* rather than on exact
values.

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

A resource whose `load` (or `probe_and_load`) raises is an observation, not the
absence of one. The kernel stores a **failure record** for that node, carrying
the probe observed alongside the failure, and ordinary probe comparison drives
invalidation, identically in every mode:

- The reading query records its edge on the failing resource before the
  exception propagates, so a later `get()` re-checks that node.
- The exception surfaces inside the query body, where the query's own
  `try`/`except` can see it; one no body handles propagates out of `db.get()`
  unchanged, including one raised while a dependent is being verified. Either
  way the node reports `failed` for `last_decision` and `last_recompute`.
- An unchanged failing probe does not move the revision, so a query that handled
  the failure stays green. A changed probe, or a transition between success and
  failure in either direction, invalidates the readers.
- A failure record holds no value. The first read in a request (or in the
  enclosing `request_span`) re-runs the load, later reads re-raise the same
  exception, and the exception is dropped when the request ends, so a node that
  keeps failing pins neither frames nor allocations.

Optional external state is therefore from-scratch consistent: a query that
returns a default when a file is missing returns the file's contents once it
appears, and the default again once it is removed. Two boundaries apply:

- **The probe must be total.** `probe()` models failure instead of raising:
  `FileResource.probe` returns `("missing",)` for an absent file, and a pipe,
  socket, or device answers as an absent path does. A resource whose `probe`
  also raises is outside the contract. With no record yet, the exception
  propagates unchanged and a query that catches it is cached as if it had no
  dependency at all: nothing in that process re-checks it, though it is still
  refused a checkpoint. With a record present, the node is reported as
  *changed* and marked *unconfirmed*, and its stored probe is retired until a
  real observation rewrites the record. Its direct readers re-execute and see
  the exception again; their dependents re-run only when the handled value
  differs. Entering that state moves the revision once per transition, not
  once per request, so `revision` settles while a resource stays unprobeable,
  and `inspect()` shows the node as its last real observation. A `load` that
  can raise different exceptions for one probe value must fold that
  distinction into the probe: invalidation compares probes, never messages.
- **Failures are not checkpointed.** A failure record, a failure the kernel
  could not record, and every record that transitively depends on either are
  omitted from a checkpoint and re-execute against live state after
  `load_checkpoint`.

`inspect()` and `explain()` show a failure node with decision `failed` and a
reason naming the exception; it counts in `resource_count`, a load that raised
is not a `resource_load`, and re-running a load on an unchanged failing probe
is not a `resource_probe_hit`. Unless the resource overrides `probe_and_load`,
the probe stored with a failure is taken just after it, so a `failed` node can
display a probe describing an already-healed world until the next request.

A `ReentrantDatabaseError` from a hook that read back into the database is not
a failure: it observes nothing, so no failure record and no probe is written. A
query that catches the refusal on a resource this `Database` has never loaded
is marked untracked with a reason naming the resource; a node that already held
a record keeps it and is marked unconfirmed, exactly as for a raising probe.

## Reentrancy

`Database` splits in two at the query boundary. Inside a query body the reading
surface is open, and only that: `get`, `read_input`, `read_resource`,
`report_untracked_read`, and a `request_span` that joins the request the
execution already opened. Everything else raises `ReentrantDatabaseError`
before doing anything at all: the administrative calls `set`, `set_many`,
`save_checkpoint`, `load_checkpoint`, `reset_statistics`,
`request_inputs_changed`, and `observe`; the observational `statistics`,
`query_profile`, `dependency_graph`, `explain`, `inspect`, `inspect_fresh`, and
the `revision` property; and `Subscription.unsubscribe`. An administrative call
would move state the running execution derives from; an observational one
answers with a function of the database's own history, which is exactly what a
query result may not depend on.

Inside a resource hook there is no open half: every call back into the
`Database`, the reading surface included, raises `ReentrantDatabaseError`
naming the hook, whether or not a query is running. A thread started inside a
query body or a resource hook stands where its parent does and is refused
rather than left to wait on the lock ([Thread Safety](#thread-safety)).

## Explicit Limitations

These fall **outside** the soundness envelope, except where an entry says
otherwise: the durable-cache entry states the conditions under which the
guarantee survives into a later process, and the eviction and caught-failure
entries hold it in-process at the cost of incrementality.

**1. Unintercepted ambient reads.** The condition 2 guard covers an enumerated
set of entry points, not a category of behaviour. Everything else that observes
external state bypasses it silently unless declared with
`db.report_untracked_read(reason)`: `os.open()`, C-extension I/O, subprocess
output, network calls, `ctypes` memory access, and similar. Four gaps sit close
enough to the guarded set to be named:

- *File metadata.* `os.stat`, `os.lstat`, `os.access`, `Path.stat`,
  `Path.exists`, `Path.is_file`, `Path.is_dir`, `Path.resolve`, and the
  `os.path` helpers built on them return normally from inside a query, so a
  query that asks whether a file exists, or how large or how recent it is,
  records no edge and is reused unchanged after the file changes. Route the
  observation through `FileStatResource` or `ResolvedPathResource`. Declaring
  it removes stale reuse for the declaring node alone.
- *The byte-oriented environment.* `os.getenvb` and `os.environb` are not
  intercepted.
- *The working directory.* `os.getcwd` and `Path.cwd` are not intercepted; pass
  absolute paths as query arguments instead.
- *Threads the query did not start.* The guard covers threads a query body
  starts, at any depth, and nothing else. A pre-warmed pool, an executor built
  at module scope, or a reused `ThreadPoolExecutor` worker is untracked, and a
  query that waits on such a worker while the worker waits on the state lock
  deadlocks rather than being refused. Hand the work to a thread the query
  starts, or declare it.

**2. Custom `eq=`/`cutoff=` with side effects.** If a policy callback performs
ambient reads or mutations, the equivalence check itself becomes a hidden
dependency the kernel cannot detect, and it may then make incorrect backdating
decisions. Policy captures and callable instance state must still be
snapshot-safe.

**3. Mutation in `fast` mode.** `fast` does not detect mutation of boundary
values inside queries. The stored snapshot is safe, but a mutating query may
observe corrupted intermediate state. Use `checked` or `strict`.

**4. Durable cross-run cache (trusted, under stated conditions).** A durable
`ArtifactStore` checkpoint ([Checkpoint Save and Load](#checkpoint-save-and-load))
is trusted for from-scratch consistency across processes and runs when **all**
of the following hold:

- **(i)** every `Input` the checkpoint depends on is set before
  `load_checkpoint`, uses the same explicit non-empty key across runs, and has
  the same equality or cutoff policy; compatible aliases resolve to one logical
  input, and a database rejects aliases with divergent policies;
- **(ii)** resources satisfy the probe contract across runs: a resource's probe
  changes whenever its `load` result changes, and probe values are
  snapshot-safe and process-independent;
- **(iii)** adapters for any adapted snapshot type are registered in the loading
  process with unchanged `freeze`/`thaw` implementations;
- **(iv)** the checkpoint key and the store it loads from come from a trusted
  channel. Content addressing proves that bytes match the key they were asked
  for by; it does not authenticate where the key came from, and an
  attacker-selected key names an attacker-selected manifest (see
  [SECURITY.md](../SECURITY.md));
- **(v)** the loading database runs in the same mode as the one that saved the
  checkpoint. The manifest records the saving mode, and a load into another
  mode refuses with `CheckpointModeError` before staging any record.

Under these conditions `load_checkpoint(key)` followed by `db.get(query)`
returns the value a fresh recomputation on the same declared state would in that
mode. A checkpoint record warms when every pinned object and every statically
captured module chain the record depends on is unchanged — a sub-query or
resource reached through a statically captured module attribute is pinned
exactly as a directly captured one is, and warms on the same terms — and a
record the kernel cannot verify is re-executed. Identities are recomputed live
in the loading process, edges are re-checked by digest, resources are re-probed
against the real world, and every snapshot loaded from the store is rejected
unless `sha256` of its bytes equals the digest it was keyed by. Query subgraphs
reached only through a runtime import or dynamic dispatch, records marked
untracked, corrupted or missing store bytes, and adapter mismatches are skipped
and re-executed. A tampered, truncated, wrong-version, wrong-kernel-fingerprint,
or wrong-mode manifest is a property of the manifest as a whole, so the load is
refused outright with a typed `CheckpointError` subclass and stages nothing.

Identities do not depend on the hash seed or the install path. A checkpoint
written under one `PYTHONHASHSEED` warms a process running under another, and
one written from one installation warms a byte-identical installation unpacked
at another prefix, because identity pins where a definition sits in its
package rather than the absolute path. A body that reads `__file__` or takes a
path argument still folds that path, and code compiled without a source file
of its own (`<string>`, a sourceless `.pyc`) folds its filename verbatim. A
checkpoint does not survive an interpreter or build-configuration change; such
records miss safely.

**5. Ambient module or class monkey-patching.** Captured modules contribute
their `__version__`, a SHA-256 digest of their source or compiled file bytes,
their declared `__all__`, and — outside the standard library — their
module-level stable constants read live and the behavior reached through
statically resolvable attribute chains. A standard-library, built-in, or frozen
module contributes only the constants on the attribute paths the capturing
query's own code reads off it; the interpreter/build identity already pins the
build. Re-exported functions and submodules pin their defining modules
transitively; dynamic access to a custom module is rejected when the behavior
cannot be proven. Rebinding a statically captured module attribute, or an
entry in a directly captured class body, moves query identity at the next
request, warm or fresh alike. Where the interpreter exposes no Python evaluator
to observe (a `type` alias before 3.14, a runtime-constructed `TypeVar`'s
bound) the payload anchors each class it reaches to its live module binding,
and rebinding such a binding, a base, a metaclass, or a directly bound body
class refuses loudly instead of moving identity.

Three shapes stay outside that envelope; route such state through an `Input`
or a `Resource`:

- A chain that lands on a class or a frozen dataclass instance, named directly
  or held inside an immutable container, is compared by the landing's identity,
  so what its members hold or read moves a fresh fold but not a memoized one:
  `foo.Model.flag = True` is seen by a fresh `Database` and not by a warm one.
  When such a class stops being its module's live binding, a warm database
  serves the stored answer while a fresh computation refuses.
- A captured standard-library module folds the names of the paths read off it,
  not the behavior behind them, so patching a stdlib function or class
  (`json.dumps = other`) is not detected, warm or fresh. A path read through
  `getattr` with a computed name contributes no path.
- Outside the standard library, a module that stores a process id, an import
  timestamp, or anything derived from them at module scope makes every
  identity that captures it process-varying.

**6. LRU eviction under active dependencies.** `Database(max_query_nodes=...)`
bounds query memo nodes only, evicting at top-level request boundaries; inputs
and resources stay resident. If an intermediate query is evicted while a
dependent is still active, the dependent re-executes it on its next request:
correct, but slower. Eviction also removes the node's call snapshot, timing
profile, and unused registry entry.

**7. Catching an exception raised by a child query.** A query's record and
edges publish only after it returns, so a failed or cyclic evaluation keeps an
earlier record usable and cannot leave a dangling edge, and a query that
catches an exception raised by a sub-query holds a value with no edge to what
produced it. The catching query is marked untracked with a reason naming the
sub-query, so it re-executes on every request, never backdates, and is kept
out of checkpoints with everything above it. A later change that makes the
sub-query succeed therefore reaches the caller, matching a fresh `Database`;
what is lost is incrementality. Model a failure the caller means to handle as
a returned value, or route it through a `Resource`. One shape is exempt: a
query refused for asking for *itself* raises `CycleError` before any work
starts, so catching that in the query that asked for itself leaves an ordinary
reusable record. A parent catching a child's self-cycle, or a cycle that
reaches back through another query, is marked like any other caught failure.

## Escape Hatches

- **`db.report_untracked_read(reason)`** marks the current query as untracked:
  it re-executes on every request and never backdates; dependents re-verify
  but can still backdate when their own results are unchanged. The kernel
  applies the same mark on its own account wherever a value rests on something
  no record describes: a caught sub-query exception (limitation 7) and a caught
  hook refusal on a never-loaded resource
  ([Failing Resource Loads](#failing-resource-loads)).

- **`ValueAdapter`** lets a custom type participate in freeze/thaw. Adapters
  extend the condition 1 boundary, so its obligations extend to them as laws:
  - *Deterministic, side-effect-free hooks.* `freeze` and `thaw` are pure
    functions of their arguments. Adapter work at query boundaries runs under
    the condition 2 guard, so an intercepted read raises `UntrackedReadError`.
  - *Owned results.* `freeze` returns a payload sharing no mutable state with
    the live value; `thaw` returns a value the caller owns outright.
  - *Semantic round-trip.* `thaw(freeze(x))` is semantically equal to `x`
    wherever the adapted type is consumed.
  - *Mode-shaped payloads.* `thaw` runs at every boundary in every mode. Its
    first argument is the payload as the snapshot holds it; its second, the
    recursive callable, yields values the way that mode exposes them, so one
    implementation serves all three modes. A payload that freezes to a `list`,
    `dict`, `set`, or dataclass inside a shared or cyclic value, or a tuple
    holding such a container, cannot be handed back whole, so `freeze` refuses
    the value with `UnsupportedValueError`; an adapter with a container to
    carry decomposes it into tuples and scalars.
  - *Pinned adapter state.* Adapter instance configuration is immutable for the
    registered lifetime: it is digested at construction and re-derived on every
    top-level request, and `AdapterContractError` names the adapter key whose
    digest moved or can no longer be derived. Implementations and configuration
    participate in checkpoint identity, not in a query's definition
    fingerprint. An adapter whose configuration cannot be digested contributes
    no digest, is skipped by the in-process check, and is refused trust by
    checkpoints.

  The built-in adapters (`BUILTIN_ADAPTERS`: a stateless `FileStatAdapter` for
  `FileStatSnapshot`) hold no instance configuration and ship with the kernel,
  so their digests are derived once per process rather than at every boundary.

- **`eq=` / `cutoff=`** on `Input` and `@query` declare a custom equivalence;
  they are mutually exclusive. `eq=` compares detached operands, so nothing a
  comparator does to them reaches the stored snapshot: a recomputed result
  arrives as thawed values in `checked` and `fast` and as detached `Frozen*`
  views in `strict`, an input update as thawed values in every mode. `cutoff=`
  derives snapshot-safe tokens from those same operands, compared under the
  canonical relation. A cyclic operand is handed over as the cycle it is, so a
  structural `left == right` raises `RecursionError` in every mode; a policy on
  a query that can return a cyclic result must be cycle-aware. The declared
  equivalence must be substitutive (condition 3) for the guarantee to hold on
  exact values.

## Output Reconciliation (Actions)

Queries are pure and never write. The separate action layer (`@action`,
`Output`, `ReconcileResult`; see [action-contract.md](action-contract.md))
reconciles a query-derived desired-output set against the filesystem with
atomic writes, content-hash change and tamper detection, ownership-ledger
orphan deletion, and dry-run planning. Reconciliation runs at top level only,
never inside a query, so it changes nothing about query semantics, the value
membrane, untracked-read enforcement, or the modes. The from-scratch guarantee
lifts to owned output files under the action contract's soundness boundary.

## Interpreter and Build Identity

Query identities, input policy digests, resource identities, and adapter
digests embed one interpreter/build identity: the implementation, the full
version tuple, the platform, `os.name`, the byte order, the API/ABI tag, the
multiarch tag, the extension suffix, the build string, the pointer width, and
every `sys.flags` field except `hash_randomization`, folded by name. Hash
randomization is excluded deliberately: two processes that leave
`PYTHONHASHSEED` unset carry the same flag and different hash orders, so
folding it would separate nothing a query's answer can depend on. Route a
dependence on hash order through an `Input` or a `Resource`. A free-threaded
build, another minor version, or another platform derives different identities
and misses safely.

## Thread Safety

Within a process, `Database` is thread-safe both across independent instances
and on one shared instance. Each `Database` holds a `threading.RLock` that
serialises every public read and mutation. Threads sharing one instance
serialise on its lock; threads holding separate instances do not contend. That
is not parallelism: on a default build CPU-bound Python work does not run in
parallel across threads, and parallel speedup needs separate processes. The
ambient-read guard is installed globally exactly once and dispatches per
context through a `ContextVar` stack of active databases, so threads inside
queries on different instances do not disturb each other's enforcement, and
raw I/O from a thread that is not inside any query is unaffected.

A thread a **query body** starts inherits the boundary of that query. Its
undeclared ambient reads raise `UntrackedReadError`, and its calls back into
the same `Database` raise `ReentrantDatabaseError` instead of waiting for the
lock, which the query body holds for its whole execution; a child that waited
while the body waited for it would deadlock. The boundary ends when the query
does. A thread a **resource hook** starts inherits the hook's standing, with or
without a query running: its raw reads are allowed, and only its calls back
into the `Database` refuse. A hook's boundary is a depth rather than a frame,
so a thread that outlives its hook stays refused, where a survivor of a query
returns to normal. Threads that already existed when the query began are
outside every boundary (limitation 1).

## Snapshot Serialization and Store Keys

The kernel derives deterministic content keys from the `Snapshot` union —
scalars, `FrozenList`, `FrozenDict`, `FrozenSet`, `FrozenRecord`,
`FrozenAdapterValue`, `FrozenGraph`, `FrozenRef`, and tuples of the same — using
a length-prefixed, type-tagged byte grammar that is stable across supported
CPython minor versions. The digest helper is internal; consumers use the
`ArtifactStore` and checkpoint APIs rather than constructing store keys.
`serialize_snapshot` and `deserialize_snapshot` round-trip the full grammar;
serialized snapshots contain data only, so adapted values still need the
matching adapter registry when thawed. A byte-grammar change is a cache-key
break: older persisted records are rejected rather than reused. The grammar
requires the canonical order rather than recording whatever order it is given,
so a hand-assembled `FrozenDict` or `FrozenSet` in any other order is rejected
with `UnsupportedValueError` by `freeze`, `thaw`, `serialize_snapshot`, and
`deserialize_snapshot` alike, and one value cannot hold two store keys.

## Checkpoint Save and Load

An `ArtifactStore` (`InMemoryArtifactStore`, `FileSystemArtifactStore`) passed
as `Database(store=...)` receives every snapshot the kernel freezes, keyed by
its content digest. An implementation owes three things: `get` returns `None`
for a digest it does not hold and never raises for one; `put` is idempotent for
equal bytes under the same digest and raises `ValueError` when a digest would be
rebound to different bytes; `contains` reports presence, defaulting to
`get(...) is not None`. Every store handed to `Database(store=...)`,
`save_checkpoint(store=...)`, or `load_checkpoint(..., store=...)` is validated
against the protocol at that call, and a missing method, or an explicit
protocol subclass implementing neither `get` nor `put`, raises `TypeError` at
injection. `InMemoryArtifactStore.keys()` returns a read-only view.

`Database.save_checkpoint(store=None) -> str` serialises the current query and
resource records — snapshot bytes, call snapshots, resource parameters,
dependency edges, per-adapter implementation digests, and the saving mode —
into a content-addressed manifest (schema v8) and returns a key prefixed with
`"ck"`. Saving rejects an adapter whose captures or state cannot be pinned.
Records whose cached value no longer matches the live graph (a dirty save with
no intervening `get`) are omitted, so a reload never warms a value a fresh run
would not produce.

`Database.load_checkpoint(key, store=None)` re-hashes the manifest against the
key, validates every record, dependency, input policy, probe, and content
address, and only then stages records atomically, under the trust rules of
limitation 4. The store passed to `load_checkpoint` is also used for later
snapshot loads if the `Database` was constructed without one. Manifest schema
v8 rejects older manifests with `CheckpointVersionError`; stale checkpoints are
re-saved, never migrated. A store found holding different bytes under a digest
the kernel is publishing is an integrity fault: `save_checkpoint` raises, and a
value re-executed because the load skipped those bytes meets the same refusal
when it is persisted again. Recovery is removing the corrupt object or
supplying a clean store.

### FileSystemArtifactStore

`FileSystemArtifactStore` accepts only digest-shaped keys, serialises each
digest with an OS-native process lock, and publishes flushed same-directory
temporary files atomically. On POSIX it uses no-follow directory-relative
operations and verifies the parent's filesystem identity immediately before
publication; POSIX cannot exclude a hostile rename in the final interval, so
store roots must not be concurrently renamed by non-cooperating processes. On
Windows it pins every directory component with a handle that denies delete
sharing and publishes from the temporary-file handle. Unsafe paths surface
`ArtifactStoreError`; lock timeouts surface `ArtifactStoreLockError`.

## Push Observers

`Database.observe(callback, query, *args, **kwargs)` registers a callback that
fires when the identified query node's stored value moves, and returns a
`Subscription` whose `unsubscribe()` detaches exactly that registration;
repeated unsubscribes are no-ops, and no change committed after `unsubscribe()`
returns reaches it. Each `observe` call is its own registration. Both calls are
outside-only ([Reentrancy](#reentrancy)).

A callback fires exactly when the node's stored value moved during a top-level
`get` / `inspect` / `inspect_fresh` / `explain` call or inside a
`request_span`: a cold execution, or a re-execution that advanced `changed_at`.
It does not fire on a backdate, a reuse, a re-execution that landed a
byte-identical value on an untracked node, or `db.set` / `db.set_many` alone;
input mutation executes nothing, and observers fire on the next `get`. An
untracked node forfeits its `eq=`/`cutoff=` policy for events as it does for
backdating: a byte-different value its policy would call equal still fires.

Events are buffered on the outermost request scope (inside a `request_span`,
the span itself, delivered when the outermost span closes, cleanly or by
raising) and delivered after the kernel lock is released, so a callback may
re-enter the database. The callback list is snapshotted when the change commits:
a subscription added after the change does not receive it, one removed before
delivery begins receives nothing, and one removed during dispatch still
receives events already snapshotted. Callback exceptions go to the
`observer_error_hook` passed to `Database(...)` (default: a one-line stderr
log) and do not suppress sibling callbacks. Subscriptions survive LRU eviction
of their node; a re-execution after eviction fires as a cold execution, and the
stream does not promise strictly climbing `changed_at` values.
`QueryChangeEvent` carries the node's `query_id`, `args_digest`, the decision
that produced the move (always `"executed"`), and the `changed_at` /
`verified_at` revisions at execution time.

## Node Record Fields

`Database.inspect(...)` returns the last recorded provenance tree as structured
data and `Database.explain(...)` formats it; neither runs a verification pass,
and neither advances a node's decision fields. `Database.inspect_fresh(...)`
verifies first. Inspecting a node that has no record executes it, and every
inspection opens a request or joins the enclosing span. Query labels consist
of the query key, a short argument digest, and the function name; formatting a
graph or profile never calls argument `repr`. Each node in a report carries
four decision fields:

- **`last_decision`** — what the most recent request that *touched* this node
  concluded: `executed`, `reused`, `backdated`, or `failed`, and `pending`
  before any request recorded one. A second reach within one request is
  recorded as `reused` without re-checking anything.
- **`last_recompute`** — the outcome of the last time the node's body actually
  ran: `executed`, `backdated`, or `failed`, and `never` before it ever has. An
  input node records the set that gave it its value; a resource node its load.
  A reuse does not advance it, so a node may read `last_decision` `reused` and
  `last_recompute` `backdated`. A checkpoint restore stamps both fields
  `reused` on a node this database never ran.
- **`reason`** — a short phrase for a reader; the definitions here are stated
  in terms of the decisions, not the phrases.
- **`untracked_reasons`** — the reasons recorded during the node's most recent
  run, in order and not deduplicated, rebuilt from that run rather than
  accumulated: the reasons passed to `db.report_untracked_read(...)` plus the
  ones the kernel recorded on its own account, each naming the sub-query or
  resource it caught.

## Public Surface

Everything `pyinc` exports, and nothing else, is inside the semver contract
this document defines; the package is PEP 561 typed. `pyinc.integrations`
states its own stable surface in [integration-contract.md](integration-contract.md);
`pyinc_tools` and `pyinc_codegen` are unstable — see
[SECURITY.md](../SECURITY.md). `scripts/check_docs.py` compares these tables
against `pyinc.__all__` in both directions.

Core:

| Name | What it is |
|---|---|
| `Database` | The incremental query database: `get`, `set`, `set_many`, `inspect`, `inspect_fresh`, `explain`, `observe`, `request_span`, `request_inputs_changed`, checkpoint save/load. Its administrative and observational entry points are outside-only: reached from a query body they raise `ReentrantDatabaseError` instead of answering (see [Reentrancy](#reentrancy)). |
| `Input` | A declared, keyed input whose values enter through `db.set`. |
| `query` | Decorator declaring a pure incremental query. |
| `Query` | The declared-query object `@query` returns; readable from other queries and from `db.get`. Handle attributes are part of query identity: writing one moves the query's identity, so records stored under the old one no longer answer. |
| `Resource` | Base class for tracked external values; the hooks are listed under condition 2. |
| `FileResource` | Text-file resource: content-hash probe, decoded string value. |
| `BinaryFileResource` | Byte-file resource: content-hash probe, raw bytes value. |
| `FileStatResource` | Stat-signature resource for existence/shape checks without content reads. |
| `FileStatSnapshot` | The frozen stat observation `FileStatResource` produces. |
| `FileStatAdapter` | The stateless built-in `ValueAdapter` rebuilding `FileStatSnapshot` at every cached boundary. |
| `BUILTIN_ADAPTERS` | Read-only map of the adapters every `Database` registers for the kernel's own value types; also the registry to hand `freeze`/`thaw` outside a database. |
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
| `FrozenRecord` | Dataclass snapshot: the class's `__qualname__` (no module component, so same-named classes in different modules share a tag) plus ordered fields. Thaws to a dict; reconstructing the original class requires a `ValueAdapter`. |
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
| `DatabaseStatistics` | Node, input, query, and resource counts plus work counters for one database. Fields: `node_count`, `input_count`, `query_count`, `resource_count`, `query_executions`, `query_reuses`, `query_backdates`, `resource_loads`, `resource_probe_hits`, `input_sets`, `input_equal_ignores`, `evictions`, `total_requests`. |
| `InspectionNode` | One node in an `inspect`/`explain` report; its decision fields are defined in [Node Record Fields](#node-record-fields). Fields: `label`, `kind`, `changed_at`, `verified_at`, `last_decision`, `last_recompute`, `reason`, `untracked_reasons`, `dependencies`. |
| `DependencyGraphNode` | One labeled node in the exported dependency graph. Fields: `label`, `kind`, `changed_at`, `verified_at`, `last_decision`, `is_untracked`, `dependency_labels`. |
| `QueryProfile` | Per-query timing aggregate from `query_profile()`: one execution count with the total, mean, minimum, maximum, and last nanosecond figures. Reuse and backdate counts live on `DatabaseStatistics`. Fields: `query_label`, `execution_count`, `total_ns`, `mean_ns`, `min_ns`, `max_ns`, `last_ns`. |
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
| `ReentrantDatabaseError` | A call re-entered the database from inside its own execution — from inside a query body, from inside a resource hook, or from a thread spawned inside a query execution. |
| `InputKeyError` | An input key is invalid or conflicts within a database. |
| `CheckpointError` | Base error for durable-checkpoint failures. |
| `CheckpointVersionError` | A checkpoint uses an unsupported manifest or kernel version. |
| `CheckpointManifestError` | A checkpoint manifest is malformed or internally inconsistent. |
| `CheckpointIntegrityError` | Checkpoint bytes do not match their content address. |
| `CheckpointModeError` | A checkpoint saved in one database mode was loaded into a database running another; refused before anything is staged. |
| `ActionError` | Base error for output reconciliation failures. |
| `ActionPathError` | An action output path is unsafe or ambiguous. |
| `ActionManifestError` | An action ownership manifest is malformed or untrusted. |
| `ActionLockTimeoutError` | An action cannot acquire its filesystem lock in time. |
| `ArtifactStoreError` | Base error for artifact-store failures. |
| `ArtifactStoreKeyError` | An artifact key is malformed or unsafe. |
| `ArtifactStoreLockError` | An artifact-store lock cannot be acquired. |
| `CompositionError` | A high-level integration entrypoint was called from inside a query body. |

## Verification

The guarantee is exercised by property-based differential tests that compare
incremental results against fresh-database recomputation over the same edit
sequences, across all three modes and with and without LRU eviction
(`tests/test_properties.py`), and by dedicated suites for dependency rewiring,
boundary mutation, and the checkpoint path: `tests/test_checkpoint_trust.py`
(tampered bytes and manifests, all six cross-mode load pairings, changed
implementations), `tests/test_checkpoint_cross_process.py` (save in one
process, reload in another, under differing hash seeds and install prefixes),
and `tests/test_fingerprint_process_stability.py` (every shipped identity is
the same in three processes under different hash seeds). Finite tests are
evidence, not a proof, which is one reason the guarantee is stated with
explicit conditions and limitations.
