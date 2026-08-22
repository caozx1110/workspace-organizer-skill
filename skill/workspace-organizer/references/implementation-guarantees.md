# Advanced tooling guarantees

This reference is for maintainers, security review, and integrations. Ordinary
users only need the command sequence in [tooling.md](tooling.md).

## Mutation durability

Before the first workspace mutation, apply persists immutable intent containing
the exact plan digest, source/destination evidence, configuration transition,
and rollback inputs. Write-ahead stage records precede parent creation,
temporary copies, atomic no-replace installation, configuration replacement,
and source removal. A source is removed only after reopening and verifying the
installed destination through the workspace root.

Removing an adopted-task registration uses atomic exchange. The displaced
configuration inode remains available as a verified backup until transition
checks pass; concurrent identity or content changes are retained as evidence
instead of silently overwritten.

## Recovery and retry

Durable records normally live under `.workspace-organizer/verification/`.
Initialization records remain beside the external plan so interruptions are
discoverable before the control directory exists. Repeated apply is read-only
and succeeds only when a verified result still matches. An incomplete intent,
failed result, interruption, or final-result write failure blocks blind retry;
preserve the intent, WAL, hashes, configuration evidence, and rollback inputs
for bounded manual recovery.

## Projection atomicity

`index` stages all six catalog/Markdown outputs as one transaction. It rechecks
target identities and bytes at commit boundaries, uses atomic exchange or
no-replace installation, and rolls back in reverse order when a concurrent
change is detected. Existing known-good outputs remain untouched on validation
or generation failure.
