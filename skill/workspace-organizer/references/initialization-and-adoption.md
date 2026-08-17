# Initialization and adoption

Use these gates when establishing `.workspace-organizer/config.json` in a new or
existing directory. Keep adoption in place; do not treat it as a migration.

## Managed root roles

| Path | Role |
| --- | --- |
| `00_总览/` | Generator-owned `TODO.md`, `TIMELINE.md`, and `MATERIALS.md` only |
| `10_收件箱/` | Unclassified incoming content; treat as `restricted` |
| `20_任务/` | Canonical active task bundles at `20_任务/<task-id>/` |
| `30_资料库/` | Reusable materials not owned by one task |
| `90_归档/` | Terminal bundles at the verified archive destination |
| `99_待整理/` | Content awaiting a decision after an approved plan |
| `.workspace-organizer/` | Configuration, operation evidence, catalogs, and cache |

Do not use the hidden control plane as an access-control boundary. Keep plans and
verification evidence local and at least `internal`.

## Initialize a new workspace

1. Perform a read-only scan of the proposed root.
2. Reject a file or user-owned directory that collides with a managed entry.
3. Confirm a stable 3-64 character lowercase ASCII workspace ID.
4. Confirm `default_sensitivity`; use `internal` when the user has not selected
   another explicit value.
5. Present the exact directories and configuration to create.
6. Obtain approval, then invoke the deterministic initialization tooling.
7. Verify the configuration and every created path before reporting success.

Use `assets/workspace-config.json` as the configuration shape. Store only these
keys in schema version 1:

- `schema_version`: integer `1`.
- `workspace_id`: stable lowercase slug.
- `default_sensitivity`: `public`, `internal`, `confidential`, or `restricted`.
- `adopted_task_paths`: exact registered legacy bundle roots.
- `adopted_material_roots`: objects with exact `path` and `sensitivity`.
- `exclude_paths`: exact roots no operation may enter.

## Adopt an existing workspace

Apply these gates in order:

1. Inventory entries without following symlinks or entering exclusions or nested
   Git repositories. Report managed-name, Unicode NFC, and case-fold collisions.
2. Confirm the workspace root and each existing managed entry's role. After
   approval, create only missing collision-free control entries.
3. Ask a person to select task bundles and material roots. Do not infer ownership.
4. Add a valid `TASK.md` to each accepted adopted task and register its exact root.
5. Register material roots with explicit sensitivity. Leave unselected candidates
   unmanaged and leave uncertain content in place.
6. Validate configuration, task IDs, path ownership, exclusions, and sensitivity
   before requesting generation.
7. Propose every rename, move, quarantine, deduplication, or normalization as a
   separate deterministic operation.

## Validate path relationships

- Store workspace-relative POSIX paths only; reject absolute paths, `.` or `..`
  segments, empty segments, trailing slashes, backslashes, NUL, and path escape.
- Compare paths after Unicode NFC normalization and case-folding. Reject a
  collision rather than choosing one spelling.
- Keep adopted task roots pairwise disjoint and separate from material roots.
- Permit nested material roots only to apply the most restrictive sensitivity.
- Keep exclusion roots pairwise disjoint. Permit an exclusion below a registered
  root as a carve-out, but never register a root at or below an exclusion.
- Exclude `.git/`, nested repositories, `.workspace-organizer/cache/`, and every
  symlink component or target even when configuration omits them.

Never overwrite, reinterpret, rename, move, delete, publish, or hash-upload
existing content as a side effect of initialization or adoption.
