# Public API and semantic-versioning tiers

The wheel ships several public surfaces. Semantic versioning applies to a name
only when it appears in the corresponding `__all__` tier below; a documented
behavioral contract for that name is stable to the extent stated in the linked
contract or guide.

1. `pyinc.__all__` is the kernel, resource, snapshot, checkpoint, inspection,
   observer, store, and action surface enumerated in the
   [kernel contract](kernel-contract.md#public-surface).
2. `pyinc.integrations.__all__` is the aggregate typed Layer-3/result surface
   enumerated in the [integration contract](integration-contract.md). Layer-3
   functions are top-level consumer boundaries.
3. Each integration module's own `__all__` is a stable module-level tier. It
   includes its public result and Layer-3 names plus any Layer-2 query handles
   intended for query composition. A Layer-2 name need not be re-exported by
   `pyinc.integrations` to carry this module-level contract.
4. `pyinc_tools.__all__` and `pyinc_codegen.__all__` are stable consumer-package
   surfaces. Their behavior is defined by the
   [tooling guide](pyinc-tools-guide.md), [LSP reference](lsp-reference.md), and
   [codegen guide](codegen-guide.md).

Names outside these lists, underscore-prefixed helpers, internal resource and
decode modules, and imports that merely happen to resolve are implementation
details. CLI commands and LSP methods documented in their public guides are
behavioral interfaces even though they are not Python `__all__` names.

## Consumer package exports

| Public module | Stable `__all__` names |
|---|---|
| `pyinc_tools` | `AnalysisDiagnostic`, `CallHierarchyCallSite`, `CallHierarchyIncomingCall`, `CallHierarchyItem`, `CallHierarchyItemKind`, `CallHierarchyOutgoingCall`, `CodeAction`, `CodeActionEdit`, `CodeActionKind`, `CodeLens`, `CompletionItem`, `CompletionItemKind`, `DeclarationLocation`, `DocumentHighlight`, `DocumentHighlightKind`, `DocumentLink`, `FileAnalysisResult`, `FileDeletionEdit`, `FileRenameEdit`, `FoldingRange`, `FoldingRangeKind`, `InlayHint`, `InlayHintKind`, `LinkedEditingRange`, `PollingWorkspaceWatcher`, `RenameEdit`, `RenameResult`, `RenameStatus`, `SelectionRange`, `SemanticToken`, `SemanticTokenModifier`, `SemanticTokenType`, `SignatureHelp`, `SignatureParameterInfo`, `TypeDefinitionLocation`, `TypeHierarchyItem`, `TypeHierarchyItemKind`, `WorkspaceAnalysisResult`, `WorkspaceSession` |
| `pyinc_codegen` | `Diagnostic`, `DiagnosticSeverity`, `FieldModel`, `SchemaAnalysis`, `SchemaGenerationError`, `SchemaModel`, `generate`, `generate_outputs`, `schema_analysis` |

## Integration module exports

| Public module | Stable `__all__` names |
|---|---|
| `pyinc.integrations.python_source` | `DependencySurface`, `DefinitionRef`, `Diagnostic`, `ImportRef`, `PythonFileAnalysis`, `PythonModuleAnalysis`, `PythonWorkspaceAnalysis`, `ResolvedImportRef`, `directory_analysis`, `file_analysis`, `module_analysis`, `module_binding_analysis_payload`, `module_wildcard_export_surface`, `resolved_imports_for_file`, `source_text`, `workspace_analysis`, `workspace_python_files` |
| `pyinc.integrations.toml_config` | `ConfigAnalysis`, `ConfigKey`, `ConfigSection`, `config_analysis`, `workspace_config_analysis` |
| `pyinc.integrations.requirements_txt` | `FileReference`, `IndexDirective`, `RequirementPayload`, `RequirementRef`, `RequirementsAnalysis`, `deep_requirements_analysis`, `requirements_analysis`, `requirements_payload`, `workspace_requirements_analysis` |
| `pyinc.integrations.installed_packages` | `ImportNameResolution`, `InstalledPackageRef`, `InstalledPackagesAnalysis`, `environment_index`, `installed_distributions_index`, `installed_packages_analysis`, `resolve_import_name` |
| `pyinc.integrations.json_config` | `JsonAnalysis`, `JsonKey`, `JsonSection`, `json_analysis`, `workspace_json_analysis` |
| `pyinc.integrations.dependency_check` | `DependencyCheckAnalysis`, `DependencyStatus`, `UndeclaredImport`, `dependency_check_analysis`, `workspace_dependency_check` |
| `pyinc.integrations.env_file` | `EnvEntry`, `EnvFileAnalysis`, `env_analysis`, `workspace_env_analysis` |
| `pyinc.integrations.xml_config` | `XmlAnalysis`, `XmlAttribute`, `XmlElement`, `workspace_xml_analysis`, `xml_analysis` |
| `pyinc.integrations.csv_data` | `CsvAnalysis`, `CsvColumn`, `csv_analysis`, `workspace_csv_analysis` |
| `pyinc.integrations.deep_module_resolution` | `DeepModuleResolutionAnalysis`, `ModulePathEntry`, `NamespacePackage`, `PthDirective`, `ResolvedModuleLocation`, `deep_module_resolution_analysis`, `resolve_module_location`, `resolve_module_path` |
| `pyinc.integrations.requirement_evaluation` | `ApplicableRequirement`, `ApplicableRequirementsAnalysis`, `MarkerEvaluation`, `PythonEnvironmentSnapshot`, `VersionSpecifierEvaluation`, `applicable_requirements`, `evaluate_markers`, `evaluate_version_specifier`, `python_environment_snapshot`, `workspace_applicable_requirements` |
| `pyinc.integrations.scope_resolution` | `Binding`, `BindingKind`, `Scope`, `ScopeKind`, `ScopeTree`, `SymbolId`, `SymbolOccurrence`, `scope_tree`, `symbol_at` |
| `pyinc.integrations.symbol_resolution` | `ClassMember`, `ClassModel`, `ModuleSymbolTable`, `Parameter`, `Reference`, `ReferenceQueryResult`, `Signature`, `Symbol`, `WorkspaceSymbolEntry`, `WorkspaceSymbolIndex`, `class_model`, `find_references`, `module_symbol_table`, `workspace_symbol_index` |
| `pyinc.integrations.notebook` | `NotebookAnalysis`, `NotebookCell`, `NotebookDefinition`, `NotebookDiagnostic`, `NotebookImport`, `notebook_analysis`, `workspace_notebook_analysis` |

The offline documentation checker compares every row above with the literal
module `__all__`, in addition to its existing kernel and aggregate-integration
checks. Adding or removing a stable name therefore requires a matching contract
change in the same revision.
