# Technical documentation

This directory contains technical documentation for developers and AI agents. Start here before reading application source. The implementation reference describes **implemented behavior**; the roadmap and `design/` pages are explicitly marked plans. All pages are public: examples must contain synthetic paths and identifiers only.

Current coverage includes the package and CLI skeleton, configuration, SQLite lifecycle and persistent registration. Archive operations remain planned.

## Reading order

| Document | Read it for |
| --- | --- |
| [architecture.md](architecture.md) | Components, execution flow, ownership and implementation boundaries |
| [configuration.md](configuration.md) | Model fields, `.env` schema, validation, public API and compatibility |
| [identities.md](identities.md) | Stable IDs, display labels and the in-memory identity guard |
| [state.md](state.md) | Explicit SQLite creation/opening, transactions, migration API, errors and limits |
| [registration.md](registration.md) | Persistent identities, registered destinations, device changes and approval API |
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
| CLI | Help, `--config`, `--check-config`, exit codes 0/2 |
| Configuration | Literal `.env` parsing, immutable models, references, collection trees and lexical storage validation |
| Collections and policies | Configuration and pure lookup methods; no scanning or file mutations |
| Sources/storage | Phone/camera/card and disk descriptions; no discovery or hardware verification |
| Stable identity guard | In-memory key checks, with trusted keys loaded from SQLite by the registry API |
| SQLite | Explicit creation/opening, schema validation, transactions and migrations through a Python API |
| Persistent registration | Explicit Python API for identities, bindings, archive destination reservations and hardware changes |
| Media history and operation journal | Planned, including initialization state and long-running operation locks |
| IMPORT, SORT, BACKUP, AUDIT | Not implemented |
| Backup menu/month availability | Not implemented; configuration records intended scope and filtering mode |
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
