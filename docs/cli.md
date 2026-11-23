# Command-line setup and planning

Use `media-archive` or `python -m media_archive`. Put `--config FILE` before the command to select a configuration file. The default is `.env` in the working directory.

| Command | Behavior |
| --- | --- |
| `--help` | Display available commands and options. |
| `--check-config` | Validate the configuration file without opening SQLite or checking devices. Passing only `--config FILE` also validates the selected file. |
| `setup --dry-run` | Show the database path and registration counts. For an existing database, open it read-only and compare the registry if the schema is current. An older schema is reported as requiring explicit migration. |
| `setup --yes` | Confirm first setup. Create `APP_STATE_DIR` if needed, build the current schema and initial registry in a temporary database, then publish `state.sqlite3` without replacing any existing file. |
| `migrate [--dry-run]` | Upgrade an existing database explicitly or show its current and target revisions without upgrading it. |
| `snapshot DIRECTORY` | Create a validated state generation in an existing directory. |
| `verify-snapshot GENERATION` | Validate a generation independently of the working configuration. |
| `--config FILE restore GENERATION --yes` | Restore working state in an existing `APP_STATE_DIR` using the selected configuration. |
| `device check BINDING [--to DIRECTORY]` | Verify a mounted Linux volume for read access. |
| `device register\|replace\|reformat BINDING [--to DIRECTORY] [--yes]` | Preview or explicitly register one verified volume binding change. See [device commands](devices.md#commands) for path requirements. |
| `audit --group GROUP --targets FIRST SECOND [--mode quick] [--dry-run] [--report]` | Compare archive paths, types and regular-file sizes. See [quick audit](audit.md#quick-audit). |
| `audit --mode full --group GROUP --targets FIRST SECOND` | Compare SHA-256 and save progress in SQLite. See [full audit](audit.md#full-sha-256-audit). |
| `audit --resume AUDIT_ID` | Resume a saved full audit on the same registered volumes. |
| `inventory [--hash-local]` | Build or resume the [initial inventory](inventory.md); optionally read local SHA-256. |
| `init-phone PROFILE [--audit AUDIT_ID]` | Capture a [phone baseline](phone-initialization.md) or compare it using a completed full audit. |
| `import --source PROFILE --dry-run [--card CARD --mount DIRECTORY] [--reimport RELATIVE_PATH] [--report]` | Show the [import plan](import-planning.md), decisions and staging preflight without copying. |
| `import --source PROFILE --card CARD --mount DIRECTORY [--reimport RELATIVE_PATH]` | Execute [camera card import](camera-import.md) with a text and JSON report. |
| `import --source PROFILE [--reimport RELATIVE_PATH]` | Execute [phone import through ADB](phone-import.md) with a text and JSON report. |
| `pending` | List unfinished journal operations and their latest attempt states. A listed operation may still have a live owner. |
| `resume ID --dry-run` | Show the next step for an unfinished operation using its numeric operation ID. |
| `resume ID [--card CARD --mount DIRECTORY]` | Resume a saved [IMPORT](import-recovery.md) or [SORT](sort.md) operation, including cleanup. The card arguments apply only to camera import. |
| `reconcile [--audit AUDIT_ID]` | Check [restored IMPORT and SORT state](import-recovery.md#after-restoring-sqlite) and clear the restore marker when resolved. Repeat `--audit` for multiple archive groups. |
| `sort --dry-run [--report]` | Read staged media and preview [SORT destinations](sort-planning.md), including files awaiting a valid date. |
| `sort` | [Place verified imports](sort.md) in their collections or the unresolved directory and save a text and JSON report. |
| `sort-date ID YYYY-MM-DD` | Set the intended calendar date for a pending import in SQLite. |
| `search [--name TEXT] [--month YYYY-MM] [--source TYPE:PROFILE]` | Search [saved media locations](catalog.md#search). |
| `conflicts [--keep-both GROUP]` | List [SORT conflicts](catalog.md#review-decisions) or save a review decision. |
| `catalog-move ID ABSOLUTE_PATH` | Verify an [existing manual move](catalog.md#reconcile-a-manual-move) and update the catalog. |
| `backup --append-test --profile ID --targets ALIAS ...` | Run an [isolated append-only backup trial](backup-append.md) with an explicit configuration. |
| `backup --dry-run [--profile ID] [--targets ALIAS ...] [--pair ALIAS ALIAS] [--collection ID] [--month YYYY-MM]` | Preview [BACKUP scope, policies and capacity](backup-planning.md). |

All commands that need existing state report a missing database as an error. Setup and migration use the [SQLite lifecycle](state.md#first-setup-and-upgrades). The configuration wizard is a separate planned feature. Camera import uses the [journal](journal.md) and [history](history.md).

The [snapshot reference](snapshots.md) covers generation contents, filesystem effects and the reconciliation requirement.

The recovery preview distinguishes a new attempt from an explicit takeover of an unfinished attempt. A takeover requires acquiring the operation's locks first. The preview reports the suggested action from the journal state.

## Reports and exit status

Setup, migration, audit, inventory, phone initialization, import, SORT, resume and reconciliation executions save one UTF-8 text/JSON pair per finished invocation after loading a valid configuration and checking output locations. Planning displays its result in the terminal. Add `--report` to a dry run to save the pair. A dry run makes no state, registration or media changes. If its report directory is under the state directory, producing that report may also create the parent directories.

State and report paths are checked against resolved collection roots, including collections reached through symbolic links. When a report is requested, the CLI rejects an existing non-directory or symbolic link at `APP_REPORTS_DIR` before setup or migration writes state.

Reports use Polish messages and JSON field names. Each pair has an invocation ID and includes command, completion time in UTC, result code and available details. Journal previews include the durable operation ID and latest attempt's run ID where available. `APP_REPORT_KEEP_RUNS` defaults to 20 in the configuration template. Rotation considers completed report pairs, including failed commands that returned an outcome. Plans, interrupted reports and unrelated files are excluded. SQLite contains the durable evidence and data needed to resume.

Rotation checks the Media Archive format marker and requires the invocation ID and completion time in `report.json` to match the directory name. Directories with missing or inconsistent metadata are left in place.

| Code | Meaning |
| --- | --- |
| 0 | Success or completed preview |
| 1 | Execution error |
| 2 | Invalid arguments or configuration |
| 3 | Partial or blocked result. An audit, inventory, phone initialization, import, resume, reconciliation or SORT preview has pending work, a published database or snapshot could not be confirmed durable, or a completed execution could not save its report. |
| 4 | Audit completed and detected differences in paths, attributes or full-mode SHA-256. |
| 130 | Interrupted by the operator |

If directory synchronization fails after setup publishes the database, the command returns and reports code 3. Use `setup --dry-run` to check the existing database and registry. A subsequent `setup --yes` refuses to replace the published file.

[CLI tests](../tests/test_cli.py) exercise the installed entry point. [Operational tests](../tests/test_operational_cli.py) check setup rollback and retry, partial results, report path validation, read-only planning and report retention with temporary state.
