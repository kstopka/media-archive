# Archive workflows and state — planned design

**Design only.** This document extracts general requirements and architecture proposals for future media operations. Current package/configuration behavior is documented in [architecture.md](../architecture.md) and [configuration.md](../configuration.md). SQLite, device adapters, transfers, audits and recovery described here are **not implemented**.

The [roadmap](../roadmap.md) defines delivery order. Requirements below describe intended behavior; proposed module/table names and protocols remain implementation designs, not existing APIs. Examples are neutral and do not describe a particular installation.

## 1. Core invariants

- IMPORT, SORT, BACKUP and AUDIT are separate, explicitly invoked operations. Connecting a device never starts backup.
- Preserve originals on source phones/cards. SORT may remove only its own verified staging copy after safe publication into a local collection.
- Use full SHA-256 plus size to identify content. Names, dates, sizes alone, mount paths and card UUIDs do not establish content identity.
- Distinct required paths may intentionally contain identical bytes. Do not globally deduplicate them by deletion or suppress required occurrences.
- A backup confirmation belongs to current content on a specific disk at a required path. Import history or a historical database flag alone does not prove that copy exists now.
- Missing storage and failed/incomplete scans are unknown states, never empty sources or permission to clean an archive.
- Only two independent copies of current content fully read and SHA-256-verified in the current readiness-check run permit indicating readiness for manual local-space release. Historical evidence alone is insufficient. Never delete local media automatically to reclaim space.

## 2. Proposed subsystem boundaries

The existing `config` package and CLI skeleton remain documented separately. Future responsibilities should be split into focused modules; exact package names can be finalized per task.

| Proposed subsystem | Responsibility |
| --- | --- |
| Devices | Discover system-mounted volumes, verify roles/identity, mounts and permissions |
| Sources | Local and ADB listing/reading adapters with explicit capabilities |
| Identity / verification | Content recognition, stable-read checks and independent hash evidence |
| Metadata | Read ExifTool results, preserve raw fields and interpret dates |
| Planner | Build operation manifests, path conflicts and whole-batch capacity plans |
| Transfer | Temporary copies, transport, safe publication and retry handling |
| State | SQLite migrations, history, journal, registration and crash reconciliation |
| Workflows | Compose the four independent operations without embedding rules in CLI presentation |
| Audit | Read-only archive inventories and comparisons |
| Reports | Polish text/JSON summaries, errors, partial results and resume references |

ADB is the intended Android transport; rsync is preferred for ordinary local paths. Adapters must declare whether source hashing, attributes and byte-level resume are available. Do not assume `adb pull` can resume from an arbitrary offset.

## 3. Initialization and import history

Inventory existing local collections and all required registered archives, retaining progress as disks are attached sequentially. Record observations and locations rather than fabricating successful import events for existing files. Quick observations remain unverified until content has been read.

Phone initialization is per persistent profile:

- `import_missing`: fill initial gaps from the configured source.
- `baseline_skip_missing`: establish a complete comparison boundary and mark baseline phone-only content as deliberately skipped. Such content is neither downloaded nor reported as archived.

Both modes subsequently use incremental history. Incomplete inventory cannot justify a skip or automatic download of unresolved baseline material. Camera profiles do not inherit the curated-phone exception. Replacing a phone or formatting a source card preserves previous history while registering the new device/filesystem association.

Later imports list the configured source, hash stable content, compare profile-specific history/dispositions, preflight staging space, transfer and independently verify the copy before recording success. Local selection/deletion must not erase the import record. Reimport requires an explicit choice rather than a silent history reset.

Track verified import completion, current verified staging presence and successful placement in the collection separately. An interrupted SORT leaves work pending against the existing staging copy. If that copy disappears before placement, report missing pending material and offer explicit reimport while preserving the previous import event. Acceptance must cover interruption and manual staging removal between IMPORT and SORT.

## 4. Sorting, dates and conflicts

