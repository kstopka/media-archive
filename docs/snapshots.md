# State snapshots and restore

Create a snapshot in an existing trusted directory. Keep generations private because they contain the SQLite history and a byte-for-byte copy of the selected configuration, including private paths and device identifiers.

```sh
media-archive --config /example/settings.env snapshot /example/generations
media-archive verify-snapshot /example/generations/snapshot-GENERATION_ID
media-archive --config /example/settings.env restore /example/generations/snapshot-GENERATION_ID --yes
```

The output directory must be separate from the state directory, reports, configuration and media paths. The first command uses the SQLite online backup API, checks the captured configuration against the copied registry and publishes the validated generation in a new directory. The generation contains `state.sqlite3`, `settings.env` and `manifest.json`. The manifest records the format version, generation ID, creation time in UTC, schema revision, original configuration path and SHA-256 hashes for both copied files. [SQLite Online Backup API](https://www.sqlite.org/backup.html)

Verification checks the file types, manifest, checksums, application identity, revision, SQLite integrity, foreign keys and captured configuration. It can run with the generation alone. Keep each generation under operator control. The checksums detect corruption and accidental changes.

If the generation is visible but the output directory could not be synchronized, `snapshot` returns exit code 3 and identifies the generation. Check it with `verify-snapshot` before retrying.

Restore requires an explicitly selected configuration and an existing `APP_STATE_DIR`. If the state directory was lost, prepare it before running `restore`. Restore validates a staged copy of the generation and upgrades a supported earlier schema on that copy when needed. The selected configuration must match the restored registry. Restore keeps the configuration file in place. If the working database exists, restore writes and synchronizes a temporary copy before publishing it as a private `state-before-restore-*.sqlite3` file. It then replaces `state.sqlite3` atomically. Pending operations and earlier history from the generation remain in the restored database. SQLite sidecars at the working path require inspection before restore. Active application processes prevent restore through the installation lock. Stop external SQLite clients before restoring.

Restore records a persistent reconciliation requirement before publishing the database. `pending` reports it, and `resume ID --dry-run` shows reconciliation as the next step for [IMPORT](import-recovery.md#after-restoring-sqlite), [SORT](sort.md#recovery) and [BACKUP](backup-snapshots.md#restore-and-reconciliation). The journal rejects planning or starting media work while reconciliation is required. The restore command changes application state only. A failure to synchronize the directory after publishing the database returns exit code 3.

The Python API lives in `media_archive.state.snapshots`. `create_snapshot(config, directory)` returns the published generation path, `validate_snapshot(directory)` returns manifest metadata, and `restore_snapshot(config, directory)` returns the generation ID and any saved previous database path. Invalid state or filesystem failures raise `StateError`. A snapshot published before a directory sync failure raises `SnapshotPublishedError`, a subclass of `StateError`. A configuration mismatch raises `ConfigError`. `load_reconciliation(connection)` reads the recovery requirement.

[Snapshot tests](../tests/test_state_snapshots.py) exercise active database copies, corrupt and incompatible generations, lock contention, preservation of history, interrupted copying and failed synchronization. BACKUP distributes the same generation to admitted archive disks, rotates older generations and reconciles known archive paths after restore. [BACKUP snapshots](backup-snapshots.md)
