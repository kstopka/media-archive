# Implementation roadmap

This is the public implementation plan, using neutral examples. Checkboxes record delivered behavior; unchecked items are **planned and not available**. The current implementation reference starts at [docs/README.md](README.md).

Tasks 1.1, 1.2 and 1.2a are implemented; configuration code remains subject to contributor review. Database initialization, media operations and the configuration wizard are not implemented. This roadmap does not authorize operations on real media.

Design references: [archive workflows and state](design/archive-workflows.md) and [future configurator](design/configurator.md). Future module boundaries, database details and command syntax must be finalized during the relevant implementation task.

## Delivery rules

- Work in small, explicitly requested tasks, reviewing each result before continuing. Split large tasks when necessary.
- A completed task includes relevant validation and updated feature documentation. Distinguish synthetic tests, hardware tests and untested behavior.
- Use temporary directories and synthetic data first. Tests or transfers on real devices require explicit authorization. Large archive audits are run locally by the operator with progress and resume support.
- Configuration checks and future dry runs must not register devices, create a production database or mutate media. A dry run may write only an explicitly selected report.
- Snapshot and recovery support are part of delivering backup, not an optional follow-up.

## Stage 1 — Foundation and state protection

- [x] **1.1 Package and CLI skeleton.** Python package, module/console entry point and initial tests. Help works without creating state or accessing media.
- [x] **1.2 Configuration.** Literal `.env` loading with python-dotenv, immutable Pydantic models, named sources/storage and safe validation errors. Preserve older configurations without rewriting them; no TOML layer or environment override.
- [x] **1.2a Collections, policies and backup profiles.** Extensible registries; source-to-collection references; explicit child policies; root scopes and date layouts; archive mappings. Structural validation prepares for later destination locking but does not persist it. See [configuration.md](configuration.md).
- [ ] **1.3 SQLite and operation journal.** Schema/migrations, stable identities, initialization state, import/copy history, destination registration/locking, operation attempts, short transactions and recovery reconciliation. Persist identities independently from editable labels and enforce the [identity guard](identities.md) before execution: unknown IDs require explicit registration, never automatic history creation or initialization restart. Record verification time/run, scope, expected/read hashes and result separately from last-confirmed presence. A changed registered archive destination must fail before media writes; missing local files must not erase history.
- [ ] **1.4 Operational CLI, reports and planning.** Operation/resume contracts, result and interruption codes, Polish text/JSON reports and retention of 20 completed runs. Dry-run must not migrate a database or record operational success.
- [ ] **1.5 State snapshots and restore.** Consistent database/configuration generations, validation and explicit restore with operations stopped. Corrupt snapshots must not replace valid state; restore must preserve history and require reconciliation with later disk changes.

Before production registration, confirm the intended archive layout. Local readiness checks remain separate from the generic schema. Proceed to task 1.3 only after the requested review and explicit continuation instruction, respecting repository-specific contributor instructions.

## Stage 2 — Devices and source reading

This stage provides inventory primitives, not media transfers.

- [ ] **2.1 Device identity and safe paths.** Validate system-mounted volumes, roles, UUIDs and additional evidence. Reject ambiguous/replaced devices, unsafe links and special files. A missing mount must not redirect writes into an empty system directory.
- [ ] **2.2 Local and ADB source adapters.** Listing, attributes, authorization, multiple devices and explicit adapter capabilities. Failures must not become empty-source results; filenames must never execute shell commands.
- [ ] **2.3 SHA-256 and stable reads.** Streaming hashes, progress, cancellation and read-stability checks. Changed sources, failures and interruptions must invalidate evidence; new transfers/full audits cannot rely solely on metadata.

## Stage 3 — First operational feature: AUDIT and initialization

