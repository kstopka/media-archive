# BACKUP scope preview

Run `backup --dry-run` to see the registered backup profiles that match the connected archive volumes. A single matching profile is proposed and its scope is previewed. When several profiles match, choose one via `--profile`; no media is scanned until the profile is selected.

```bash
media-archive backup --dry-run
media-archive backup --dry-run --profile data
media-archive backup --dry-run --profile data --targets a --collection photos
media-archive backup --dry-run --profile data --targets a b --collection photos --month 2025-02
```

## Targets and scopes

The preview selects approved, writable archive volumes in the chosen profile. Targets marked read-only, offline or failing identity checks appear with their status. One connected target is sufficient for a scope preview. Pass one or two exact aliases with `--targets` and `--profile` to choose specific volumes. If an explicitly selected volume is unavailable or more than two volumes are available by default, the preview asks for a target selection.

At profile level, **All** covers the complete tree of every assigned root collection. Selecting a collection covers its full tree, including helper files and child stages, unless a month is supplied. Plain collections without dated children have no month choice. Files at different paths remain separate candidates, including equal content. Each file uses its deepest configured collection's policy and registered destination. Monthly scopes exclude approval of local-deletion propagation and retain the configured change policy.

A month includes `YYYY/YYYY.MM` under each configured phone-style collection and matching day folders under `Różne YYYY` for camera collections. At profile level, an explicit month combines the dated collections and leaves plain collections outside that scope. The planner reads directory layouts and does not use EXIF, filenames or modification times to assign a month.

## Month availability

Select a dated collection to show **All** followed by the months with pending work, newest first. The list depends on the selected connected targets. A missing file or a size difference is enough to show a month. The planner compares SHA-256 for same-size files if the metadata check finds no work. Depending on the volume of already matching data, that read can take time. An explicit `--month` restricts the directory and content checks to that month. File and byte totals describe the selected scope and include copies already present on the targets.

Read and scope errors are shown with exit code `3`. Both the month list and an explicit month identify any file or other non-directory entry blocking an expected date directory. Treat an incomplete preview as unresolved. This stage discovers scope and policies. Physical independence, persistent required pairs, capacity checks, transfer, cleanup and snapshots belong to subsequent BACKUP stages.

The Python API is `media_archive.backup_planning.plan_backup(config, ...)`. It returns CLI details and candidate files with their required archive paths and effective policies. The preview reads media and SQLite. It creates no reports, media copies, assignments or database records. Tests use registered synthetic volumes to cover scope selection, policy inheritance, month availability and the command's read-only behavior.
