# Getting Started

This guide builds a small incremental program from the public `pyinc` API. It
covers the five choices every application makes: stable input keys, query
boundaries, tracked resources, an execution mode, and where side effects occur.

## Install

```console
python -m pip install pyinc
```

The wheel has no runtime dependencies and includes `pyinc`, `pyinc_tools`, and
`pyinc_codegen`.

## 1. Declare keyed inputs and queries

An `Input` is a base value supplied by your application. Give it a stable,
non-empty key: the key is its identity in a `Database` and in checkpoints.

```python docs-check
from pyinc import Database, Input, query

SOURCE = Input[str]("guide.source")


@query
def words(db: Database) -> tuple[str, ...]:
    return tuple(SOURCE.read(db).split())


@query
def word_count(db: Database) -> int:
    return len(words(db))


db = Database()
db.set(SOURCE, "incremental work stays focused")
assert db.get(word_count) == 4

db.set(SOURCE, "incremental work stays very focused")
assert db.get(word_count) == 5
```

Calling one query from another is ordinary Python. While `word_count` runs,
the database records its dependency on `words`, and `words` records its
dependency on `SOURCE`. `db.get(...)` is the top-level request boundary;
calling a `Query` inside another query delegates to the same database.

Use an explicit `@query(key="...")` only when the default
`module:qualified_name` identity is not stable enough for your deployment. An
`Input` or query accepts either `eq=` for custom equality or `cutoff=` for a
snapshot-safe comparison token, never both.

## 2. Track files and other resources

Inputs are pushed into a database. Resources are pulled from external state and
re-probed when a request needs them. Built-in resources cover text and binary
files, file metadata, environment variables, and directory listings.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, FileResource, query

FILES = FileResource()


@query
def nonempty_lines(db: Database, path: str) -> tuple[str, ...]:
    text = FILES.read(db, path)
    return tuple(line.strip() for line in text.splitlines() if line.strip())


with TemporaryDirectory() as directory:
    path = Path(directory, "names.txt")
    path.write_text("Ada\n\nGrace\n", encoding="utf-8")

    db = Database()
    assert db.get(nonempty_lines, str(path)) == ("Ada", "Grace")

    path.write_text("Ada\nLinus\n", encoding="utf-8")
    assert db.get(nonempty_lines, str(path)) == ("Ada", "Linus")
```

Inside a query, raw `open()`, `io.open()`, environment access, directory
listing, and `Path.iterdir()` are intercepted outside resource scope and raise
`UntrackedReadError`. For ambient reads the guard cannot intercept—such as
`os.open()`, subprocess output, time, random values, network calls, or C
extensions—call `db.report_untracked_read(reason)`. That node then executes on
every request and cannot backdate.

Custom resources implement the public `read`, `probe`, `load`, optional
`probe_and_load`, `identity`, and `label` hooks. Read the
[kernel contract](kernel-contract.md#conditions-for-from-scratch-consistency)
before relying on a custom probe across checkpoints.

## 3. Choose a mode

The mode changes boundary exposure and mutation checking, not dependency or
ambient-read tracking.

| Mode | Values seen by queries and callers | In-query mutation |
|---|---|---|
| `strict` | Immutable snapshots such as `FrozenList`, `FrozenDict`, `FrozenSet`, and `FrozenRecord` | A write fails immediately. |
| `checked` | Owned thawed copies | A before/after fingerprint detects mutation. |
| `fast` | Owned thawed copies | Not checked; deterministic, non-mutating queries are the caller's responsibility. |

Start with `Database(mode="strict")`. Move a measured workload to `checked` or
`fast` only when code genuinely needs ordinary containers at a boundary.

`freeze()` is a snapshot conversion, not a general object serializer. Lists,
mappings, sets, and dataclass instances become their corresponding `Frozen*`
records; tuples stay tuples. Shared or cyclic mutable graphs use
`FrozenGraph`/`FrozenRef`. `thaw()` reconstructs ordinary containers and graph
identity, but a dataclass becomes a dictionary unless a matching
`ValueAdapter` explicitly reconstructs its type.

## 4. Inspect what happened

`inspect()` is observational: it reports the most recently recorded decision
without starting another verification pass. Use `inspect_fresh()` when the
inspection itself must first verify current inputs and resources.

```python docs-check
from pyinc import Database, Input, query

NUMBER = Input[int]("guide.number")


@query
def doubled(db: Database) -> int:
    return NUMBER.read(db) * 2


db = Database()
db.set(NUMBER, 3)
assert db.get(doubled) == 6
assert db.inspect(doubled).last_decision == "executed"

db.set(NUMBER, 3)
assert db.get(doubled) == 6
assert db.inspect(doubled).last_decision == "reused"

print(db.explain(doubled))
print(db.statistics())
print(db.query_profile())
```

Hold a batch of reads inside `db.request_span()` when they should all see one
world: the batch shares a single resource-validation pass, and a `db.set` inside
it rolls the span so later reads re-derive. See
[Request Spans](kernel-contract.md#request-spans) for a worked example.

Use `dependency_graph()` for a machine-readable graph. `statistics()` reports
work counts and cache decisions; `query_profile()` reports timing aggregates.
Those APIs are better correctness and performance signals than inferring work
from wall-clock time alone.

## 5. Reconcile a first output

Queries describe values and remain free of side effects. An `@action` is a
separate top-level boundary that turns a complete desired `Output` set into
files.

```python docs-check
from pathlib import Path
from tempfile import TemporaryDirectory

from pyinc import Database, Input, Output, action, query

NAME = Input[str]("guide.action.name")


@query
def greeting(db: Database) -> str:
    return f"Hello, {NAME.read(db)}!\n"


@action(tool="getting-started/greeting-v1")
def write_greeting(db: Database) -> tuple[Output, ...]:
    return (Output.text("greeting.txt", greeting(db)),)


with TemporaryDirectory() as directory:
    db = Database()
    db.set(NAME, "Ada")

    plan = write_greeting.plan(db, root=directory)
    assert plan.created == ("greeting.txt",)
    assert plan.dry_run

    result = write_greeting.reconcile(db, root=directory)
    assert result.created == ("greeting.txt",)
    assert Path(directory, "greeting.txt").read_text() == "Hello, Ada!\n"
```

`plan()` runs the same preflight and locking as `reconcile()` without changing
outputs or the ownership ledger. The action owns only paths recorded
in its validated ledger. Review the [action contract](action-contract.md)
before sharing an output root between tools.

## Next steps

- Depend on the guarantee: [Kernel contract](kernel-contract.md)
- Analyze source and configuration: [Integration contract](integration-contract.md)
- Generate a file set: [Codegen guide](codegen-guide.md)
- Run analysis or connect an editor: [`pyinc-tools` guide](pyinc-tools-guide.md)
- Understand package boundaries: [Architecture](architecture.md)
