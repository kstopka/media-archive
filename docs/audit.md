# Quick archive audit

Quick audit compares two explicitly selected targets of the same registered archive group. Both targets can be connected at once for a complete comparison. A read-only configured target can participate.

```bash
media-archive audit --group data --targets a b
media-archive audit --group data --targets a b --dry-run --report
```

`quick` is the default and currently the only accepted mode. Target order defines the first and second side in the report. The command checks live mounts and registered identities and rejects two accessible targets on the same physical disk. It scans each target's complete registered group root, including paths that belong to no backup profile. Paths outside the registered root are out of scope.

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
| 4 | Both scans complete and the audit detected differences. |
| 130 | Interrupted by the operator. |

The [CLI reference](cli.md) covers report retention. [Synthetic audit tests](../tests/test_audit.py) exercise metadata comparisons, missing disks, directory read failures, interruption, observation rollback, distinct disk checks, report rotation and dry runs. They use temporary archives and SQLite state. Full SHA-256 audit, checkpoints and sequential attachment are planned for the next stage.
