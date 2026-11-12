# Future configuration wizard

**Design only — not implemented.** This description can serve as the basis for a future configurator once the core application works. Today settings are edited in `.env`; current behavior is documented in [configuration.md](../configuration.md). The wizard does not change the separation of IMPORT, SORT, BACKUP and AUDIT.

## Concepts and example names

| Concept | Responsibility | Neutral example |
| --- | --- | --- |
| Import profile | Source device/settings and destination collection reference | Travel phone, Action camera |
| Local collection | Local root and its archive/policy definition | Phone media, Finished videos |
| Sorting rule | Organization of new verified imports | Year/month, Recording day |
| Backup policy | Changed-content and local-deletion behavior | Append, Documents, Selection |
| Archive group | Registered physical destination disks | Data archives |
| Backup profile | Root collections assigned to an archive group | Personal media, Video originals |

Display names are user-selected. Stable profile/collection IDs are distinct from device identifiers and mount paths. Replacing hardware must not erase source history. A collection can exist without an import source or while its destination disks are undecided.

For sources, the display name is `LABEL` and the stable identity is `ALIAS`. Renaming a displayed phone/camera edits only its label. Unknown IDs require explicit registration against persisted history; the wizard must not treat an ID edit as an automatic new profile or restart initialization. See the current [identity contract](../identities.md).

## 1. Add an import source

1. Select **Add source** and connect a phone or source card.
2. Review discovered sources and resolve ambiguity. Phones require an authorized ADB connection; cameras can use assigned cards.
3. Select the source and name its persistent import profile, for example **Travel phone**.
4. Associate the observed device identity with that profile. Future replacement hardware can retain the profile and history.
5. Select the source folder, for example `DCIM/Camera`. Additional media-type filters are a proposed extension, not a current general configuration feature.
6. For a phone, select the one-time initialization mode:
   - **Fill initial gaps** (`import_missing`): download missing material during initial import.
   - **Preserve a curated baseline** (`baseline_skip_missing`): after complete comparison with required local/archive inventories, mark baseline phone-only content as intentionally skipped. Do not download it or count it as archived.
7. Select an existing destination collection or create one using the next section.

The initialization choice is not “all files versus only new files.” Both modes subsequently use incremental import history. Camera sources do not inherit the special curated-phone baseline behavior. Changing an alias, device setting or configuration file must not silently restart initialization.

Card formatting can change filesystem UUIDs and reuse filenames. Keep the logical card/profile identity and explicitly register the new filesystem episode; identify content by full hashes. Unknown UUIDs do not authorize automatic source selection.

## 2. Configure local collections

For **Phone media**, choose a local root, a year/month sorting rule and an append policy. For **Documents**, choose a local root and verified-replacement policy, without an import source or date sorting. For **Finished videos**, choose a local root and append policy; files can be placed there manually.

Import profiles reference the collection ID. The local destination path is defined once in the collection, not duplicated in source or backup settings.

### Staging and sorting

```text
Source → IMPORT → staging → SORT → local collection
```

All imports go through staging, even when the sorting rule is chosen during source setup. IMPORT and SORT remain independently invoked operations. Staging can hold files from multiple sources; each import retains its destination collection association. Staging itself is outside normal backup collections.

Current configuration provides `plain`, `year_month` and `camera_day` layouts. Phone collections currently require `year_month`, and camera collections require `camera_day`; these are suitable defaults for current use, not a finished general-purpose sorting system. A deferred **configurable sorting rules** feature would let a collection select a reusable, named rule and later support other layouts. Its accepted inputs, destination mapping and compatibility behavior need a separate design before implementation. Introducing or changing a rule must not automatically reorganize existing files. Existing event folders remain untouched by SORT.

### Policy selection

| Intended role | Different content at the same name | Locally missing file |
| --- | --- | --- |
| Originals / final selections / finished media | Preserve both variants | Retain archive copy |
| Mutable documents | Replace only after verification | Retain archive copy |
| Eligible selection stages | Replace only after verification | Exact deletion list requires approval each run |
| Eligible print backlog | Preserve variants; no automatic replacement | Exact deletion list requires approval each run |

