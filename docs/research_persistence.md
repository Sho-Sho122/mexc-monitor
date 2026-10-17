# Research / 511 persistence

This changes persistence only. Research Scanner, the four strategies, Exit
Research, beta1 trading rules and historical Forward results are not modified.
No recovery archive is automatically imported into formal Forward data.

## Before computation

The workflow uses its checked-out code and, after dependency installation, calls
`research_persistence.py prepare` before recording the research run start.
The helper fetches main and checks that research inputs in the workspace have no
uncommitted modifications. It also refuses a change to Python, workflow or
requirements files since checkout: start a fresh workflow from current main in
that case, rather than mixing old code with new data.

Only the existing Research persistence paths and `511_data` are refreshed from
that fetched commit. Code, HEAD and the ordinary Git index are not reset. The
external `baseline.json` records the checkout SHA, fetched SHA and per-group
input blob identities. The prepared marker is published only after all inputs
are written. Missing remote input files require review; they are not interpreted
as permission to delete local evidence.

The fetched snapshot is not a lock on future remote changes. The save-time
comparison below remains mandatory. Existing `mexc-research` concurrency is
retained, with cancellation disabled.

## Independent output preservation

After computation, Research and 511 are archived separately. Each archive has a
versioned manifest, baseline identities, output SHA-256/blob identities and an
archive hash. Archives contain regular, allowlisted files only. Missing output
files, an unknown manifest version or corrupted content prevent saving.
The first completed archive cannot be replaced by a repeated archive command.

Recovery artifacts are uploaded before either save. Final audit artifacts are
attempted with `always()`, including raw outputs as a fallback if archival failed.
Names include both run ID and attempt, avoiding rerun name collisions.
Audit records distinguish archive failure, data conflict, transport retry,
successful save and already-saved output. Raw Git error text is not persisted
because it may contain credentials; the operation, status and error category
are recorded instead.

Research save does not depend on 511 computation, archival or artifact upload
success. A failed Research save prevents publishing dependent 511 output. A
failed 511 computation is preserved for inspection but is not published. Failed
archive/upload/save steps are reported at the end even when `continue-on-error`
was necessary to let independent persistence finish.

## Optimistic save and retry

1. Validate the immutable recovery archive and manifest.
2. Fetch current main. If its entire group equals the archived output, record
   `ALREADY_SAVED`. This handles duplicate execution and a successful push whose
   response was lost.
3. Otherwise require that the entire remote group still equals its input
   baseline. Added, removed or changed group files cause `511_REMOTE_CONFLICT`
   or `RESEARCH_REMOTE_CONFLICT`. Code changes also prevent publication.
4. Build a candidate commit from current main using a private temporary index.
   Replace only the selected group's verified archive content; retain all other
   paths. Neither the normal index nor computed working files are overwritten.
5. Perform a normal fast-forward push. On transport failure or a competing push,
   fetch again and recheck the full group before another candidate is built.
   There are at most five save attempts, followed by a reconciliation-only fetch.

There is no force push, state merge, `reset --hard`, rebase, historical Forward
reclassification, quote imputation or automatic replay at a fabricated cutoff.
Unrelated Monitor/HYBRID/LAB updates can be retained through retries. A genuine
same-group conflict is preserved and stopped, not optimized or merged away.

## Recovery and operational limits

Use a fresh external recovery directory per workflow run/attempt. Do not reuse
one with a prepared baseline, edit manifests, or fall back to the legacy
`model511.workflow_save` CLI; that historical module remains unchanged for
compatibility but is no longer called by the revised workflow.

On failure, retain Recovery, Save Audit, run ID/attempt, checkout/input SHA,
manifest and raw outputs. Verify hashes before any investigation. Restoration
into existing Forward state requires a separate review and authorization.
An interrupted preparation must be restarted in a fresh checkout and directory;
its incomplete marker cannot be used to certify an archive.

Disk failure, runner destruction or failure of both artifact uploads cannot be
made infallible by workflow code. Monitor upload failures and retain any
available raw outputs. No guarantee of remote recovery is claimed in those
cases. There is no deployment or GitHub shakedown in this local change.

After approval to deploy, drain obsolete runs and ensure dispatches use the
updated main workflow. Already-running or old-ref workflows do not retroactively
gain this protection. A writer outside this protocol can still cause a detected
conflict; changing that writer or restoring historical data is separate work.

## Local tests

Install the test-only dependency from `requirements-persistence-tests.txt`.
Production persistence uses only the Python standard library.

From the repository root with the root on `PYTHONPATH`, run:

```sh
python -W error -m unittest discover -s tests/model511 -v
python -W error -m unittest discover -s tests/persistence -v
```

The new integration tests create disposable local bare repositories and clones.
Their commits and pushes are fixture operations, never GitHub operations. They
exercise stale snapshots, simultaneous writers, same/different output,
mid-save races, DNS/HTTP failures, lost acknowledgements, restart/idempotency,
archive corruption and traversal, missing files, code drift, CLI exit status,
Research/511 isolation, and workflow artifact wiring. Existing beta1 tests are
kept unchanged. The `511 beta1 tests` CI runs both suites with warnings treated as errors.
Its push path filters cover the helper, persistence tests and requirements.


## GitHub Actions shakedown after separate deployment approval

1. Review and publish only this persistence change; do not include Adaptive or
   data files. Confirm the remote commit SHA and the automatic `511 beta1 tests`
   run for that exact SHA: 87 beta1 tests and 30 persistence tests must pass.
   This CI has read-only contents permission and uses disposable local Git
   remotes; it never runs the production Research workflow.
2. Before a production check, obtain separate approval: `MEXC Research` writes
   new formal Forward data. Let old-ref runs finish and confirm dispatch uses
   the new main workflow. Do not rerun an obsolete workflow attempt.
3. With that approval, dispatch one normal Research run. Check prepare SUCCESS,
   checkout/input SHA, unchanged strategy command order, separate Research and
   511 save status, and all four Recovery/Save Audit artifacts. Download and
   verify manifest SHA-256 values and compare saved group blobs with manifest.
4. Check that saved commits retain unrelated paths, have fast-forward parents,
   and include only their respective owned data group. Then observe a second
   normal run using the previous saved state. Do not manufacture production
   conflicts or replay old Recovery into Forward. Race/failure injection belongs
   only in the disposable local tests.
5. If a conflict or upload failure occurs, preserve logs, run ID/attempt, baseline,
   both group archives and audits. Stop further manual dispatches for review.
   Do not force push, overwrite state, restore artifacts or fall back to the old
   save routine. Missing artifacts or hash mismatches fail the shakedown.

Local tests do not certify GitHub permissions, artifact service availability or
Ubuntu runtime behavior; those remain checks for the approved shakedown.