SORT handles verified staging content only. Read embedded metadata without changing the original bytes:

- Photos: use `DateTimeOriginal` as the recorded capture date.
- Videos: interpret the container creation date as UTC and convert to the configured local timezone, for example `Europe/Warsaw`. Do not reconstruct travel timezones.
- Missing/invalid dates: keep material in the unresolved-date location, report it and accept an explicit date stored in state before retrying. No silent filename, mtime or import-time fallback.

Organize new imports using the collection's configured year/month or camera day layout. Keep video segments separate and photos with the corresponding recording-day media. Existing manually organized event folders remain untouched.

On one filesystem, publish without overwriting unrelated content. Across filesystems, copy, independently verify and only then remove the owned staging copy. Reconcile both locations after interruption before deleting anything.

Equal content at the required path may reuse the existing verified instance. Different media content with the same name must preserve both variants, adding a hash suffix and a durable conflict group. Suffix length/collision extension is an implementation detail; the full hash remains the identity. Repeated SORT runs must be idempotent. Conflict review records decisions such as “keep both” without deleting files implicitly.

## 5. Backup scope and policies

After the operator enters BACKUP, identify/select connected registered targets before computing available month choices. Propose matching backup profiles; require selection when several match. Shared disks/mounts do not automatically assign collections to a profile.

- Profile **All** includes every assigned root collection.
- Collection **All** includes its complete tree, including helper files and policy-controlled child folders.
- A month includes the exact configured monthly roots across included phone collections, or camera day directories for that month. It does not search the whole disk by EXIF.
- Plain collections have no month submenu.
- Offer months newest first only when transfers are pending for selected connected targets. A missing second disk can make a month reappear when it is later selected; incomplete scans must remain visible as problems.

Build one coherent plan for the whole selected batch. Resolve the closest explicit child policy per path; do not process a child twice through parent and child scopes. A month changes scope, never policy.

| Policy role | Changed content | Local deletion |
| --- | --- | --- |
| Originals and permanent selections | Preserve both variants | Keep archive copy |
| Mutable documents | Verified replacement without version history | Keep archive copy |
| Eligible selection-stage subtrees | Verified replacement | Exact list approval each run |
| Eligible print-backlog subtree | Preserve variants; no automatic replacement | Exact list approval each run |

These are configurable roles, not category-name checks. Existing cleanup permission is limited to the explicitly authorized selection-stage/backlog scopes. New collections must not inherit extra deletion authorization. Cleanup requires a complete fresh source scan, per-target exact list approval and state revalidation after transfer verification. Declining cleanup does not invalidate successful copies. Never run a blanket mirror/delete over a collection or archive root.

### Capacity and target independence

Before copying or cleanup, calculate space for all collections in the batch on every selected available target, including temporary copies, replacements, state snapshots and the configurable reserve of at least 1 GiB. A deficit or unreliable calculation on one selected target blocks the entire batch. A full disk is not an offline disk; do not switch targets automatically.

Allow one target and a partial-copy status; a later run fills the missing second copy. With two selected targets, use at most one transfer per target and two concurrently. One target failure can allow the other to finish. Exception: space exhaustion discovered mid-run stops further copying to both. Preserve already verified copies rather than rolling them back.

Read-only targets remain available to inventory/audit but receive no media writes, replacements, deletions or state snapshots. Extending an archive retains old disk identities; adding a disk does not migrate existing data.

### Successive archive pairs

Persist the required target pair for each material occurrence and its required archive path. When an existing pair becomes full, the operator explicitly switches the pair used for newly assigned material. Previously assigned material retains its original pair, including pending second copies. Adding or selecting a new pair alone must preserve these assignments. Readiness checks and missing-copy planning use the material's recorded pair. For example, earlier material assigned to A/B stays on A/B after new material starts using C/D. Transfer to a different pair requires a separate explicit migration design.

### Fresh proof before manual local-space release

