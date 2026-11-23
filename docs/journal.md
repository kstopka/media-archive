# Operation journal and locks

Revision `0004` adds a journal for operations that publish one destination occurrence. A durable operation records its stable key, intent, destination occurrence and required resources. Each attempt records a run ID from the [history API](history.md), its current stage, and dated events. Reports can be removed independently of this state.

## Resource locks

Use `media_archive.state.locking.lock_resources(database, resources)` around the entire operation, including file work between short SQLite transactions. The context acquires every resource or raises `OperationBusy` and releases any locks acquired in that call. Retry acquisition after the conflicting operation exits.

- A collection resource uses the canonical path of its root directory. Use `collection_resource(config, collection_id)` to obtain it. The root takes an exclusive lock and its ancestors take shared locks. This blocks operations on the same or nested roots even when configuration reassigns the directory to a different collection ID. Disjoint roots can share ancestor locks.
- `Resource("archive", group_id)` covers the entire archive group.
- `Resource("source", str(source_id))` covers the registered source profile.

The workflow supplies the full set of resources it touches. For example, backup needs the source collection root and destination archive group. Planning validates the registered configuration in the same SQLite transaction and checks that a local destination has its root collection lock and an archive destination has its group lock. Operators using the same working database must cooperate through these locks.

Linux `flock` holds locks on persistent files beside the canonical database path. The database and its directory must be under application control on a local filesystem. Keep the lock files in place. The lock ends when the owning context closes its descriptors or the process exits. Workers must not retain inherited lock descriptors. Time elapsed and PID records do not grant ownership. Separate installations use separate locks. A shared installation lock remains held for the duration of the resource locks. Restore needs exclusive access to the same installation. See [state snapshots](snapshots.md).

## Journal API

All writes in `media_archive.state.journal` require the active handle yielded by `lock_resources()` and `transaction(connection)`. Hold the locks until after the transaction commits. Use a short transaction for each checkpoint and perform physical file work between transactions.

| API | Contract |
| --- | --- |
| `plan_operation(connection, locks, config, ...)` | Record a stable `operation_key`, UTF-8 `intent`, `target_occurrence_id` and `required_resources` before file work. Repeating the same plan returns the same ID. |
| `start_attempt(connection, locks, operation_id, run_id, recovery=None)` | Start an attempt with its own durable run ID. A committed operation cannot be attempted again. |
| `advance_attempt(connection, locks, attempt_id, state, verification_id=None)` | Advance one stage. Entering `verified` requires matching evidence recorded after the attempt began for this destination and this run. |
| `restart_after_missing_destination(connection, locks, attempt_id, observation_id)` | Return reconciliation to `planned` using an observation of absence recorded after this attempt began for its destination and run. The event retains the observation reference. |
| `restart_before_replacement(connection, locks, attempt_id, verification_id)` | Return a BACKUP replacement attempt to `planned` when a fresh full read of its destination matches the old digest and size in the operation intent. |
| `stop_attempt(connection, locks, attempt_id, state, error_code=...)` | Preserve the attempt as interrupted, failed, in conflict or in need of review. Supply a short diagnostic code free of private data. |
| `commit_result(connection, locks, attempt_id, record_result)` | Invoke the history writer and mark the published attempt committed in the same transaction. The callback receives the connection and verification ID. A callback failure rolls back its history writes and leaves the attempt published. |
| `load_operation()`, `load_resources()`, `load_attempts()`, `load_events()`, `load_unfinished()` | Read plans, required resources, attempts, events and pending operations. An unfinished listing does not grant ownership. |

The usual sequence is `planned → transferring → transferred → verifying → verified → published → committed`. Publication is asserted by the caller after the file has been published and persisted. The history writer uses the supplied connection and the existing history API for the import, collection placement or archive copy. The caller controls the outer transaction. SQLite permits one committed result per operation. Invalid journal transitions raise `StateError`. Configuration mismatches use the registry's `ConfigError`.

The [shared transfer API](transfer.md) drives these transitions for a single planned copy, including fresh verification on recovery.

## Recovery

1. Read the unfinished operation and its resources, end the read transaction, and acquire the same locks.
2. Allocate a new run ID and explicitly start a recovery attempt. Use `recovery="restart"` for an abandoned attempt before publication was possible. Use `recovery="reconcile"` to inspect a possibly published destination. The old active attempt becomes interrupted and keeps its events.
3. Reconciliation starts at `verifying`. Record matching verification after the attempt starts before confirming publication and committing history. If the workflow confirms the destination is absent, record that observation and call `restart_after_missing_destination()` before transferring again. Keep uncertain destinations in `needs_review`. A reconciliation interrupted before verification or confirmation of absence still requires reconciliation on the next attempt.

These APIs store evidence supplied by the workflow. The [import recovery commands](import-recovery.md) use the transfer and device APIs to verify on-disk files before committing recovery. [Journal tests](../tests/test_state_journal.py) exercise migration, process contention and termination, checkpoint recovery and atomic result recording on synthetic state.
