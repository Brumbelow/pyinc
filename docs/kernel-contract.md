## Kernel Contract

`pyfoundinc` v1 is a correctness-first, in-memory incremental kernel.

The current contract is intentionally narrow:

- Query inputs and outputs must cross cached boundaries as snapshot-safe values or via registered `ValueAdapter`s.
- Query definitions may capture immutable values plus explicit `Input`, `@query`, and resource handles. Mutable closure/global state is rejected.
- `strict` exposes frozen snapshots only.
- `checked` exposes thawed owned copies and raises if a query mutates one of its boundary inputs.
- `fast` exposes thawed owned copies without mutation detection, but still uses semantic cutoffs and backdating.
- External state must be read through explicit resources such as `FileResource`, `FileStatResource`, `EnvResource`, or `DirectoryResource`.
- During query execution, raw ambient read helpers such as `open`, `os.getenv`, `os.environ`, `os.listdir`, `os.scandir`, and `Path.iterdir` are rejected as untracked reads unless called from inside a resource load/probe scope.
- Resource identity includes resource configuration. Custom resources must therefore be snapshot-safe or provide `identity()` for keying.
- `Database.report_untracked_read()` is the explicit impurity escape hatch. It makes that query always re-execute and disables backdating for that node.
- Default cutoff behavior is value-based. The kernel does not use object identity as semantic equality or backdating truth.
- `Database(max_query_nodes=...)` enables bounded query memoization. Eviction happens at top-level request boundaries, affects query nodes only, and drops evicted call snapshots.

Anything outside that envelope is intentionally rejected, marked impure, or left out of scope for v1.