The operator accepts the additional read time: before indicating that a selected local scope can be released manually, independently read and SHA-256-verify both required archive copies of its current contents on two distinct connected physical disks.

- Verify the selected files/required locations, not the whole archive as an implicit full audit. Ordinary incremental backup may use history and current observations to avoid redundant transfers; that does not establish fresh release-readiness evidence.
- A new copy already independently read and verified after transfer during the same run satisfies the requirement. Do not read it immediately a second time merely to issue the indication. Older existing copies require a fresh full read in that run.
- Match each result to expected current content, disk identity and required path. Invalidate affected evidence if a change or instability is detected after verification. File presence, size/mtime agreement or a previous run's hash is insufficient.
- An offline second disk, incomplete read, hash mismatch, changed file or unresolved device identity blocks readiness for affected material. Preserve historical evidence, but display it separately rather than presenting it as a current verification.
- Reports distinguish previously verified content, current presence observations and full content verification in this run. The indication is scoped to the files actually verified and the run/time of that evidence; it is not a permanent database flag or a promise of future disk health.
- Read-only archive targets may supply fresh verification evidence because this check only reads them. A positive result never deletes local files automatically.

Required acceptance cases: two fresh matching reads allow readiness; a historical-only/offline copy does not; a corrupt existing copy blocks readiness despite unchanged size; a freshly verified new transfer is not redundantly rehashed; interruption cannot report unchecked files as ready.

Manual-space-release guidance follows the effective backup policy at each selected path, including explicit child overrides. Paths with `deletions=keep` are eligible after the required fresh verification. Paths with `deletions=confirm_each_run` require a warning that subsequent backup may propose deleting their archive copies after local removal. Report these paths separately from files eligible for retained-archive space release. Catalog names and source profiles do not determine eligibility. Existing per-run exact deletion approval remains required.

## 6. Planned identity and SQLite model

Keep these identities separate:

| Identity | Meaning |
| --- | --- |
| Content | Full SHA-256 and size |
| Occurrence | Content at a particular source/local/archive path |
| Import profile | Stable alias associated with replaceable devices and a collection; editable display label is not identity |
| Collection | Stable local/archive policy and destination definition |
| Device | Logical phone, card or archive disk with observed hardware evidence |
| Volume episode | A filesystem identity observed on a device; formatting can change it |

UUIDs can change or be cloned; mount points are observations, not durable identities. Fast path/size/mtime observations can reduce work but cannot replace content verification when names are reused.

Proposed state groups, not finalized SQL tables:

| State group | Required information |
| --- | --- |
| Profiles and initializations | Device associations, collection reference, baseline boundary, policy and resumable completion |
| Collections and backup assignments | IDs, policies, local roots, registered archive mappings and history |
| Devices and volumes | Roles, filesystem episodes, identity evidence and last-known locations |
| Contents and observations | Full hash/size, source paths/attributes, observation time and certainty |
| Imports and dispositions | Verified import completion, staging presence, collection placement, explicit baseline skip/rejection/reimport, reasons and scope |
| Local/archive instances | Content, required path, assigned target pair, location, last-confirmed presence time and associated verification |
| Metadata and conflicts | Raw date fields, interpretation/manual decisions, variants and review results |
| Runs, operations and verifications | Plans, attempts, temporary files, progress, errors; verification time, run ID, file/disk/path and read scope, algorithm, expected/read hashes and result, separate from presence observations |
| Audits and migrations | Scope/findings/completeness and schema version changes |

Use foreign keys, uniqueness constraints and indexes to prevent duplicate history while preserving intentional occurrences. Exact DDL belongs to task 1.3 and later schema migrations as features are delivered.

Store shared profile, collection, device and volume definitions once and reference their IDs from history. Each import must retain its source profile, actual source device/volume episode where applicable, observed source path, content and destination occurrence through these references. Each archive copy must retain its actual volume episode and relative path. Preserve historical associations when current paths or devices change. Required target assignments also exist for copies still pending. Database-size estimates belong to the finalized schema and representative synthetic data.

