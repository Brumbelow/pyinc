# pyinc Documentation

Start with [Getting Started](getting-started.md) for a runnable path through
inputs, queries, resources, modes, inspection, and actions. The
[examples](../examples/) directory holds small runnable scripts;
`examples/calc/` is the worked example of a query graph that reconciles its
results to disk. The documents below each have one job.

## Learn and operate

| Document | Use it when you need to… |
|---|---|
| [Getting Started](getting-started.md) | Build and run a first incremental graph and declared-output action. |
| [`pyinc-tools` Guide](pyinc-tools-guide.md) | Install or operate the CLI/LSP, configure an editor, understand overlays, or troubleshoot. |
| [LSP Reference](lsp-reference.md) | Check an advertised method or its user-visible limitations. |
| [Demo](demo.md) | See the watcher running on a real workspace. |
| [FAQ](faq.md) | Compare pyinc with Salsa or `lru_cache`, see what is out of scope, or decide whether it fits your workload. |
| [Codegen Guide](codegen-guide.md) | Generate typed Python models from JSON Schema through the action layer. |
| [Releases and Verification](releases.md) | Check a published artifact, or see what the release pipeline enforces. |

## Depend on a contract

| Document | Contract it defines |
|---|---|
| [Kernel Contract](kernel-contract.md) | From-scratch consistency, its three conditions, execution modes, checkpoints, and limits. |
| [Action Contract](action-contract.md) | Portable declared outputs, atomic publication, ownership, repair, deletion, and dry runs. |
| [Integration Contract](integration-contract.md) | Stable integration entrypoints/result types, supported input shapes, and key limits. |

## Extend the project

| Document | Purpose |
|---|---|
| [Integration Authoring](integration-authoring.md) | Normative three-layer pattern for adding an integration without widening the kernel. |

## Packages

One distribution ships three top-level typed packages; the stable integration
surface is a subpackage of `pyinc`.

| Package | Stability | Owns | Contract |
|---|---|---|---|
| `pyinc` | Stable | The query kernel: `Database`, `Input`, `@query`, `Resource` and the built-in resources, `freeze`/`thaw` and the snapshot types, `ValueAdapter`, inspection and push observers, artifact stores and checkpoints, and the `@action` output layer. Domain-agnostic: no language, schema, or editor concept lives here. | [Kernel](kernel-contract.md), [Action](action-contract.md) |
| `pyinc.integrations` | Stable | Frozen result types and high-level entrypoints for Python source, TOML/JSON/XML/CSV/env configuration, requirements and installed packages, dependency checks, deep module resolution, scopes and symbols, and notebooks, plus the shared source geometry (`SourcePosition`, `SourceRange`, `DocumentMap`). Payload queries and decode helpers stay module-local. | [Integration](integration-contract.md) |
| `pyinc_tools` | Unstable | `pyinc-tools analyze`, the polling watcher and mirror workspaces, `WorkspaceSession` as the lock-owning façade, protocol-position conversion, and the stdio LSP/JSON-RPC server. | [Tools guide](pyinc-tools-guide.md), [LSP](lsp-reference.md) |
| `pyinc_codegen` | Unstable | JSON Schema analysis and typed-Python generation through the public query and action APIs. | [Codegen guide](codegen-guide.md) |

Both consumer packages use only public `pyinc` and `pyinc.integrations` names;
something a consumer needs that only a kernel internal provides is a reason to
widen the public API deliberately, not to reach around it. Integrations compose
at the query layer by importing one another's public `@query` functions, which
the kernel tracks as ordinary dependency edges
([composition](integration-contract.md#composition-and-experimental-helpers)).
The `bench/` harness is not shipped in the wheel; it pairs every timing with an
incremental-equals-fresh assertion, and its only extra dependency, `joblib`,
sits in the `bench` extra. What stays out of scope is listed in the
[FAQ](faq.md#what-is-out-of-scope).

The project [README](../README.md) remains the concise package overview.
