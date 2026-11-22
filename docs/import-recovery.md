# Import recovery

Find the operation ID in the import report or with `media-archive pending`. Preview the next journal step, then retry that operation:

```bash
media-archive resume 42 --dry-run
media-archive resume 42
media-archive resume 42 --card card-a --mount /example/card
```

Execution reacquires the recorded resource locks and verifies the staging volume. If an earlier attempt could have published the file, it independently reads and hashes the destination first. A matching destination is recorded with fresh verification and commits exactly one import. This path works with the phone or card disconnected. A missing destination permits copying again from the saved source. For a camera, supply the registered card and its mount point. For a phone, connect and authorize the phone recorded for the operation. A new copy starts from the beginning and checks the staging space reserve. If the source moved, run a new import plan against its current location. A mismatching or unsafe destination stops recovery and remains untouched. Interrupted parts remain available for explicit cleanup.

Successful attempts and their verified source and staging locations remain in history. Repeating an already completed `resume` returns the original result. If a phone resume fills its last baseline gap, it also completes initialization.

## Missing staging

The latest import of each content identity in a profile requires staging material until collection placement is recorded. Import planning checks that pending paths contain regular files of the recorded size on the registered staging volume. The content is checked independently again when it is used for recovery or placement. A missing pending file is shown in the report and is never copied automatically. Use `import --source PROFILE --reimport RELATIVE_PATH` with the normal card options if applicable. The source path must contain the same known content for the reimport to fill the gap. During phone initialization, this option is limited to previously imported baseline content missing from staging. It requires a path from the saved baseline and the original phone and camera root. A successful reimport creates a new verified copy and preserves the old import event. A recorded placement keeps its history after manual selection or deletion.

## After restoring SQLite

Run `media-archive reconcile` before importing or resuming. For a phone baseline match backed by local copies, reconciliation verifies SHA-256 and size at one of the recorded paths in the collection. If every recorded copy is missing or differs, it reports the unresolved match and retains the restore marker. Restore a matching copy to a recorded path before retrying. For each archive group that supplied an archive-only baseline match, run a fresh [full audit](audit.md#full-sha-256-audit) and supply its ID with `--audit AUDIT_ID`. The command requires a post-restore SHA-256 read for each such match in the appropriate collection on the current audit pair. Several groups can be supplied by repeating the option.

Reconciliation verifies known published staging files by SHA-256, confirms absent destinations, and resolves unfinished import attempts with the journal. A known missing staging copy is reported and can be explicitly reimported once reconciliation succeeds. Unrecognized staging files, mismatches, missing required audit evidence and unresolvable attempts keep the restore lock in place. Preserve an unrecognized file outside staging before retrying, once you have established where it belongs. The command never modifies media. Journal changes and removal of the restore marker commit together. This workflow covers IMPORT state. An older snapshot cannot reconstruct history for files no longer present on any medium.

Execution saves a UTF-8 text and JSON report. Code `0` means the selected operation completed, `3` indicates blocked or partial recovery, and `130` indicates interruption. See [CLI exit codes](cli.md#reports-and-exit-status) for other errors. The Python entry points are `media_archive.import_recovery.resume_import()` and `media_archive.import_restore.reconcile_imports()`.

Tests use [temporary staging and real rsync](../tests/test_import_planning.py), [synthetic ADB](../tests/test_sources.py), and [synthetic archive volumes](../tests/test_inventory.py). They cover interrupted publication, mismatching destinations, retries without duplicates, missing staging, and stale audit evidence after restore.
