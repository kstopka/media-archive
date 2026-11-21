# Phone import through ADB

Connect and authorize the Android phone registered for the profile. Complete the [baseline comparison](phone-initialization.md) before importing. The staging volume must be registered and writable.

```bash
media-archive import --source phone-a --dry-run
media-archive import --source phone-a
```

While `import_missing` is pending, the command copies only recorded gaps whose path, SHA-256 and size still match the baseline on the original phone and camera root. Equal content needs one verified import from a recorded path. Available gaps can be filled across several invocations. If any gap remains, the command reports pending work. Once all gaps have verified imports, the command completes initialization in SQLite. The following invocation imports new content. A profile initialized under `baseline_skip_missing` starts with incremental import and retains the saved skips.

The [shared transfer](transfer.md) reads the planned source through ADB, checks its SHA-256 and the staging copy, and records success after verified publication. Earlier successful files remain recorded if a subsequent file fails. Phone originals remain in place. Repeat the command to continue with remaining eligible files. A staging conflict blocks the plan, including a published file whose success was never recorded. Journal reconciliation for that case is tracked in [the roadmap](roadmap.md#stage-4--import-and-verified-transfers).

`media_archive.phone_import.import_phone(config, profile, *, reimport=(), announce=..., cancelled=None)` returns the manifest, file outcomes and exit status used by the CLI. After initialization, `--reimport RELATIVE_PATH` follows the [shared reimport rules](import-planning.md#decisions-and-capacity). Execution saves a UTF-8 text and JSON report. Code `0` indicates success or no eligible files, `3` indicates blocked or partial work, and `130` indicates interruption. ADB authorization and offline errors are reported as source errors. See the [CLI exit status contract](cli.md#reports-and-exit-status).

[Synthetic ADB tests](../tests/test_sources.py) exercise baseline completion, incremental imports, saved skips, authorization and connection loss.