The configuration layer implements a [pure identity guard](../identities.md). The state layer must load registered keys and enforce it before execution: unknown IDs require explicit registration, never automatic history creation or initialization restart. Labels remain freely editable. Removed entries retain history; intentional ID renames require a separate explicit design rather than inference from paths, labels or hardware.

### Destination registration

At first explicit registration, persist the collection ID, archive group ID and normalized archive root plus collection-relative destination, including effective mappings for child collections. Subsequent operations using a changed v1 mapping or archive-group association must fail before media writes. Reading `.env`, `--check-config` and dry-run never register or lock destinations.

Changing the local root with the same collection ID does not reset import history or move existing files. Updating the mount of the same disk or adding a disk with the same archive layout does not change the archive mapping. Historical copy evidence remains tied to its actual disk/path/content.

A collection may be registered while it has no backup assignment. Register its archive mapping explicitly when an archive group is first assigned, including inherited child mappings. Removing a current assignment must retain existing mapping locks and history so that removing and re-adding configuration cannot bypass them.

A separate post-v1 feature may offer verified destination migration or a new destination history with an explicit old-content cutoff. Preserve previous files and history; do not implement either mode by resetting global import memory or editing `.env` alone.

### Journal and crash reconciliation

Proposed attempt states:

```text
planned → transferring → transferred → verifying → verified → committed
```

Additional states may include `interrupted`, `failed`, `conflict` and `needs_review`. A transferred or merely hashed temporary file is not yet a published completed copy.

The filesystem and SQLite do not share a transaction. Record durable intent, transfer to a target-filesystem temporary file, verify, persist file data, publish using the applicable policy, persist directory metadata where supported, then commit evidence. Reconcile actual files and journal state after crashes between publication and database commit. Publication guarantees require filesystem-specific tests.

Only the final database commit records operation completion. A crash after publication leaves an unfinished journal entry even if the destination file exists. Recovery must validate the destination and reconcile evidence before recording completion, retaining attempt history and preventing duplicate success records. Task 1.3 tests persistent journal transitions, retries and completion constraints with synthetic evidence. Task 4.1 adds end-to-end crash tests around actual transfer, verification and publication using temporary files and directories.

Use short database transactions and coordinated writes; do not keep a transaction open throughout a large transfer. Lock colliding operations over the same area. Copy status is per current content/required location, not one global run flag, and distinct registered volumes must be checked for physical independence.

### Agreed requirements for task 1.3

These four requirements define the accepted state-layer behavior for implementation and review. Task 1.3 remains planned.

1. **Explicit device registration.** Persist stable device IDs and associations with source profiles and filesystem episodes. A replacement phone or changed UUID requires explicit registration of the association while preserving previous history. Keep actual source and destination identities and paths accessible from each operation's history through stable references.
2. **Deferred archive assignment and retained destination locks.** Allow a collection to be registered before its archive targets are selected. At its first explicit archive assignment, register the group and relative destination, including effective child mappings. Retain those locks after removing configuration entries. For example, removing and re-adding a collection cannot silently change its destination from `Archive/Films` to `Archive/OtherFilms`. Apply the same historical mapping checks on re-addition.
3. **Safe schema migrations and operation concurrency.** A migration updates database structure when application versions add or change tables. Apply each migration atomically so failure preserves the preceding valid schema and history. Protect conflicting file operations for their full lifetime, including transfer time between short database transactions. For example, a second BACKUP targeting the same destination must not race an unfinished BACKUP. Independent operations may proceed concurrently when their scopes permit it. Tests must cover migration failure, conflicting processes, interruption and safe recovery.
4. **Durable history and explicit initialization progress.** Report-file rotation preserves database history, verification evidence and required run references. For example, after a backup of 100 photos, removing its text/JSON reports outside the retention limit preserves the recorded copy outcomes and verification evidence. Registering a source leaves its initialization pending. Later initialization workflows record progress and mark completion only after the required comparison and processing have finished. Interrupted initialization retains progress for resumption. Manual selection or local removal preserves prior import history.

