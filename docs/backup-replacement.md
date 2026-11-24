# Controlled BACKUP replacement trial

Use an isolated configuration with synthetic sources, a separate database and dedicated archive directories. The trial requires the current schema revision, explicit configuration, profile and selected targets.

```bash
media-archive --config fixture.env backup --dry-run --profile data --collection documents --targets a
media-archive --config fixture.env backup --replace-test --profile data --collection documents --targets a
```

Only collections whose effective change policy is `replace_verified` are written by this trial. The example assigns it to documents and the three selection stages. The print backlog keeps its append policy and can be tested separately with [the append command](backup-append.md). The trial repeats whole-scope capacity planning under the BACKUP locks. At another historical path, a copy still requires explicit approval with `--copy-if-elsewhere`.

## Publication and evidence

- A matching required path is verified with SHA-256 and reused. If the catalog last marked this version absent, record fresh evidence and update which contents are present at that path. A missing required path uses the ordinary no-overwrite publication.
- For a changed file the operation stores the previous destination's SHA-256 and size. The new file is copied into a temporary part on the same filesystem, read in full and synchronized. Immediately before the atomic rename, transfer reads the old destination again and checks the file and directory identities. Changing the destination causes the trial to stop. Archive targets should be left alone while the trial holds its application locks.
- The previous file stays at its required path until atomic publication. The trial keeps no separate version of it. After synchronization, SQLite links the new evidence to the local occurrence and treats earlier different content recorded at that path on the same volume as absent. Last-known evidence on other volumes stays in history.

## Recovery and limits

Repeat `backup --replace-test` with the same configuration, scope and targets. If the previous file is still present and matches the recorded intent, transfer can restart. If the new file is already present, the trial rereads it and commits fresh evidence without copying again. If the destination differs from both, the operation stops for inspection. An interrupted part can remain on disk and contributes to measured space usage.

When the source has changed again, recovery first checks unfinished replacements at the same required path on the same archive volume. If the destination contains the earlier operation's published bytes, the trial records that result before starting the next replacement. If the earlier publication cannot be confirmed, the trial pauses at that path and reports the pending operation ID.

Each selected target runs one transfer at a time. The [cleanup trial](backup-cleanup.md) handles confirmed deletion and the [coordination trial](backup-coordination.md) checks fresh copy evidence. The production BACKUP workflow is enabled after snapshot and recovery integration. [Synthetic tests](../tests/test_backup_planning.py) cover the policy boundary, both archive targets, interruptions on either side of publication, changed sources and destinations, and refreshing the catalog after a manual restore.
