# Append-only BACKUP trial

Run `media-archive migrate` to upgrade the test database to revision `0011`. Use an isolated configuration, database, source files and archive scope. The normal BACKUP workflow becomes available after the complete backup stage, including snapshots and restore.

```bash
media-archive --config fixture.env backup --dry-run --profile data --targets a
media-archive --config fixture.env backup --append-test --profile data --targets a
```

The trial requires an explicit configuration, profile and target selection. It repeats the whole-batch capacity check under the archive and collection locks before creating any assignments or copying media. It executes policies with `keep_both`. The capacity preview continues to include the full selected scope. Collections using verified replacement are outside this trial. The command writes text and JSON reports.

## Required paths and existing copies

The first copy pins the pair by device ID and the selected disk's required path in SQLite. A second disk can be offline during the first copy. When it becomes available, its path is pinned independently. For example, a different existing `image.jpg` on disk A makes the new file use a SHA-256 suffix there, while disk B can keep `image.jpg`. Earlier shared-path assignments migrate to explicit identical paths for both members.

A matching copy at the disk's required path is reused after SHA-256 verification. A collision discovered before a path is pinned uses a hash suffix and preserves the existing bytes. The suffix grows when its shorter form would take another path reserved by the batch. Once a disk's path is pinned, different content at that path blocks the copy. Distinct source paths are kept as separate occurrences even when their bytes match.

If saved history for the selected disk records the same content under a different path, the preview lists that historical location. The write trial stops before planning transfers until the operator approves creating a separate copy with `--copy-if-elsewhere`. The old path and its file are left in place.

## Publication and recovery

The shared transfer copies through a temporary part, verifies the full SHA-256 and publishes without overwriting. SQLite records the result only after publication. Run the same `backup --append-test` command to recover a failed or interrupted transfer. It recalculates available capacity and checks the latest operation for that copy before attempting another transfer, including when the copy was previously recreated. Orphan parts consume measured free space and are retained for inspection.

The trial runs transfers sequentially. Controlled replacement, cleanup approval, two-target coordination and snapshot publication are separate milestones. Synthetic tests cover per-disk conflict names, historical-path approval, verified reuse and recovery between publication and the SQLite result.