### Foundation acceptance for task 1.3

- Explicit creation establishes application identity and a supported schema version. Opening an unrelated, corrupt or newer unsupported database fails safely. Schema migration failure must preserve the previous valid schema and history, and normal reads and dry runs must not migrate it.
- Enforce foreign keys and uniqueness constraints on every applicable write connection. Retrying one operation must preserve its attempts and commit its result at most once. Distinct occurrences of equal content must remain distinct, including after local removal or verified replacement at a reused path.
- Concurrency tests must use separate database connections/processes and cover a conflicting operation and process termination. SQLite transaction locking alone does not protect a file transfer between short transactions. Recovery must establish that the prior owner has stopped before taking over unfinished work.
- Source initialization progress must survive interruption and changes to labels, configured initial policy or hardware. Completion and baseline decisions require evidence supplied by the later inventory/initialization workflows. Creating the database or registering a profile cannot mark initialization complete.
- Separate report retention from durable history and evidence. Removing a completed report must preserve run references needed by verifications, imports and recovery.

Detailed SQL tables, indexes and internal state APIs are implementation decisions within these contracts. Later features may add migrations for their own entities. Tests for this foundation use temporary state and synthetic records.

## 7. Audit, reporting and recovery

AUDIT reads complete configured archive roots, including existing content outside currently assigned backup collections. Quick mode compares paths/types/sizes; full mode independently hashes file occurrences. Report only-on-one-side entries, content differences, naming conflicts, read errors and incomplete scopes. Resuming must invalidate evidence for changed files; an inventory is not an atomic snapshot.

### Explicit copy repair

AUDIT reports damaged copies. A separately invoked repair action presents the affected content, a source verified against its expected SHA-256 and the exact destination for approval. A surviving archive copy can supply the source when local content has been removed. Publish a verified replacement through the safe transfer/journal mechanism and record new evidence. An unresolved expected hash or absence of a matching source blocks repair. Acceptance must cover one damaged archive copy with a valid surviving copy and no local original, interruption during replacement and absence of any valid source.

### Reports and state recovery

Reports are Polish text and JSON, with run IDs, scope, target outcomes, counts, errors, conflicts and unverified files. Retain 20 completed report runs; preserve import history and required resume state independently. Final operational exit/resume contracts remain to be specified; currently only configuration/help statuses 0/2 exist.

Each backup creates a consistent SQLite/configuration snapshot after recording its results, saved under `.media-archive` on connected registered writable archives outside data/audit roots. Keep five validated generations per disk, deleting older generations only after a new valid snapshot exists. Use an active-database-safe snapshot mechanism rather than copying only the main database file during writes. Snapshot failure makes the backup result incomplete.

Restore explicitly with operations stopped; validate the generation, restore state/configuration and reconcile changes made to media afterward. Restore must not modify media or fabricate missing history. Lost history of content absent from all media cannot be reconstructed by hashing existing files. Database reset is outside v1.

## 8. Required failure checks and limits

Before ordinary use, verify interruptions, changed sources, unplugged/replaced disks, hash mismatches, insufficient space, safe replacement, fresh deletion approval, publication/database crash windows and corrupted snapshots. Use synthetic fixtures first; test real filesystems and transports only on agreed samples. Record unperformed checks explicitly.

SHA-256 verification establishes the bytes read at verification time, not permanent disk health. A short filename hash is not identity evidence. Preserving bytes/embedded metadata is separate from preserving filesystem ownership, ACLs or all timestamps; NTFS/exFAT/ADB capabilities need documented tests. Large audits require local progress/resume rather than unattended agent-driven multi-terabyte hashing.

Future operational documentation must replace proposals with tested contracts as each subsystem is implemented, keeping this design clearly separate from the implementation reference.
