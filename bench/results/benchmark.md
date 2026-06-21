# pyinc benchmark

Every row's `correct` column is the engine's output compared against a
fresh, cache-free recomputation for that scenario. pyinc is correct in
every scenario; a naive cache may be fast but stale.

| target | scenario | engine | seconds | peak KiB | graph | nodes | correct |
|---|---|---|---|---|---|---|---|
| synthetic | cold | pyinc | 0.007317 | 34.3 | 14 | 14 | True |
| synthetic | cold | full | 0.000003 | 0.4 | 0 | 0 | True |
| synthetic | cold | naive | 0.000004 | 0.5 | 0 | 6 | True |
| synthetic | cold | joblib | 0.005698 | 57.4 | 0 | 0 | True |
| synthetic | unchanged | pyinc | 0.000489 | 4.6 | 14 | 14 | True |
| synthetic | unchanged | full | 0.000002 | 0.4 | 0 | 0 | True |
| synthetic | unchanged | naive | 0.000002 | 0.1 | 0 | 6 | True |
| synthetic | unchanged | joblib | 0.001799 | 9.5 | 0 | 0 | True |
| synthetic | localized_semantic_edit | pyinc | 0.005040 | 13.4 | 14 | 14 | True |
| synthetic | localized_semantic_edit | full | 0.000002 | 0.4 | 0 | 0 | True |
| synthetic | localized_semantic_edit | naive | 0.000003 | 0.1 | 0 | 6 | True |
| synthetic | localized_semantic_edit | joblib | 0.001833 | 9.7 | 0 | 0 | True |
| synthetic | high_fanout_shared_edit | pyinc | 0.004707 | 14.5 | 14 | 14 | True |
| synthetic | high_fanout_shared_edit | full | 0.000003 | 0.4 | 0 | 0 | True |
| synthetic | high_fanout_shared_edit | naive | 0.000003 | 0.1 | 0 | 6 | False |
| synthetic | high_fanout_shared_edit | joblib | 0.004121 | 10.4 | 0 | 0 | True |
| synthetic | checkpoint_restore | pyinc | 0.000933 | 20.0 | 14 | 14 | True |
| calc | cold | pyinc | 0.064890 | 83.8 | 19 | 19 | True |
| calc | cold | full | 0.057017 | 71.5 | 0 | 0 | True |
| calc | cold | naive | 0.057944 | 71.8 | 0 | 2 | True |
| calc | unchanged | pyinc | 0.004542 | 9.6 | 19 | 19 | True |
| calc | unchanged | full | 0.057913 | 71.5 | 0 | 0 | True |
| calc | unchanged | naive | 0.000021 | 0.8 | 0 | 2 | True |
| calc | unreferenced_file_edit | pyinc | 0.004438 | 9.5 | 19 | 19 | True |
| calc | unreferenced_file_edit | full | 0.056768 | 71.5 | 0 | 0 | True |
| calc | unreferenced_file_edit | naive | 0.000017 | 0.8 | 0 | 2 | True |
| calc | comment_only_referenced_edit | pyinc | 0.005852 | 12.8 | 19 | 19 | True |
| calc | comment_only_referenced_edit | full | 0.057180 | 71.5 | 0 | 0 | True |
| calc | comment_only_referenced_edit | naive | 0.056468 | 71.8 | 0 | 2 | True |
| calc | localized_semantic_edit | pyinc | 0.055205 | 57.0 | 19 | 19 | True |
| calc | localized_semantic_edit | full | 0.058214 | 71.5 | 0 | 0 | True |
| calc | localized_semantic_edit | naive | 0.056397 | 71.8 | 0 | 2 | True |
| calc | high_fanout_shared_edit | pyinc | 0.055476 | 55.3 | 19 | 19 | True |
| calc | high_fanout_shared_edit | full | 0.058800 | 71.5 | 0 | 0 | True |
| calc | high_fanout_shared_edit | naive | 0.056760 | 71.8 | 0 | 2 | True |
| calc | removed_emitted_artifact | pyinc | 0.042232 | 45.9 | 19 | 19 | True |
| calc | removed_emitted_artifact | full | 0.049013 | 71.0 | 0 | 0 | True |
| calc | removed_emitted_artifact | naive | 0.048240 | 71.3 | 0 | 2 | True |
| calc | tampered_generated_output | pyinc | 0.003286 | 9.5 | 19 | 19 | True |
| calc | tampered_generated_output | full | 0.049901 | 71.0 | 0 | 0 | True |
| calc | tampered_generated_output | naive | 0.000032 | 0.8 | 0 | 2 | False |
| calc | checkpoint_restore | pyinc | 0.006363 | 49.4 | 13 | 13 | True |
| codegen | cold | pyinc | 0.131052 | 68.4 | 20 | 20 | True |
| codegen | cold | full | 0.132725 | 62.6 | 0 | 0 | True |
| codegen | unchanged | pyinc | 0.025832 | 13.9 | 20 | 20 | True |
| codegen | unchanged | full | 0.129651 | 62.5 | 0 | 0 | True |
| codegen | comment_only_referenced_edit | pyinc | 0.025546 | 16.1 | 20 | 20 | True |
| codegen | comment_only_referenced_edit | full | 0.140115 | 62.5 | 0 | 0 | True |
| codegen | localized_semantic_edit | pyinc | 0.063177 | 36.0 | 20 | 20 | True |
| codegen | localized_semantic_edit | full | 0.129295 | 62.3 | 0 | 0 | True |
| codegen | high_fanout_shared_edit | pyinc | 0.085490 | 38.5 | 20 | 20 | True |
| codegen | high_fanout_shared_edit | full | 0.129704 | 62.4 | 0 | 0 | True |
| codegen | removed_emitted_artifact | pyinc | 0.073832 | 47.8 | 23 | 23 | True |
| codegen | removed_emitted_artifact | full | 0.096715 | 53.8 | 0 | 0 | True |
| codegen | tampered_generated_output | pyinc | 0.019549 | 13.3 | 23 | 23 | True |
| codegen | tampered_generated_output | full | 0.096312 | 53.8 | 0 | 0 | True |
| codegen | checkpoint_restore | pyinc | 0.023024 | 73.0 | 17 | 17 | True |
| action | cold | pyinc | 0.002483 | 12.5 | 5 | 5 | True |
| action | cold | full | 0.002080 | 12.1 | 0 | 0 | True |
| action | unchanged | pyinc | 0.001376 | 7.0 | 5 | 5 | True |
| action | unchanged | full | 0.002014 | 12.1 | 0 | 0 | True |
| action | high_fanout_shared_edit | pyinc | 0.002051 | 9.8 | 5 | 5 | True |
| action | high_fanout_shared_edit | full | 0.002704 | 12.1 | 0 | 0 | True |
| action | removed_emitted_artifact | pyinc | 0.001142 | 7.9 | 5 | 5 | True |
| action | removed_emitted_artifact | full | 0.001471 | 10.6 | 0 | 0 | True |
| action | tampered_generated_output | pyinc | 0.001132 | 6.8 | 5 | 5 | True |
| action | tampered_generated_output | full | 0.001576 | 10.6 | 0 | 0 | True |
