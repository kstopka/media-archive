# Confirmed BACKUP cleanup

Choose an explicit profile and targets and use the All scope for the profile or one of its root collections. `--yes --full-read` enables BACKUP execution with full verification and eligibility for cleanup. Removing files from the archive requires approval of the exact list shown during that run.

```bash
media-archive backup --yes --full-read --profile data --collection photos --targets a b
```

The command first budgets the whole selected batch and completes the applicable append and replacement transfers and their verification. It then scans the local and archive trees of the eligible collections on the selected writable targets. A collection needs both its own `COLLECTION_*_ALLOW_DELETE=true` and a policy with `DELETIONS=confirm_each_run`. The example configuration grants this to the three selection stages and the print backlog. Monthly scopes skip cleanup.

## Exact list and confirmation

BACKUP lists individual files that lack a corresponding local path by target alias, collection, disk-relative path, byte count and SHA-256. A known conflict variant is protected while its original local path exists. An untracked suffix-shaped file in an append collection is also protected while its plausible base path exists. BACKUP keeps directories and reserved transfer parts in place and leaves contents of other collections untouched.

In a terminal, type `USUŃ` to approve the displayed list for this invocation. Press Enter to decline. Approval requires both input and the prompt to use a terminal. Declining keeps completed transfers and includes the proposed list in the report. No confirmation is stored for later invocations.

## Revalidation and outcome

After approval, the trial reopens the selected volumes, rescans the eligible trees and compares the entire list before removing its first file. A missing source directory or selected disk, scan error, unstable name or change to the approved state stops cleanup. Each removal also checks the local absence and the archive file's identity and full digest through the verified volumes immediately before unlinking. Application locks stay held through approval and execution. Avoid editing these trees in external tools during a run.

SQLite records an intent immediately before each deletion. After the file is removed and the parent directory is synchronized, one transaction marks known locations absent on that volume and clears the intent. A failure during cleanup can leave a partial result. Repeat the same BACKUP command to reconcile unfinished intents on the selected volumes. BACKUP confirms and syncs an absent path before updating the catalog. If the file still exists, deletion requires approval of the new list.

The report lists completed removals, reconciled intents and any failure. Exit code `3` indicates a blocked or partial run and `130` indicates interruption.

[Synthetic tests](../tests/test_backup_planning.py) exercise exact lists on distinct targets, policy boundaries, missing or unreadable sources, changes during approval and interruption before and after unlink.
