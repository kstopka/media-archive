# Confirmed BACKUP cleanup trial

Use an isolated configuration, separate state database and test archives. Run `media-archive migrate` on the test database to apply the current revision. Choose an explicit profile and targets and use the All scope for the profile or one of its root collections.

```bash
media-archive --config fixture.env backup --cleanup-test --profile data --collection photos --targets a b
```

The command first budgets the whole selected batch and completes the applicable append and replacement transfers and their verification. It then scans the local and archive trees of the eligible collections. A collection needs both its own `COLLECTION_*_ALLOW_DELETE=true` and a policy with `DELETIONS=confirm_each_run`. The example configuration grants this to the three selection stages and the print backlog. Monthly scopes are excluded from the trial.

## Exact list and confirmation

The trial lists individual files by target alias, collection, disk-relative path, byte count and SHA-256. It lists files that lack a corresponding local path. A known conflict variant is protected while its original local path exists. An untracked suffix-shaped file in an append collection is also protected while its plausible base path exists. The trial keeps directories and reserved transfer parts in place and leaves contents of other collections untouched.

In a terminal, type `USUŃ` to approve the displayed list for this invocation. Press Enter to decline. Approval requires both input and the prompt to use a terminal. Declining keeps completed transfers and includes the proposed list in the report. No confirmation is stored for later invocations.

## Revalidation and outcome

After approval, the trial reopens the selected volumes, rescans the eligible trees and compares the entire list before removing its first file. A missing source directory or selected disk, scan error, unstable name or change to the approved state stops cleanup. Each removal also checks the local absence and the archive file's identity and full digest through the verified volumes immediately before unlinking. Application locks stay held through approval and execution. Avoid editing these trees in external tools during a run.

SQLite records an intent immediately before each deletion. After the file is removed and the parent directory is synchronized, one transaction marks known locations absent on that volume and clears the intent. A failure during cleanup can leave a partial result. Repeat the same cleanup command to reconcile unfinished intents on the selected volumes. The trial confirms and syncs an absent path before updating the catalog. If the file still exists, deletion requires approval of the new list.

The report lists completed removals, reconciled intents and any failure. Exit code `3` indicates a blocked or partial run and `130` indicates interruption.

[Synthetic tests](../tests/test_backup_planning.py) exercise exact lists on distinct targets, policy boundaries, missing or unreadable sources, changes during approval and interruption before and after unlink.
