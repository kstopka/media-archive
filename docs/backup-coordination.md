# Coordinated BACKUP

```bash
media-archive backup --yes --profile data --collection photos --targets a b
```

Select one or two registered archive disks explicitly. BACKUP applies both append and verified replacement policies. It uses a queue for each available target and runs at most one transfer per disk and two transfers at once. The workers serialize short SQLite operations and release that lock while reading or copying file contents. The whole batch must pass the capacity check before copying. Each copy uses the recorded pair and required path for that disk. If one requested disk is disconnected, BACKUP reports it and can fill copies on the available member of the pair.

## Failures and retry

A transfer error stops that disk's queue and lets the other disk finish. If preflight finds different bytes at a pinned append-only path, that disk's queue is blocked before writing and the other disk can proceed once the whole batch passes its capacity check. If a target runs out of space or falls below its required transfer reserve, both queues stop. Transfers already published and verified keep their results. Interrupted work is checked by the existing journal on the next run. Repeat the command with the available members of the same pair to fill missing copies.

The command reports exit code `3` for a blocked or partial run, including an explicitly requested disconnected target. Selecting a single available disk can complete successfully while a file still shows `1/2`.

## Evidence and manual space release

Each file reports `0/2`, `1/2` or `2/2` based on verified copies of the current local content in this invocation. The report includes the target, exact path and verification ID for each counted copy. A new transfer uses its independent full SHA-256 read before publication. An existing copy needs a fresh full read. A selected target configured as read-only can supply that evidence for its member of a recorded pair. A missing or mismatching file on that target is reported and checks continue with the other files. The final check compares the archive paths and physical mounts, then rechecks the local source before counting the copies. This uses the saved SHA-256 reads without hashing new copies again. A changed source, changed copy or unavailable disk invalidates the affected evidence.

The report indicates manual space release only for paths with two fresh copies on separate physical disks. It highlights paths with confirmed deletion policies because removing the local file can make the next All run propose archive cleanup. The indication is scoped to the current run.

BACKUP checks fresh copy evidence after publishing snapshots. Synthetic tests cover overlapping transfers, one-target completion and retry, shared stop on space exhaustion, and invalidation after verification. [Snapshots](backup-snapshots.md), [BACKUP tests](../tests/test_backup_planning.py)
