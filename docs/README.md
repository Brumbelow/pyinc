# pyinc Documentation

Start with [Getting Started](getting-started.md) for a runnable path through
inputs, queries, resources, modes, inspection, and actions. The documents below
each have one job.

## Learn and operate

| Document | Use it when you need to… |
|---|---|
| [Getting Started](getting-started.md) | Build and run a first incremental graph and declared-output action. |
| [`pyinc-tools` Guide](pyinc-tools-guide.md) | Install or operate the CLI/LSP, configure an editor, understand overlays, or troubleshoot. |
| [LSP Reference](lsp-reference.md) | Check an advertised method or its user-visible limitations. |
| [Demo](demo.md) | See the watcher running on a real workspace. |
| [Codegen Guide](codegen-guide.md) | Generate typed Python models from JSON Schema through the action layer. |
| [Migrating from 2.x](migration-v3.md) | Discard incompatible state and update code for 3.0. |

## Depend on a contract

| Document | Contract it defines |
|---|---|
| [Kernel Contract](kernel-contract.md) | From-scratch consistency, its three conditions, execution modes, checkpoints, and limits. |
| [Action Contract](action-contract.md) | Portable declared outputs, atomic publication, ownership, repair, deletion, and dry runs. |
| [Integration Contract](integration-contract.md) | Stable integration entrypoints/result types, supported input shapes, and key limits. |

## Extend or understand the project

| Document | Purpose |
|---|---|
| [Architecture](architecture.md) | Package ownership and the boundaries between kernel, integrations, tools, and codegen. |
| [Integration Authoring](integration-authoring.md) | Normative three-layer pattern for adding an integration without widening the kernel. |

The project [README](../README.md) remains the concise package overview and
documentation map used on GitHub and PyPI.
