# BACKUP scope and capacity preview

Run `backup --dry-run` to see the registered backup profiles that match the connected archive volumes. A single matching profile is proposed and its scope is previewed. When several profiles match, choose one via `--profile`; no media is scanned until the profile is selected.

```bash
media-archive backup --dry-run
media-archive backup --dry-run --profile data
media-archive backup --dry-run --profile data --targets a --collection photos
media-archive backup --dry-run --profile data --targets a b --collection photos --month 2025-02
media-archive backup --dry-run --profile data --targets c --pair c d
```

## Targets and scopes

Automatic preview selection uses approved, writable archive volumes in the chosen profile. Targets marked read-only, offline or failing identity checks appear with their status. One connected target is sufficient for a scope preview. Pass one or two exact aliases with `--targets` and `--profile` to choose specific volumes. An explicitly selected read-only archive can be included to check an existing copy from its recorded pair during execution. The preview reserves no write space for that target. If an explicitly selected volume is unavailable or more than two volumes are available by default, the preview asks for a target selection.

At profile level, **All** covers the complete tree of every assigned root collection. Selecting a collection covers its full tree, including helper files and child stages, unless a month is supplied. Plain collections without dated children have no month choice. Files at different paths remain separate candidates, including equal content. Each file uses its deepest configured collection's policy and registered destination. Monthly scopes exclude approval of local-deletion propagation and retain the configured change policy.

A month includes `YYYY/YYYY.MM` under each configured phone-style collection and matching day folders under `Różne YYYY` for camera collections. At profile level, an explicit month combines the dated collections and leaves plain collections outside that scope. The planner reads directory layouts and does not use EXIF, filenames or modification times to assign a month.

## Required archive pairs

The preview checks that the selected targets are on different physical disks and separate from the selected sources. It also checks mounted members of the proposed pair even when only one was selected for transfer. A disconnected member is allowed unless the registry already identifies both members as the same physical device.

A saved pair and its required path still apply to the same local occurrence, matched by volume, collection, path and current SHA-256. Only selected members of that pair are checked. The preview counts files assigned outside the selection separately. For new occurrences, two configured active targets form the proposed pair, even if only one is connected. Use `--pair` with two active target aliases to choose a new pair explicitly when there is no unique default. `--targets` chooses the connected disks to inspect. The preview leaves both existing and proposed assignments in SQLite unchanged. The transfer stage must commit a new assignment before its first copy.

## Month availability

Select a dated collection to show **All** followed by the months with pending work, newest first. The list depends on the selected connected targets. A month appears when at least one selected member of a required pair needs a copy, a replacement or a conflict variant. The planner checks the required path and compares SHA-256 for same-size files. Append-only scopes also hash the source to choose a conflict name or find historical copies. Large scopes can take time to read. An explicit `--month` restricts the directory and content checks to that month. File and byte totals describe the selected scope and include copies already present on the targets.

Read and scope errors are shown with exit code `3`. Both the month list and an explicit month identify any file or other non-directory entry blocking an expected date directory. Treat an incomplete preview as unresolved.

## Capacity of the whole batch

Each selected disk shows its available bytes, required bytes and any deficit. The planner rejects batches that need the same archive path as both a file and a directory, including case or Unicode normalization equivalents. The budget covers new content rounded to allocation blocks, directories, one temporary transfer directory per target and a new snapshot generation. A replacement keeps the old file allocated while reserving the full new file. A conflict reserves room for a new variant. A verified current copy consumes no transfer allocation. The configured reserve is at least 1 GiB per target. The calculation gives no credit for clearing parts, replacing files or deleting older snapshots.

BACKUP reads the complete old and new contents before it records a replacement intent. [Controlled replacement](backup-replacement.md)

The snapshot estimate includes SQLite's current page count, an allowance of 16 pages per planned copy or check for history growth, the configuration, a manifest allowance and new snapshot directories. Any unavailable capacity calculation or shortage on a selected disk blocks the entire preview with exit code `3`. A full connected disk stays selected and its deficit is reported. `backup --yes` recalculates the media plan, budgets snapshot-only recipients and rechecks the actual generation size before publication. [Snapshot recipients](backup-snapshots.md)

The Python API is `media_archive.backup_planning.plan_backup(config, ...)`. It returns CLI details, candidate files with effective policies, and per-target copy assessments with their required paths and pairs. The preview reads media and SQLite. It creates no reports, media copies, assignments or database records. Tests use registered synthetic volumes to cover scope selection, policy inheritance, month availability and the command's read-only behavior.