Policies are reusable and do not follow from folder names. Explicit child rules override their nearest parent; other folders inherit the enclosing policy. Required copies under distinct paths remain distinct, even with identical content.

The wizard must expose deletion eligibility explicitly. Selecting a policy does not approve a deletion list, inherit eligibility into a newly configured child rule, or silently expand the authorized cleanup scope. Missing sources or scan errors block deletion planning/execution.

## 3. Create a backup profile

1. Name the profile, for example **Personal media**.
2. Select or create an archive group.
3. Connect and select the destination disks; verify their roles/identities. Disks may be registered sequentially.
4. Choose the relative archive root on those disks, for example `Data`.
5. Assign root collections, for example **Photos** and **Documents**.
6. Choose their relative archive destinations and review the complete mapping:

```text
Photos    → selected archive disks → Data/Photos
Documents → selected archive disks → Data/Documents
```

Policies already belong to collections; they are not selected separately for each disk. Child rules are included through their root and must not appear as duplicate transfer scopes.

The [registry API](../registration.md) reserves destinations at the first explicit archive assignment, including child collections. The reservation survives removal from the current configuration. A changed mount of the same disk or an additional disk with the same archive layout is allowed. Archive-directory migration is deferred. The setup interface will invoke registration after the operator approves the candidate.

Retain old full disks as `read_only` and add new `active` targets instead of overwriting identities. Read-only targets cannot receive media changes, deletions or state snapshots. New entries do not migrate files or establish two verified copies by themselves.

### Later backup selection

```text
Personal media
├── All → all assigned root collections
├── Photos
│   ├── All → complete photo tree
│   └── Month → matching dated roots from included phone profiles
└── Documents → complete collection
```

Month choices are newest first and depend on pending work for selected connected targets. A month completed on target A reappears when missing target B is selected. A scan failure is not proof that no work remains.

Photo All includes helper files, selection stages and final selections. A month includes configured monthly roots, not arbitrary EXIF-matched files or unrelated selection folders. Camera month scopes contain configured day directories. Finished videos can use a separate archive group/profile; neighboring folders or shared mounts never assign membership automatically.

## 4. Review and initialize explicitly

Show source identities, stable profile/collection names, source/local/archive paths, sorting rules, policies and deletion eligibility together. Saving settings alone does not import, move or back up files.

First setup follows the [planned state lifecycle](../state.md#planned-first-setup-and-upgrades). After explicit confirmation, create `state.sqlite3` in the selected state directory and record the initial state. Later configuration reviews use the same database.

A separate action starts the existing-data inventory. Required local collections, archives and relevant phone baselines can be scanned with resumable progress while disks are connected sequentially. Until all required comparison scope is complete, curated-baseline decisions remain unresolved. Quick path/size observations do not prove content equality; full local SHA-256 audits provide content evidence.

The configuration-writing mechanism, edit/validation interface and handling of concurrent configuration edits require a separate implementation design. This document does not add an alternative configuration format or prescribe a new command syntax.

## 5. General settings

- Staging and unresolved-date locations, outside backed-up collection scopes.
- Database/configuration/report locations on the system disk.
- Configured timezone and existing metadata interpretation rules.
- Free-space reserve of at least 1 GiB. A deficit on any selected available target blocks the entire batch before copying.
- Retention: 20 completed report runs, excluding persistent import/resume history.
- Database/configuration snapshots on writable archives outside audit roots, keeping five validated generations.

Defaults can be offered but must be visible in the final summary. See [archive-workflows.md](archive-workflows.md) for the planned operational and recovery constraints.

## 6. Normal operation after setup

```text
IMPORT → staging → SORT → local collection
                               ↓
                       manual selection
                               ↓
                         manual BACKUP
```

Source originals remain on phones/cards. Persistent history prevents automatic re-download of previously imported and locally rejected material. Indicating readiness for manual local-space release requires full SHA-256 reads of both current copies on distinct connected disks in the same check run. A new copy already fully verified in that run need not be read twice; historical verification or an offline second disk is insufficient. This reads the selected material, not the entire archive. See [fresh-proof rules](archive-workflows.md#fresh-proof-before-manual-local-space-release).

Adding a new phone, a standalone local collection or new archive disks are independent setup paths. None requires restarting all configuration, resetting the database or automatically running an operation.
