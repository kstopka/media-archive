# Executing SORT

Run `media-archive migrate` for an existing database to enable SORT and its catalog. Review the [metadata and destination preview](sort-planning.md), then run:

```bash
media-archive sort --dry-run
media-archive sort
```

SORT works from verified imports on registered local volumes. A file with a date goes to its assigned collection. A file awaiting a date moves to `LOCAL_UNDATED_ROOT`, where it stays outside the default backup scope. Use the reported import ID with `sort-date ID YYYY-MM-DD`, then run SORT to place it in its collection. The command reads the working copy and leaves the phone, card and existing event folders alone.

## Publication and collisions

SORT locks the working directories and the relevant collection roots. It rechecks the registered volumes and records the selected destination, date and metadata in the journal. At the destination it checks case, Unicode normalization and ambiguous names. An existing verified file of the same content can be reused. If the name belongs to different content, SORT preserves it and adds a SHA-256 suffix starting at eight characters to the new variant. The suffix grows when required. A durable conflict group records the known variants and their original name and paths. Use [conflict review](catalog.md#review-decisions) to inspect variants and record a decision.

On the same mount, SORT can publish a hard link directly under the final name after verifying the working file. Across mounts, when hard links are unavailable or when the working inode already has other names, SORT uses rsync and checks the remaining space reserve. The copy path requires a non-overwriting rename for publication. SORT commits the result before removing its previous working copy and rechecks the published copy before unlinking. It retains the working copy if the destination is uncertain. Embedded metadata and file contents are preserved.

## Recovery

`pending` lists unfinished moves, including placements with cleanup still outstanding. Use `resume ID --dry-run` to inspect the operation and `resume ID` to retry it. SORT also resumes pending moves on the next run. It checks the stored destination for equivalent names and verifies a possibly published file before copying again. A conflicting file that appeared after planning is kept in place, and the operation reports that it needs attention. Finish a pending move before changing its manual date.

After a state restore, `reconcile` reads the registered working and final locations for pending SORT operations. It can confirm a published file or allow retry when a matching working copy remains. Reconciliation leaves cleanup to SORT or `resume`. Unresolved operations and unrecognized entries in the unresolved-date directory retain the restore marker. See [recovery after restore](import-recovery.md#after-restoring-sqlite).

SORT and `resume` save text and JSON reports with outcomes and available operation IDs. Exit code `0` includes an empty batch. Code `3` indicates blocked files or material awaiting a date. Code `130` means execution was interrupted. `sort --dry-run` shows proposed paths using the date and collection layout. Execution checks the actual destination and reports the final path if a name conflict changes it.

The Python entry points are `media_archive.sort_execution.execute_sort(config)` and `resume_sort(config, operation_id)`. [Tests](../tests/test_sort_planning.py) exercise same-mount publication, copying with rsync, name collisions, manual resolution of missing dates, and interrupted publication and cleanup on isolated files.
