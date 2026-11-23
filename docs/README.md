# Technical documentation

This directory contains technical documentation for developers and AI agents. Start here before reading application source. The implementation reference describes **implemented behavior**; the roadmap and `design/` pages are explicitly marked plans. All pages are public: examples must contain synthetic paths and identifiers only.

Current coverage includes the package and CLI, configuration, SQLite lifecycle, persistent registration, media history, archive audit, and the initial inventory.

## Reading order

| Document | Read it for |
| --- | --- |
| [architecture.md](architecture.md) | Components, execution flow, ownership and implementation boundaries |
| [configuration.md](configuration.md) | Model fields, `.env` schema, validation, public API and compatibility |
| [identities.md](identities.md) | Stable IDs, display labels and the in-memory identity guard |
| [state.md](state.md) | Explicit SQLite creation/opening, transactions, migration API, errors and limits |
| [registration.md](registration.md) | Persistent identities, registered destinations, device changes and approval API |
| [devices.md](devices.md) | Linux mount checks, safe file paths and explicit device changes |
| [sources.md](sources.md) | Read-only local and ADB adapters, streaming SHA-256 and read stability checks |
| [audit.md](audit.md) | Quick comparisons, resumable SHA-256 reads and audit reports |
| [inventory.md](inventory.md) | First inventory of local collections and archive targets, and how to resume it |
| [phone-initialization.md](phone-initialization.md) | Capture a phone baseline, compare content and record initial decisions |
| [history.md](history.md) | Media occurrences, evidence, archive requirements and initialization |
| [journal.md](journal.md) | Operation attempts, resource locks and recovery |
| [transfer.md](transfer.md) | Verified one-file transfer, publication and recovery |
| [import-planning.md](import-planning.md) | Source manifests, profile decisions, reimport and staging preflight |
| [camera-import.md](camera-import.md) | Import a registered camera card to staging and inspect the results |
| [phone-import.md](phone-import.md) | Fill baseline gaps and import new content from a registered phone through ADB |
| [import-recovery.md](import-recovery.md) | Resume a journaled import, reimport missing material and reconcile restored state |
| [sort-planning.md](sort-planning.md) | Read capture dates, preview SORT paths and record a manual date |
| [sort.md](sort.md) | Publish verified placements, preserve conflicts and recover interrupted moves |
| [backup-append.md](backup-append.md) | Test append-only publication, disk-specific paths and recovery |
| [backup-planning.md](backup-planning.md) | Select a BACKUP profile and preview scope, targets and capacity |
| [catalog.md](catalog.md) | Search saved locations, reconcile manual moves and review durable conflicts |
| [cli.md](cli.md) | Setup, migrations, planning, journal previews and report retention |
| [snapshots.md](snapshots.md) | Manual state snapshots, validation and restore |
| [development.md](development.md) | Environment, tests, change locations and documentation maintenance |
| [Project README](../README.md) | Installation and user-facing configuration overview |
| [.env.examples](../.env.examples) | Complete example of the current registry-based configuration |

For a configuration change, read the architecture and relevant configuration sections, then open only the modules and tests involved. These documents describe contracts and navigation; inspect the affected source when modifying its implementation.

## Feature documentation

| Implemented feature | Technical reference |
| --- | --- |
| CLI help and configuration checks | [CLI contract](architecture.md#cli-execution) |
| Configuration, collections, policies | [Configuration reference](configuration.md) |
| Display names and identity checks | [Identity contract](identities.md) |
| SQLite lifecycle and schema migrations | [State foundation](state.md) |
| Persistent configuration registration | [Registry API](registration.md) |
| Media history and initialization | [History API](history.md) |
| Operation journal and locks | [Journal API](journal.md) |
| Shared transfer | [Transfer API](transfer.md) |
| Import planning | [Import manifest and preview](import-planning.md) |

As features grow, give each feature its own page and register it here. Before creating or editing a feature, read its technical reference and any linked AI-specific instructions. Shared architecture and development rules remain in their existing pages; create additional instruction files only when necessary.

## Roadmap and planned designs

| Document | Purpose |
| --- | --- |
| [roadmap.md](roadmap.md) | Public implementation stages, completion status and acceptance criteria |
| [design/archive-workflows.md](design/archive-workflows.md) | Planned operations, verification, state model, policies and recovery |
| [design/configurator.md](design/configurator.md) | Future setup flow for sources, local collections and backup profiles |

These documents use neutral examples and do not depend on private planning notes or reports. Design descriptions do not establish that a capability is implemented or authorize operations on real data. Update task status and implemented feature references as work is delivered.

## Implemented vs planned

| Area | Current implementation |
| --- | --- |
| CLI | Help, configuration checks, setup, migration, journal previews, import recovery and reports |
| Configuration | Literal `.env` parsing, immutable models, references, collection trees and lexical storage validation |
| Collections and policies | Configuration and pure lookup methods; no scanning or file mutations |
| Sources/storage | Linux volume checks and read-only local and ADB adapters |
| Stable identity guard | In-memory key checks, with trusted keys loaded from SQLite by the registry API |
| SQLite | Explicit creation/opening, schema validation, transactions and migrations through a Python API |
| Persistent registration | Explicit Python API for identities, bindings, archive destination reservations and hardware changes |
| Media history and initialization | Python APIs for recording evidence, archive requirements and resumable progress |
| Operation journal | Python API for durable attempts, recovery and process-held resource locks |
| Shared transfer | Python API for copying, verifying, publishing and recovering a planned file transfer |
| Import planning | CLI preview and Python API for source decisions, exact reimport and staging preflight |
| State snapshots and restore | CLI and Python APIs for manual generations, validation and restore |
| AUDIT | Quick metadata comparison and resumable full SHA-256 comparison for two selected archive targets |
| Initial inventory | Durable local collection and archive scopes, with optional local SHA-256 |
| Phone initialization | Fixed ADB baseline and initial decisions against saved local and archive evidence |
| Camera import | Verified import from registered cards to staging |
| Phone import | Verified ADB import to staging, including completion of pending baseline processing |
| SORT | Read-only preview, verified placement and cleanup, explicit date decisions and durable name conflicts |
| Catalog | Search saved media locations, verify manual moves and review name conflicts |
| BACKUP | Read-only [scope, target and capacity preview](backup-planning.md). Append-only transfer is available for isolated trials. Snapshot publication is planned. |
| Configuration wizard | Future enhancement; settings are currently edited in `.env` |

The expected future pipeline is source → staging → SORT → local collection → manually invoked BACKUP. This explains existing configuration relationships; it is not an executable workflow yet. Future behavior must be marked as such throughout these documents.

## Documentation maintenance contract

Update the relevant pages **in the same change as the code**:

1. Update model fields, `.env` keys, defaults, API contracts and validation rules in [configuration.md](configuration.md).
2. Update component responsibilities and implemented/planned boundaries in [architecture.md](architecture.md) and this index when capabilities change.
3. Update commands, fixtures and test navigation in [development.md](development.md).
4. Keep the root README and `.env.examples` consistent with user-visible changes.
5. For each new feature, add its own document with entry points, data flow, public API, side effects, errors, invariants, tests and limitations; link it here. Update the same document when editing that feature. Keep the root README brief, linking to feature documentation rather than duplicating it.

Describe the final implementation rather than conversation history. Link to source/tests instead of reproducing whole implementations. Never copy private `.env` values, actual device identifiers, local reports or media paths into these pages. Local planning notes, when present, remain separate from this public implementation reference.
