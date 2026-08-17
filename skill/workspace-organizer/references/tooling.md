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

Apply rejects a missing, stale, altered, or mismatched plan/approval. Before the
first workspace mutation it persists an immutable intent containing the exact
plan digest, source/destination evidence, configuration transition (when
applicable), and rollback inputs. Immutable write-ahead stage records then
precede parent creation, controlled temporary copies, atomic no-replace
installation, configuration replacement, and source removal. Files and archive
trees become visible at their final destination only after a verified temporary
copy is complete. Source removal occurs only after reopening and verifying the
installed destination through the workspace root.

Removing an adopted-task registration uses an atomic exchange: the displaced
configuration inode remains available as a verified backup until both the old
and new bytes pass their transition-boundary checks. An identity or content
change exchanges the prior inode back and retains durable evidence instead of
silently overwriting a concurrent configuration update.

Durable records normally live under `.workspace-organizer/verification/` (or
the existing control directory when upgrading an older workspace without that
role). Initialization records stay beside the external initialization plan so
an interruption remains discoverable even before the control directory exists.
Repeated apply is read-only and succeeds only while a verified result still
matches. An intent without a verified result, a failed result, an interrupt, or
a final-result write failure blocks blind retry. Preserve the intent, WAL,
source/destination hashes, configuration before/after evidence, and rollback
inputs for bounded manual recovery before preparing a new plan.

## Generate projections

`index ROOT` validates all canonical inputs, filters sensitivity, builds all six
catalog/Markdown outputs, and stages them in a controlled transaction directory.
Target parents remain bound by no-follow directory descriptors. At every commit
boundary all six target identities and prior bytes are rechecked; existing
targets use atomic exchange, while absent targets use atomic no-replace install.
The displaced files serve as rollback backups. Any `BaseException`, including
`KeyboardInterrupt`, rolls installed targets back in reverse order when they
still match the transaction; concurrent user changes are never overwritten.
Incomplete rollback evidence is retained under the cache for manual recovery.
An unmarked overview, symlink, invalid record, collision, or write failure keeps
the previous known-good set. Repeating an unchanged render performs no writes.

The module also exposes the command implementations as Python functions for
tests or a trusted host integration. Do not bypass plan approval by calling
private helpers directly.
