# BACKUP snapshot and recovery trial

Use a test configuration, a separate state database and dedicated archive directories. Initialize a new test database with `media-archive --config fixture.env setup --yes`. To upgrade an existing test database, run `media-archive --config fixture.env migrate`.

```sh
media-archive --config fixture.env backup --snapshot-test --profile data --targets a b
```

The trial plans media transfers for the selected targets. It also budgets the snapshot on every connected, registered writable archive admitted at preflight, including archives outside the media selection. An unavailable snapshot budget on an admitted archive blocks the batch before media transfer. Archives configured for read-only access can still supply fresh evidence for an existing copy when selected.

After transfer results and any confirmed cleanup are committed, BACKUP creates one SQLite online backup with the configuration captured at the start. It publishes the same three files under `APP_ARCHIVE_STATE_DIR/snapshot-ID` at the root of each admitted volume. The default state directory is `.media-archive`, outside the data and audit roots. An interruption stops the trial. A completed partial media run still tries to save its recorded results.

Each target stages, validates and synchronizes its generation before atomic publication. The trial rechecks available space with the actual generation size and keeps the required reserve. Rotation retains the new generation and up to four older valid generations. Invalid generations and unrelated files are preserved. Failure on one archive leaves the other archive's results available. The report records publication or failure per archive and returns exit code `3` for an incomplete run. The final check for manual space release runs after snapshot publication and verifies that the previously read copies and their local source are still current.

## Restore and reconciliation

Use the existing [verify-snapshot and restore commands](snapshots.md), then run `reconcile` with the chosen configuration. Reconciliation verifies known BACKUP paths on the matching current archive volumes, including paths verified by a read alone on a read-only target. It records missing paths and observes newer contents at known paths, without assigning these contents to a new archive pair. A later BACKUP can register matching copies at its required paths. Files at paths absent from the snapshot are considered by the next BACKUP plan.

Reconciliation commits a pending BACKUP attempt when a fresh read matches its expected publication. A missing initial copy or the unchanged preimage of a replacement can be retried by BACKUP. If a pending replacement's destination contains different bytes that match a fresh full SHA-256 read of its current local source on the registered volume, revision `0013` closes the old intent as superseded and links it to the archive verification. The attempt history is kept. An unconfirmed pending target or a disconnected volume needed by an unfinished attempt or cleanup keeps the restore requirement active. Offline volumes with historical copies are listed as unchecked. Reconciliation settles prior cleanup records from observed presence and leaves further deletion to a new approval.

[Synthetic tests](../tests/test_backup_planning.py) cover distribution of identical generations, recipient capacity, retention, partial snapshot failure, later archive contents and pending publication recovery.

## Hardware verification

The Stage 6 trial used two separate physical NTFS disks, one synthetic file, a separate SQLite database and dedicated directories for files and snapshots.

- Each disk was tested on its own. BACKUP reported `1/2` fresh copies and the archived file matched the source SHA-256. Each disk supplied a verified snapshot for restoring the test database and clearing the reconciliation requirement. On one disk, restoring an older generation and running `reconcile` recognized a newer archived version and preserved its bytes.
- The joint run reported `2/2` fresh copies and verified the already matching archive files in place. Both disks received the same generation. `verify-snapshot` succeeded on each disk, and `state.sqlite3`, `settings.env` and `manifest.json` were byte-identical across the two copies.

These results cover the isolated BACKUP workflow. The first production batch and full workflow verification are tracked in [Stage 7](roadmap.md#stage-7--integration-and-controlled-rollout).
