# Archive audit

## Quick audit

Quick audit compares two explicitly selected targets of the same registered archive group. Both targets can be connected at once for a complete comparison. A read-only configured target can participate.

```bash
media-archive audit --group data --targets a b
media-archive audit --group data --targets a b --dry-run --report
```

`quick` is the default mode. Target order defines the first and second side in the report. The command checks live mounts and registered identities and rejects two accessible targets on the same physical disk. It scans each target's complete registered group root, including paths that belong to no backup profile. Paths outside the registered root are out of scope.

The audit compares exact relative paths, filesystem entry types and regular-file sizes. It reports entries unique to each side, size and type differences, and name collisions after Unicode NFC normalization and case folding. Symlinks and special files contribute their entry types. The audit follows directories and does not follow links or read file contents. Matching metadata describes the attributes read during the scan. It provides no SHA-256 verification and is not an atomic filesystem snapshot.

When a target or directory cannot be read, the report names the incomplete scope. Paths under that scope are not reported as absent on that side. Observed differences elsewhere are retained. The terminal shows a summary and the normal command writes detailed text and JSON reports to the application reports directory.

Interrupting a scan with Ctrl+C produces a report with the directories read, the scopes left unread, and differences observed so far. Unread scopes remain unknown. Scanning stops before observations are written to SQLite.

A normal run records positive observations of regular files in SQLite, with the registered volume episode, path relative to the mount, size and modification time. Each observation belongs to the run reported under `ID obserwacji`. The observations have unknown content identity and leave existing content and copy evidence intact. The audit uses the archive group lock while collecting and recording observations. It never requests write access on the archive media. A dry run performs the comparison without writing state or operation locks. Add `--report` to save its report.

If observation storage fails, SQLite rolls back the observation transaction. The report retains the scan results, describes the storage error, and leaves `ID obserwacji` empty.

| Exit code | Outcome |
| --- | --- |
| 0 | Both scans complete and compared attributes agree. |
| 1 | Execution failed, for example because the selected targets share one physical disk. |
| 2 | Invalid arguments or configuration. |
| 3 | At least one scan incomplete, observation storage failed, or a completed run could not save its report. Takes priority over detected differences. |
| 4 | Both scans complete and the audit detected differences. This includes content differences in full mode. |
| 130 | Interrupted by the operator. |

## Full SHA-256 audit

Full audit independently reads every regular file on both registered volumes. Run it manually for an initial content comparison or when you need to confirm the copies after a disk problem or replacement. Use the quick audit for routine metadata checks. Apply an explicit `media-archive migrate` if the state database predates revision `0007`. Start a new run with the group and target aliases, or resume using the ID shown at startup and in the report.

```bash
media-archive audit --mode full --group data --targets a b
media-archive audit --resume AUDIT_ID
```

You can connect both targets or read them one at a time. If a target is offline, a completed inventory from the same audit remains usable with its original read times. An incomplete inventory remains pending. A resume must use the same registered volume episodes and archive root. To compare sequentially, the two disks need comparable distinct hardware identifiers, or physical independence must already have been established with both present in the same audit. When the application cannot confirm independence, connect both targets and resume to complete the comparison.

Each successful full read saves its SHA-256, file attributes, time and archive occurrence in SQLite in one transaction. Ctrl+C or a process exit leaves completed checkpoints available. Resume inventories attached targets again and checks the current attributes immediately before reusing a checkpoint. The path, type, size, inode and timestamps must agree on the same registered filesystem. Changed files are read again, and removed files are excluded once their former paths have been checked. Read errors leave the affected scope incomplete and are reported separately from pending files and directories. An offline target without a completed inventory is noted separately. Terminal progress groups thousands in file counts and displays the amount read in decimal units such as MB, GB or TB. Full mode requires durable state and rejects `--dry-run`.

The report lists per-target completion, whether the target was mounted in this invocation, and per-file SHA-256 read times. Code `0` requires complete scans on two distinct disks and agreement on paths, attributes and SHA-256. Incomplete scope or unconfirmed disk independence returns `3`; two complete scopes with differences return `4`. If SQLite rejects a checkpoint or the completed inventory, code `3` preserves the audit ID and the saved reads already verified in this invocation. Ctrl+C returns `130`. With sequential attachment, the result compares the bytes observed at the respective read times. Audit observations and checkpoints remain in SQLite when reports rotate. They can support later reconciliation after restore, whose flag is cleared by a separate workflow.

The [CLI reference](cli.md) covers report retention. [Synthetic audit tests](../tests/test_audit.py) exercise metadata comparisons, missing disks, incomplete scopes, report failures and full audit checkpointing, changed files and sequential attachment. They use temporary archives and SQLite state.
