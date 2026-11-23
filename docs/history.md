# Media history and initialization

Revision `0003` adds persistent history and initialization state. Use `media_archive.state.history` and `media_archive.state.initialization` with a connection from [`open_database()`](state.md#entry-points-and-ownership). Apply the migration explicitly to an existing database. The APIs write inside the caller's `transaction(connection)`, so a future operation can commit its journal entry and history together. Reads can use a read-only connection. Validation failures and database errors escaping the state transaction use `StateError`.

## Record and read history

| API | Contract |
| --- | --- |
| `create_run(connection)` | Allocate a durable run ID for evidence. The [operation journal](journal.md) uses this ID for execution state. |
| `record_content(connection, sha256, size)` | Return the shared ID of a full SHA-256 and byte count. |
| `record_occurrence(connection, kind, path, ...)` | Create a distinct occurrence and optionally link its known content ID. Record a new occurrence when a different file uses the same path. |
| `record_observation(connection, occurrence_id, run_id, ...)` | Record presence or absence at an explicit time. A presence observation may include byte size and modification time. |
| `record_verification(connection, occurrence_id, run_id, ...)` | Record scope, time, expected SHA-256, actual SHA-256, bytes read and result. A match requires a full read with the expected digest and size. |
| `record_import(connection, source_occurrence_id, verification_id)` | Record a successful import using matching evidence for its staging destination. |
| `record_placement(connection, import_id, verification_id)` | Record a matching verified placement in the collection registered to the source profile. |
| `assign_archive_pair(connection, local_occurrence_id, first_device_id, second_device_id, required_path)` | Record the two required devices and the disk-relative path before backup. |
| `pin_archive_path(connection, local_occurrence_id, device_id, path)` | Finalize the required path for a member of the recorded pair. |
| `record_archive_copy(connection, local_occurrence_id, verification_id)` | Link matching verification on the required path and device to its archive assignment. |

Resolve a registered phone or camera alias to its stable profile ID with `load_source_id(connection, kind, alias)`. Read rows with `load_content()`, `load_occurrence()`, `load_occurrences()` by kind and path, `load_observations()`, `load_verifications()`, `load_imports()` by source profile ID, `load_placement()` by import ID, and `load_archive_pair()` or `load_archive_copies()` by local occurrence ID. The record readers return SQLAlchemy row mappings or tuples of mappings. Singular optional results return `None`. Read the verification through its occurrence ID to get the historical path, device and volume. Evidence timestamps are stored in UTC. SHA-256 is the fixed verification algorithm.

A phone source stores its absolute device path and registered device ID. A card source or archive copy stores a path relative to its registered filesystem episode. Staging and local collection paths are absolute and can include a registered local volume episode where available. Local collection occurrences also retain the collection ID. Quick inventories may record presence with an unknown content ID. A later workflow records a known-content occurrence after establishing identity. History keeps successful imports even when an observation reports that the staging file is missing.

History paths must be representable in UTF-8. Recording or looking up a path with an undecodable filename raises `StateError` without including the path in the diagnostic.

## Archive requirements

Two different registered targets in the collection's archive group must be selected before the first backup. One can be offline while copying to the other. The required path is relative to the disk and must be inside the registered collection destination. When assigning a pair, the deepest registered destination that contains the path must belong to the occurrence's collection. This protects child destinations, including reservations for collections removed from configuration. Requirements refer to device IDs and stay fixed for that occurrence. Completed copy evidence includes the actual volume episode. Selecting a new pair for new material leaves earlier assignments and pending copies in place.

Compare the pair with recorded copy evidence to see which copies have been recorded. Historical evidence describes an earlier read. A current presence or readiness decision requires fresh evidence and physical identity checks in the later workflow.

A file target cannot be a registered destination directory or one of its ancestors. For example, a reservation for `Data/Photos/All/Main` requires `Data/Photos/All` to be a directory. The check uses stored reservations, including those for collections removed from configuration. Filenames without extensions are allowed. The backup workflow creates missing parent directories when it writes the file.

## Initialization

`load_initialization(connection, source_id)` returns `pending` for a registered profile before initialization starts. `start_initialization()` records the policy, baseline boundary and named comparison and processing scopes. `record_initialization_progress()` saves a cursor and the run that produced it, or records completion of that scope. Processing requires all comparison scopes to be complete. `finish_initialization()` requires evidence of completion for every registered scope. The caller supplies the boundary and cursors as strings that its workflow can interpret when resuming. Boundaries, scope names and cursors must be representable in UTF-8. Invalid encoding raises `StateError` without exposing the supplied value.

For `baseline_skip_missing`, start with the registered phone used for the baseline. After comparison completes, `record_baseline_skip()` can record a decision for a known occurrence from that phone. `load_baseline_skips()` reads these decisions by profile. The decision records a skip independently of import and copy history. The caller determines which occurrences fall within the baseline and supplies the evidence from inventory and comparison. Hardware, labels and policy edits preserve recorded initialization state.

The [phone initialization workflow](phone-initialization.md) fixes the baseline manifest, checks the saved evidence and records the decisions. Local gaps that require a transfer leave its processing scope pending.

[History tests](../tests/test_state_history.py) exercise migration, distinct occurrences, evidence, archive requirements, resumable initialization and transaction rollback with synthetic data. The API stores evidence supplied by callers. The [initial inventory](inventory.md) records existing files and durable scope progress independently of per-profile phone initialization.
