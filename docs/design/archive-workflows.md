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

### Fresh proof before manual local-space release

The operator accepts the additional read time: before indicating that a selected local scope can be released manually, independently read and SHA-256-verify both required archive copies of its current contents on two distinct connected physical disks.

- Verify the selected files/required locations, not the whole archive as an implicit full audit. Ordinary incremental backup may use history and current observations to avoid redundant transfers; that does not establish fresh release-readiness evidence.
- A new copy already independently read and verified after transfer during the same run satisfies the requirement. Do not read it immediately a second time merely to issue the indication. Older existing copies require a fresh full read in that run.
- Match each result to expected current content, disk identity and required path. Invalidate affected evidence if a change or instability is detected after verification. File presence, size/mtime agreement or a previous run's hash is insufficient.
- An offline second disk, incomplete read, hash mismatch, changed file or unresolved device identity blocks readiness for affected material. Preserve historical evidence, but display it separately rather than presenting it as a current verification.
- Reports distinguish previously verified content, current presence observations and full content verification in this run. The indication is scoped to the files actually verified and the run/time of that evidence; it is not a permanent database flag or a promise of future disk health.
- Read-only archive targets may supply fresh verification evidence because this check only reads them. A positive result never deletes local files automatically.

Required acceptance cases: two fresh matching reads allow readiness; a historical-only/offline copy does not; a corrupt existing copy blocks readiness despite unchanged size; a freshly verified new transfer is not redundantly rehashed; interruption cannot report unchecked files as ready.

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
| Imports and dispositions | Verified success, explicit baseline skip/rejection/reimport, reasons and scope |
| Local/archive instances | Content, required path, location, last-confirmed presence time and associated verification |
| Metadata and conflicts | Raw date fields, interpretation/manual decisions, variants and review results |
| Runs, operations and verifications | Plans, attempts, temporary files, progress, errors; verification time, run ID, file/disk/path and read scope, algorithm, expected/read hashes and result, separate from presence observations |
| Audits and migrations | Scope/findings/completeness and schema version changes |

Use foreign keys, uniqueness constraints and indexes to prevent duplicate history while preserving intentional occurrences. Exact DDL belongs to task 1.3 and later schema migrations as features are delivered.

The configuration layer implements a [pure identity guard](../identities.md). The state layer must load registered keys and enforce it before execution: unknown IDs require explicit registration, never automatic history creation or initialization restart. Labels remain freely editable. Removed entries retain history; intentional ID renames require a separate explicit design rather than inference from paths, labels or hardware.

### Destination registration

At first explicit registration, persist the normalized archive root plus collection-relative destination. Subsequent operations using a changed v1 mapping must fail before media writes. Reading `.env`, `--check-config` and dry-run never register or lock destinations.

Changing the local root with the same collection ID does not reset import history or move existing files. Updating the mount of the same disk or adding a disk with the same archive layout does not change the archive mapping. Historical copy evidence remains tied to its actual disk/path/content.

A separate post-v1 feature may offer verified destination migration or a new destination history with an explicit old-content cutoff. Preserve previous files and history; do not implement either mode by resetting global import memory or editing `.env` alone.

### Journal and crash reconciliation

Proposed attempt states:

```text
planned → transferring → transferred → verifying → verified → committed
```

Additional states may include `interrupted`, `failed`, `conflict` and `needs_review`. A transferred or merely hashed temporary file is not yet a published completed copy.

The filesystem and SQLite do not share a transaction. Record durable intent, transfer to a target-filesystem temporary file, verify, persist file data, publish using the applicable policy, persist directory metadata where supported, then commit evidence. Reconcile actual files and journal state after crashes between publication and database commit. Publication guarantees require filesystem-specific tests.

Use short database transactions and coordinated writes; do not keep a transaction open throughout a large transfer. Lock colliding operations over the same area. Copy status is per current content/required location, not one global run flag, and distinct registered volumes must be checked for physical independence.

## 7. Audit, reporting and recovery

AUDIT reads complete configured archive roots, including existing content outside currently assigned backup collections. Quick mode compares paths/types/sizes; full mode independently hashes file occurrences. Report only-on-one-side entries, content differences, naming conflicts, read errors and incomplete scopes. Resuming must invalidate evidence for changed files; an inventory is not an atomic snapshot.

Reports are Polish text and JSON, with run IDs, scope, target outcomes, counts, errors, conflicts and unverified files. Retain 20 completed report runs; preserve import history and required resume state independently. Final operational exit/resume contracts remain to be specified; currently only configuration/help statuses 0/2 exist.

Each backup creates a consistent SQLite/configuration snapshot after recording its results, saved under `.media-archive` on connected registered writable archives outside data/audit roots. Keep five validated generations per disk, deleting older generations only after a new valid snapshot exists. Use an active-database-safe snapshot mechanism rather than copying only the main database file during writes. Snapshot failure makes the backup result incomplete.

Restore explicitly with operations stopped; validate the generation, restore state/configuration and reconcile changes made to media afterward. Restore must not modify media or fabricate missing history. Lost history of content absent from all media cannot be reconstructed by hashing existing files. Database reset is outside v1.

## 8. Required failure checks and limits

Before ordinary use, verify interruptions, changed sources, unplugged/replaced disks, hash mismatches, insufficient space, safe replacement, fresh deletion approval, publication/database crash windows and corrupted snapshots. Use synthetic fixtures first; test real filesystems and transports only on agreed samples. Record unperformed checks explicitly.

SHA-256 verification establishes the bytes read at verification time, not permanent disk health. A short filename hash is not identity evidence. Preserving bytes/embedded metadata is separate from preserving filesystem ownership, ACLs or all timestamps; NTFS/exFAT/ADB capabilities need documented tests. Large audits require local progress/resume rather than unattended agent-driven multi-terabyte hashing.

Future operational documentation must replace proposals with tested contracts as each subsystem is implemented, keeping this design clearly separate from the implementation reference.
