# Controlled BACKUP replacement

Collections with `replace_verified` use controlled replacement within the regular BACKUP workflow. The example assigns it to documents and the three selection stages. The print backlog uses the append policy.

```bash
media-archive backup --dry-run --profile data --collection documents --targets a
media-archive backup --yes --full-read --profile data --collection documents --targets a
```

BACKUP repeats whole-scope capacity planning under the archive and collection locks. At another historical path, a copy requires explicit approval with `--copy-if-elsewhere`.

## Publication and evidence

- A matching required path is verified with SHA-256 and reused. If the catalog last marked this version absent, record fresh evidence and update which contents are present at that path. A missing required path uses the ordinary no-overwrite publication.
- For a changed file the operation stores the previous destination's SHA-256 and size. The new file is copied into a temporary part on the same filesystem, read in full and synchronized. Immediately before the atomic rename, transfer reads the old destination again and checks the file and directory identities. Changing the destination stops the operation. Leave archive targets alone during BACKUP.
- The previous file stays at its required path until atomic publication, which replaces that version. After synchronization, SQLite links the new evidence to the local occurrence and treats earlier different content recorded at that path on the same volume as absent. Last-known evidence on other volumes stays in history.

## Recovery and limits

Repeat `backup --yes --full-read` with the same configuration, scope and targets. If the previous file is still present and matches the recorded intent, transfer can restart. If the new file is already present, BACKUP rereads it and commits fresh evidence without copying again. If the destination differs from both, the operation stops for inspection. An interrupted part can remain on disk and contributes to measured space usage.

When the source has changed again, recovery first checks unfinished replacements at the same required path on the same archive volume. If the destination contains the earlier operation's published bytes, BACKUP records that result before starting the next replacement. If the earlier publication cannot be confirmed, BACKUP pauses at that path and reports the pending operation ID.

Synthetic tests cover the policy boundary, both archive targets, interruptions on either side of publication, changed sources and destinations, and refreshing the catalog after a manual restore. [BACKUP tests](../tests/test_backup_planning.py)