- [ ] **3.1 Quick inventories and comparison.** Scan complete registered archive roots, recording paths, types, sizes, errors and incompleteness. Attribute agreement must never be reported as verified content agreement.
- [ ] **3.2 Full resumable AUDIT.** Hash contents with checkpoints, progress and sequential disk attachment. Revalidate changed files after resuming; report exactly which scope was verified.
- [ ] **3.2a Explicit copy repair.** Deliver after the shared verified-transfer mechanism in 4.1. Present a verified source and exact destination for approval, including recovery from a surviving archive copy after local removal. See [repair design](design/archive-workflows.md#explicit-copy-repair).
- [ ] **3.3 Initial existing-data inventory.** Record local collections and all required archives, preserving progress while disks are offline. Existing files are observations, not fabricated successful imports. No automatic reorganization of existing collections.
- [ ] **3.4 Source baseline initialization.** Persist the scope/boundary of a phone baseline; apply `baseline_skip_missing` only when comparison is complete. Phone-only baseline content may be skipped, never marked imported or archived. `import_missing` fills initial gaps; subsequent imports share incremental history behavior. Camera sources do not inherit the phone baseline exception.

After acceptance, inventory/audit can be used without waiting for IMPORT/SORT/BACKUP. Until archive snapshots are integrated, protect the operational database using the accepted snapshot/restore mechanism.

## Stage 4 — IMPORT and verified transfers

- [ ] **4.1 Shared transfer mechanism.** Local/ADB transport, target-filesystem temporary files, independent source/copy hashes and safe publication. Partial transfers must never become completed copies; reconcile publication/database crash windows. ADB may restart a file instead of resuming by byte offset.
- [ ] **4.2 Import planning.** Source manifests, observations, per-profile dispositions and staging-space preflight. Hash identity in another profile must not suppress a required distinct occurrence.
- [ ] **4.3 Camera import.** Videos/photos, configured exclusions, reporting of unsupported types and content recognition despite formatting or reused names. Preserve source cards; reject hash mismatches.
- [ ] **4.4 Phone import through ADB.** Apply baseline dispositions and import new content, handling authorization/offline states. Preserve phone originals.
- [ ] **4.5 Resume and import history.** Journal attempts, reconcile interruptions and retain history after manual selection. Distinguish verified import, staging presence and completed collection placement. Missing staging before SORT completion requires a visible pending-material error and explicit reimport. Failed/unverified transfers cannot produce success records.

## Stage 5 — SORT and catalog

- [ ] **5.1 ExifTool metadata.** Photo capture dates, film container UTC conversion, raw fields and diagnostics. Test invalid/missing dates, day/month/year boundaries and daylight-saving transitions; never rewrite embedded metadata.
- [ ] **5.2 Destination/date planning.** Configured year/month or day layouts, separate video segments and unresolved dates outside backup collections. Store manually supplied dates in state; do not guess from mtime, filename or import time.
- [ ] **5.3 SORT execution and conflicts.** Move verified staging files; across filesystems, copy/verify before deleting only the program-owned staging copy. Preserve distinct contents with hash suffixes and durable conflict groups. Handle NTFS/Unicode naming conflicts and repeated runs safely.
- [ ] **5.4 Search and conflict review.** Search by name/month/source, show last-known offline locations and reconcile manual moves. Resolving a conflict as “keep both” must not delete either variant.

## Stage 6 — BACKUP with mandatory snapshots

Target planning must persist each material occurrence's required pair. An explicit switch assigns new material to the new pair while earlier material and pending second copies retain their original assignments. See [successive archive pairs](design/archive-workflows.md#successive-archive-pairs). Manual-space-release guidance uses each path's effective deletion policy, including child overrides. Retention paths are eligible after fresh verification. Paths that propagate approved deletions require a separate warning about subsequent archive cleanup.

- [ ] **6.1 Scope and policies.** Manually enter backup, identify/select connected targets and propose a matching profile. Profile All includes assigned roots; collection All includes its complete tree. Month scopes combine configured dated roots and appear only for work pending on selected connected targets. Plain collections have no month submenu.
- [ ] **6.2 Targets and capacity.** Validate independent disks and preflight the entire selected batch, including temporary files, replacements, snapshots and at least 1 GiB reserve. Insufficient space on any selected available target blocks the whole batch before transfer or cleanup.
- [ ] **6.3 Append policies.** Verify each required path/target independently. Skip current verified copies, preserve conflicting contents and intentional copies under different paths; never move historical archive copies automatically.
- [ ] **6.4 Controlled replacement.** Apply only where policy explicitly permits replacement, publishing after verification. Preserve the previous valid copy on failure; changed content requires new evidence.
- [ ] **6.5 Confirmed deletions.** Only explicitly eligible selection/printing subtrees in an All scope, with an exact per-target list, fresh state validation and approval every run. Missing sources, scan failures or changed approved state block cleanup. No blanket mirror/delete operation.
- [ ] **6.6 Two-target coordination.** One transfer per target, at most two targets in parallel, independent results and partial-copy status. One target failure may leave the other running; space exhaustion during execution stops further copying to both. Keep successful copies. Manual-space-release readiness requires full SHA-256 reads of both current copies in the same run on distinct connected physical disks, limited to selected material. Reuse a new copy's full verification from that run without reading it twice; historical/offline evidence alone cannot grant readiness. See [fresh-proof rules](design/archive-workflows.md#fresh-proof-before-manual-local-space-release).
- [ ] **6.7 Snapshot/recovery integration.** Produce one consistent generation after recording backup results, saving it on connected registered writable archives outside audit roots. Keep five validated generations per target. Snapshot failure makes the run incomplete even if media succeeded; restore reconciles later file state.

Backup is ready for normal use only after the **entire stage**, including snapshots and recovery, is accepted. Earlier transfer components remain test-only.

## Stage 7 — Integration and controlled rollout

- [ ] **7.1 End-to-end and failure scenarios.** IMPORT → SORT → manual selection → BACKUP → AUDIT → state restore. Test disconnections, source changes, capacity failures and publication/database crash windows. Distinguish automated results from agreed NTFS/exFAT/ADB and large-file tests; tool probes do not validate the application.
- [ ] **7.2 Operator instructions and first batch.** Document setup, initialization, progress/resume, recovery, conflict handling and attribute limits. Prepare a small production trial for explicit operator authorization; report completed checks and remaining limitations. Full archive audits remain separate local operations.

## Deferred features

- Interactive configuration wizard: [design/configurator.md](design/configurator.md), after core operations work.
- **Configurable sorting rules** (deferred): allow collections to select reusable, named sorting rules instead of requiring the current source-specific layouts. Keep `year_month` for phone collections and `camera_day` for camera collections as current defaults; adding the feature must not reorganize existing files automatically. Define rule inputs, destinations and migration behavior in a separate design before implementation.
- Additional media-type filters; details require a separate design.
- Explicit archive-destination changes: verified migration or a new destination history with a deliberate cutoff, retaining previous files/history. Editing `.env` alone must not bypass v1 destination locking.
- Database reset. Restoring a consistent state snapshot is the v1 recovery path.

Connecting media does not trigger backup. Automatic source deletion, global content-based deletion and automatic reorganization of existing archives are outside the planned workflow.
