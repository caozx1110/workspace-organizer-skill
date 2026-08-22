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
  route. It accepts inbox or staging originals, opens the source through a
  no-follow descriptor, copies a bounded snapshot to scratch space under
  `/tmp`, rejects escaping paths and links, and never extracts or changes the
  original. `--max-source-bytes`, `--max-total-bytes`, and `--max-entries`
  bound all supported formats. ZIP inspection additionally validates the
  classic end-of-central-directory declaration and applies
  `--max-metadata-bytes` before Python loads the central-directory entries;
  split and ZIP64 inputs fail closed.

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

The dry-run mutation list is complete: initialization includes the exact
configuration target and digest, adopted archive includes the registration
before/after transition, and organize reports every independently detectable
source or destination error rather than stopping after the first collision.

Apply rejects a missing, stale, altered, or mismatched plan/approval. For the
implementation-level durability, concurrency, and recovery guarantees, see
[advanced tooling guarantees](implementation-guarantees.md).

## Generate projections

`index ROOT` validates canonical inputs, filters sensitivity, and regenerates the
six catalog/Markdown outputs as one controlled transaction. It leaves the last
known-good set intact on validation or generation failure and performs no writes
when the inputs are unchanged. See [advanced tooling guarantees](implementation-guarantees.md)
for the low-level atomicity and recovery contract.

The module also exposes the command implementations as Python functions for
tests or a trusted host integration. Do not bypass plan approval by calling
private helpers directly.
