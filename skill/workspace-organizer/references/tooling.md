# Deterministic tooling

Invoke the standard-library CLI at `scripts/workspace_organizer.py`. Every
command takes an explicit workspace root. Read-only results and verification
summaries are UTF-8 JSON on standard output; contract or safety failures return
exit status 2 without guessing a recovery action.

## Inspect without mutation

- `inventory ROOT` records deterministic filesystem metadata while skipping
  exclusions, symlinks, and nested Git repositories. Compressed originals are
  metadata-only and large files may carry a deferred-hash marker.
- `scan ROOT` adds confirmation-required classification proposals and duplicate
  candidates. It never chooses ownership, a task, or a destination.
- `inspect-compressed ROOT PATH --yes` is the only archive-member inspection
  route. It accepts inbox or staging originals, uses scratch space under `/tmp`,
  rejects escaping paths and links, enforces entry/size limits, and never
  extracts or changes the original.

## Plan, approve, apply, and verify

Use one of `plan-init`, `plan-organize`, or `plan-archive` with `--output` to
write a new immutable plan. Existing output files are never replaced. For
organizing, pass a JSON array whose objects contain only explicit `source` and
`destination` paths; ownership and role are supplied by the user rather than
inferred. Store an initialization plan outside the not-yet-initialized workspace
so creating the plan itself does not invalidate the recorded root snapshot.

Run `dry-run ROOT --plan PLAN` to recheck every source snapshot, exclusion,
collision, destination, and configuration binding without mutation. Then bind
an approval to the exact plan bytes with:

```sh
python3 scripts/workspace_organizer.py approve --plan PLAN --output APPROVAL --yes
python3 scripts/workspace_organizer.py apply ROOT --plan PLAN --approval APPROVAL
python3 scripts/workspace_organizer.py verify ROOT --plan PLAN
```

Apply rejects a missing, stale, altered, or mismatched plan/approval. Successful
moves and archives record immutable evidence under
`.workspace-organizer/verification/`; repeated apply is read-only and succeeds
only while the verified destination still matches. A failed or partial record
blocks blind retry. Preserve it and inspect the recorded source, destination,
hashes, and rollback evidence before preparing a new plan.

## Generate projections

`index ROOT` validates all canonical inputs, filters sensitivity, builds all six
catalog/Markdown outputs, and commits them as one recoverable replacement set.
An unmarked overview, symlink, invalid record, collision, or write failure keeps
the previous known-good set. Repeating an unchanged render performs no writes.

The module also exposes the command implementations as Python functions for
tests or a trusted host integration. Do not bypass plan approval by calling
private helpers directly.
