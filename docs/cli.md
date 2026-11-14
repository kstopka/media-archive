# Command-line setup and planning

Use `media-archive` or `python -m media_archive`. Put `--config FILE` before the command to select a configuration file. The default is `.env` in the working directory.

| Command | Behavior |
| --- | --- |
| `--help` | Display available commands and options. |
| `--check-config` | Validate the configuration file without opening SQLite or checking devices. Passing only `--config FILE` also validates the selected file. |
| `setup --dry-run` | Show the database path and registration counts. For an existing database, open it read-only and compare the registry if the schema is current. An older schema is reported as requiring explicit migration. |
| `setup --yes` | Confirm first setup. Create `APP_STATE_DIR` if needed, build the current schema and initial registry in a temporary database, then publish `state.sqlite3` without replacing any existing file. |
| `migrate [--dry-run]` | Upgrade an existing database explicitly or show its current and target revisions without upgrading it. |
| `pending` | List unfinished journal operations and their latest attempt states. A listed operation may still have a live owner. |
| `resume ID --dry-run` | Show the next step for an unfinished operation using its numeric operation ID. Actual file recovery will be available with the transfer workflow. Until then, `resume ID` returns status 1. |

All commands that need existing state report a missing database as an error. Setup and migration use the [SQLite lifecycle](state.md#first-setup-and-upgrades). The configuration wizard is a separate planned feature. Media operations will use the [journal](journal.md) and [history](history.md).

The recovery preview distinguishes a new attempt from an explicit takeover of an unfinished attempt. A takeover requires acquiring the operation's locks first. The preview reports the suggested action from the journal state.

## Reports and exit status

Setup and migration executions save one UTF-8 text/JSON pair per finished invocation after loading a valid configuration and checking output locations. Planning displays its result in the terminal. Add `--report` to a dry run to save the pair. A dry run makes no state, registration or media changes. If its report directory is under the state directory, producing that report may also create the parent directories.

State and report paths are checked against resolved collection roots, including collections reached through symbolic links. When a report is requested, the CLI rejects an existing non-directory or symbolic link at `APP_REPORTS_DIR` before setup or migration writes state.

Reports use Polish messages and JSON field names. Each pair has an invocation ID and includes command, completion time in UTC, result code and available details. Journal previews include the durable operation ID and latest attempt's run ID where available. `APP_REPORT_KEEP_RUNS` defaults to 20 in the configuration template. Rotation considers completed report pairs, including failed commands that returned an outcome. Plans, interrupted reports and unrelated files are excluded. SQLite contains the durable evidence and data needed to resume.

Rotation checks the Media Archive format marker and requires the invocation ID and completion time in `report.json` to match the directory name. Directories with missing or inconsistent metadata are left in place.

| Code | Meaning |
| --- | --- |
| 0 | Success or completed preview |
| 1 | Execution error |
| 2 | Invalid arguments or configuration |
| 3 | Partial result. A published database could not be confirmed durable, or the state change succeeded and report writing failed. |
| 130 | Interrupted by the operator |

If directory synchronization fails after setup publishes the database, the command returns and reports code 3. Use `setup --dry-run` to check the existing database and registry. A subsequent `setup --yes` refuses to replace the published file.

[CLI tests](../tests/test_cli.py) exercise the installed entry point. [Operational tests](../tests/test_operational_cli.py) check setup rollback and retry, partial results, report path validation, read-only planning and report retention with temporary state.
