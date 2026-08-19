# Scenario validation matrix

This matrix is the auditable validation record for Issue #5. All scenario data
is public and synthetic. Tests construct disposable workspaces under the system
temporary directory and never operate on a user workspace.

The machine-readable traceability source is
[`tests/fixtures/scenarios/requirement-matrix.json`](../tests/fixtures/scenarios/requirement-matrix.json),
and the deterministic scenario definitions are in
[`tests/fixtures/scenarios/scenarios.json`](../tests/fixtures/scenarios/scenarios.json).

## Representative families

| Family | `type` | Workspace state | Sensitivity | Shared contract evidence |
| --- | --- | --- | --- | --- |
| Research | `research` | `active` | `public` | Same v1 `TASK.md` fields and lifecycle graph |
| Paper review | `review` | `waiting` | `confidential` | Same v1 `TASK.md` fields and lifecycle graph |
| Reimbursement | `reimbursement` | `completed` | `internal` | Same v1 `TASK.md` fields; eligible archive path |
| Presentation | `presentation` | `planned` | `public` | Same v1 `TASK.md` fields and lifecycle graph |
| Competition | `competition` | `blocked` | `internal` | Same v1 `TASK.md` fields and lifecycle graph |
| External collaboration | `collaboration` | `active` | `internal` | Same v1 `TASK.md` fields and lifecycle graph |
| Contract | `contract` | `cancelled` | `restricted` | Same v1 `TASK.md` fields and lifecycle graph |

`ScenarioValidationTests.test_seven_families_share_one_schema_and_lifecycle`
validates the same `planned -> active -> completed -> archived` transition path
for every family without adding type-specific fields or transition rules.

## Filesystem and safety cases

| Case | Expected behavior | Repeatable test |
| --- | --- | --- |
| Unicode and spaces | Preserve spelling; explicit move only; percent-encoded local links | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Large placeholder | Inventory metadata is retained and scan hashing is deferred at the configured threshold | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Compressed original | Metadata-only scan; member listing requires explicit confirmation; no extraction or source mutation | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Exact duplicates | Hash-equal files are only confirmation-required duplicate candidates | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Suspected duplicates | Similar names with different hashes remain separate confirmation-required proposals | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Temporary file | Remains restricted in staging and is not indexed or moved automatically | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Nested Git | Recorded as a boundary; descendants are not scanned, indexed, moved, or exposed | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Sensitive task/material | Confidential and restricted names, paths, counts, and hashes are filtered before default views | `test_empty_initialization_full_pipeline_and_difficult_files` |
| Stale source or destination collision | Dry-run blocks; apply does not mutate the workspace | `test_stale_and_collision_cases_leave_manifest_unchanged` |
| Index write failure | All six generated targets return to their prior byte-identical state | `test_index_failure_rolls_back_all_six_outputs` |

## End-to-end flows

The empty-root scenario exercises inventory and deterministic initialization
planning, dry-run, exact-byte approval, apply, durable verification, conservative
scan proposals, an explicitly approved organization move, generated indexes,
and a verified terminal archive. Before/after file manifests prove that only the
approved source/destination pair and local operation evidence change.

The adoption scenario begins with a Unicode, space-containing legacy task path,
a confirmed material root, an existing managed inbox, nested Git content, and
an unrelated file. Initialization registers exact roots without moving or
rewriting existing files. A later approved archive moves the adopted task once,
updates only its exact registration, and preserves rollback inputs in immutable
verification evidence.

The requirement JSON preserves the exact reviewed Issue/Epic criterion text and
source URLs. Its validator enforces all eight Issue #5 acceptance IDs, all six
Epic success IDs, all seven Epic definition-of-done IDs, their reviewed coverage
mapping, real unittest targets, and an exact delivery-gate command registry.
Dashboard coverage here is limited to the stable sensitivity-filtered
generated-view interface; HTML implementation stays owned by Issue #7.
