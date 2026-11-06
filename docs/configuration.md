# Configuration reference

This document describes current code contracts. [`.env.examples`](../.env.examples) is the complete public template; [architecture.md](architecture.md) explains the loading pipeline.

## Public API

```python
from media_archive.config import ConfigError, Configuration, load_config, parse_env

# Reads exactly one file; returns a fully validated configuration graph.
config: Configuration = load_config("/absolute/path/to/settings.env")

# Parses assignments only; does not require a complete configuration graph.
values: dict[str, str] = parse_env("APP_TIMEZONE='Europe/Warsaw'\n")
```

`load_config(path: str | Path = ".env") -> Configuration` and `parse_env(text: str) -> dict[str, str]` raise `ConfigError` for configuration failures. Parsing alone does not validate required fields, types or relationships. No process-environment override, recursive loading, interpolation or shell execution is performed. `APP_CONFIG_FILE` is metadata; only the argument to `load_config()` selects the file.

The package also exports `ConfigModel`, `AppSettings`, `LocalSettings`, `LocalVolume`, `PhoneDevice`, `PhoneProfile`, `CameraCard`, `CameraProfile`, `ArchiveTarget`, `ArchiveGroup`, `LocalCollection`, `BackupPolicy` and `BackupProfile`. The separate [identity API](identities.md) exports `ConfigurationIdentities`, `configuration_identities()` and `validate_registered_identities()`. Treat helpers in `registry.py` and `builders.py` as implementation details.

## Parsing and identifiers

`env.parse_env()` uses `dotenv.parser.parse_stream`, preserving syntax errors and duplicate assignments before a dictionary is built. It accepts python-dotenv comments, quotes, escape handling and optional `export`. It rejects unknown keys, malformed assignments, missing `=`, duplicate keys and parsed values with characters below code point 32 other than tabs. Multiline values therefore fail. Error messages include a line number or recognized setting name, not its value.

`.env` parameter keys are uppercase. Registry-list entries and ID references accept either case. `entry_names()` trims comma-separated list items and normalizes them to uppercase internally; models expose lowercase registry IDs. IDs use `[a-z][a-z0-9_]*`. An explicit list preserves its order and rejects duplicates or settings for unlisted entries. If a list is absent, names are inferred from entry keys and sorted. An explicitly empty list cannot coexist with settings for its entries.

Archive groups are discovered from `ARCHIVE_GROUPS` or group-level `ROOT_RELATIVE`/`TARGETS` settings. Target settings alone do not introduce a group. Names such as `extra_target_group` and target names containing `_TARGET_` are valid: the archive-target regex checks syntax only, while the registry assigns each key using full known-group prefixes and any explicit target lists. If no group/target pair matches, loading fails; if several pairs remain possible, loading fails with an ambiguity error instead of choosing a prefix arbitrarily. For example, `ARCHIVE_EXTRA_TARGET_GROUP_TARGET_B_UUID` could mean group `extra` / target `group_target_b` or group `extra_target_group` / target `b`. Explicit lists can select one interpretation; if both are registered, rename an entry to avoid the colliding key. Target-name inference is scoped to the resolved group, preserving explicit order or sorting inferred names.

`ALIAS` identifies a persistent import profile; it defaults to its registry name in lowercase and is not itself a `RegistryId`. Aliases and device identifiers retain supplied spelling. `LABEL` is editable display text: it defaults to the alias for sources and to the lowercase registry name for collections and backup profiles. Changing a label never changes identity; duplicate labels are allowed, blank labels are not. Unknown registered IDs require explicit registration under the [identity contract](identities.md); persistent enforcement belongs to SQLite task 1.3.

List/field normalization is performed while loading. Lookup methods described below use **exact string equality**, so pass canonical lowercase registry IDs and exact configured aliases. They do not normalize query arguments.

## Model and `.env` field reference

All DTOs derive from `ConfigModel`: `frozen=True`, unknown model fields forbidden, defaults validated, arbitrary types such as `ZoneInfo` allowed, input values hidden in validation-error text. Collections are stored as tuples. `repr()` and `str()` omit model values. Explicit field access and `model_dump()` still expose values and are not safe public diagnostics.

### Application and local storage

| Model field | `.env` setting | Type/requirement |
| --- | --- | --- |
| `AppSettings.timezone` | `APP_TIMEZONE` | Valid available `ZoneInfo` name |
| `declared_config_file` | `APP_CONFIG_FILE` | Nonblank path metadata, relative or absolute; model input alias is `config_file` |
| `database_file` | `APP_DATABASE_FILE` | Absolute path |
| `reports_dir` | `APP_REPORTS_DIR` | Absolute path |
| `min_free_bytes` | `APP_MIN_FREE_BYTES` | Decimal integer >= 1073741824 |
| `report_keep_runs` | `APP_REPORT_KEEP_RUNS` | Positive decimal integer |
| `database_keep_generations` | `APP_DATABASE_KEEP_GENERATIONS` | Positive decimal integer |
| `archive_state_dir` | `APP_ARCHIVE_STATE_DIR` | One relative folder name |
| `LocalSettings.staging_root` | `LOCAL_STAGING_ROOT` | Absolute path |
| `LocalSettings.undated_root` | `LOCAL_UNDATED_ROOT` | Absolute path |
| `LocalSettings.volumes` | `LOCAL_VOLUMES` | Nonempty tuple of `LocalVolume` |
| `LocalVolume.alias` | Entry name from `LOCAL_VOLUMES` | Lowercase registry name |
| `LocalVolume.root` | `LOCAL_VOLUME_<ID>_ROOT` | Absolute path |
| `LocalVolume.uuid` | `LOCAL_VOLUME_<ID>_UUID` | Nonblank identifier |

All application fields and both local working directories are required. Retention numbers are configuration values; no retention or snapshot execution exists yet. `declared_config_file` uses a validation alias, so a plain `model_dump()` dictionary needs its key renamed to `config_file` when rebuilding `AppSettings`.

### Policies and local collections

| Model field | `.env` suffix | Type/default |
| --- | --- | --- |
| `BackupPolicy.id` | Entry in `POLICIES` | Registry ID |
| `changes` | `POLICY_<ID>_CHANGES` | Required: `keep_both` or `replace_verified` |
| `deletions` | `POLICY_<ID>_DELETIONS` | Required: `keep` or `confirm_each_run` |
| `LocalCollection.id` | Entry in `COLLECTIONS` | Registry ID |
| `label` | `COLLECTION_<ID>_LABEL` | Nonblank; defaults to lowercase entry name |
| `local_root` | `COLLECTION_<ID>_LOCAL_ROOT` | Required absolute path |
| `archive_relative` | `COLLECTION_<ID>_ARCHIVE_RELATIVE` | Required relative path below archive-group root |
| `policy_id` | `COLLECTION_<ID>_POLICY` | Required policy reference |
| `parent_id` | `COLLECTION_<ID>_PARENT` | Optional collection reference; empty/absent means `None` |
| `layout` | `COLLECTION_<ID>_LAYOUT` | `plain` (default), `year_month`, `camera_day` |
| `allow_delete` | `COLLECTION_<ID>_ALLOW_DELETE` | Pydantic boolean; default `False`; prefer `true`/`false` in `.env` |

Policy settings describe future execution: `keep_both` preserves conflicting contents; `replace_verified` allows replacement after verification; `keep` retains locally missing archive files; `confirm_each_run` requires an exact deletion-list approval. No policy executes file operations today. `allow_delete` makes a specific collection eligible and never approves a list or grants inherited deletion eligibility.

### Import sources

Phone entries come from `PHONE_PROFILES` and use prefix `PHONE_<ID>_`:

| Suffix | Model field | Type/default |
| --- | --- | --- |
| `ALIAS` | `PhoneProfile.alias` | Identifier; defaults to lowercase registry name |
| `LABEL` | `PhoneProfile.label` | Nonblank display string; defaults to alias |
| `COLLECTION` | `collection_id` | Required collection reference |
| `INITIAL_POLICY` | `initial_policy` | `import_missing` (default) or `baseline_skip_missing` |
| `TRANSPORT` | `PhoneDevice.transport` | `adb` |
| `SERIAL` | `PhoneDevice.serial` | Device identifier |
| `CAMERA_DIR` | `PhoneDevice.camera_dir` | Absolute device path |
| `MODEL`, `ANDROID_VERSION`, `BUILD` | Device metadata fields | Optional strings, default `""` |

All transport/serial/camera fields empty or absent creates `device=None` and `enabled=False`. Any nonempty connection field requires a complete valid `PhoneDevice`. Inactive profiles still require a valid collection, layout and initialization policy. Initialization policies are settings only; no initialization status is persisted yet. The source directory is configured, not inferred from alias `main`.

Camera entries come from `CAMERA_PROFILES`, with prefix `CAMERA_<ID>_`:

| Suffix | `CameraProfile` field | Type/default |
| --- | --- | --- |
| `ALIAS` | `alias` | Identifier; defaults to lowercase registry name |
| `LABEL` | `label` | Nonblank display string; defaults to alias |
| `COLLECTION` | `collection_id` | Required collection reference |
| `MODEL` | `model` | Required nonblank string |
| `CAMERA_FOLDER` | `camera_folder` | Required single relative folder name |
| `SOURCE_RELATIVE` | `source_relative` | Required relative source path |
| `SOURCE_EXAMPLE_SUBDIR` | `source_example_subdir` | Optional relative path; empty/absent becomes `None` |
| `IGNORE_EXTENSIONS` | `ignore_extensions` | Comma-separated extensions, trimmed and uppercased; default empty tuple |
| `CARDS` | `cards` | Tuple of registered `CameraCard` objects selected by name; default empty tuple |

`CAMERA_CARDS` registers card names; `CAMERA_CARD_<ID>_UUID` sets each card's current UUID. The resulting `CameraCard` has a lowercase `alias` and optional `uuid`. Missing/empty UUID means unknown identity, not automatic trust. Camera profiles may have no cards during configuration; an assigned card must exist and cannot be assigned more than once.

### Archives and backup profiles

| Model field | `.env` setting | Type/default |
| --- | --- | --- |
| `ArchiveGroup.id` | Entry in `ARCHIVE_GROUPS` | Registry ID |
| `root_relative` | `ARCHIVE_<ID>_ROOT_RELATIVE` | Required relative audit/data root |
| `targets` | `ARCHIVE_<ID>_TARGETS` | Nonempty tuple of `ArchiveTarget` |
| `ArchiveTarget.alias` | Target list entry | Lowercase registry name |
| `mount` | `ARCHIVE_<ID>_TARGET_<TARGET>_MOUNT` | Required absolute path |
| `uuid` | `ARCHIVE_<ID>_TARGET_<TARGET>_UUID` | Required identifier |
| `state` | `ARCHIVE_<ID>_TARGET_<TARGET>_STATE` | `active` (default) or `read_only` |
| `BackupProfile.id` | Entry in `BACKUP_PROFILES` | Registry ID |
| `label` | `BACKUP_<ID>_LABEL` | Nonblank; defaults to lowercase entry name |
| `archive_group_id` | `BACKUP_<ID>_ARCHIVE_GROUP` | Required archive-group reference |
| `collection_ids` | `BACKUP_<ID>_COLLECTIONS` | Nonempty tuple of root collection references |
| `month_filter` | `BACKUP_<ID>_MONTH_FILTER` | Only `pending_selected_targets`, also the default |

`read_only` represents exclusion from future media and snapshot writes. `pending_selected_targets` records future month-menu behavior, not a current disk check. Lists are extensible; no fixed pair or fixed number of profiles is required by configuration. Archive groups and backup profiles may be absent, and a local collection may be unassigned while destinations are undecided.

## Field validation

[field_types.py](../src/media_archive/config/field_types.py) centralizes these constraints:

- Absolute data paths cannot be `/`, relative, or contain `..`. On Linux, a leading `//` is normalized to `/` before validation and storage-boundary comparisons. Paths distinguish uppercase and lowercase characters, including when checking collection-root uniqueness. No filesystem existence is required. `Path` performs lexical normalization without resolving symlinks.
- Relative paths cannot be absolute, empty/`.` or contain `..` or backslashes. `PurePosixPath` normalizes separators and `.` components. A single-folder value adds a one-component constraint.
- Identifiers contain no whitespace or slashes/backslashes; values beginning `REPLACE_WITH_` are rejected. Registry IDs use the stricter registry-name grammar.
- Counts/reserve accept unambiguous decimal strings or Python integers; fractional/exponent forms and booleans are rejected. Counts are positive and reserve is at least 1 GiB.
- Timezones must be available to `zoneinfo`. Extensions contain uppercase letters/digits after normalization; an empty whole list is valid, but an empty item inside a nonempty list is not.

## Relationship validation

`validate_relationships()` runs these groups in order:

| Function | Invariants |
| --- | --- |
| `_registries` | Unique IDs per registry; at least one policy, collection and local volume; unique local volume aliases |
| `_collections` | Existing policies, unique local roots, membership in a registered local volume, exact nearest-parent declaration, preserved parent/child archive suffix, explicit deletion eligibility |
| `_backups` | Existing groups/collections, nonempty profile scopes, root collections only, each root assigned at most once across profiles, nonempty archive targets, unique target aliases per group, no overlapping assigned archive scopes within a group, archive state folder outside audit roots |
| `_sources` | Unique aliases within each source/card registry, unique active phone serials, exclusive card assignment, existing collection references, raw-media policy `keep_both` + `keep`, phone layout `year_month`, camera layout `camera_day` |
| `_storage` | Case-insensitively unique known UUIDs across local/archive/card entries; disjoint volume/mount roots; staging and undated outside collections/archives and each other; configuration/database/reports mutually disjoint and outside data volumes/staging/undated |

The layout restrictions above are current implementation constraints: arbitrary named sorting presets are not implemented. UUID/alias uniqueness checks use case-folding; path comparisons are lexical and case-sensitive, not an emulation of NTFS semantics.

Nested collections must declare their nearest containing collection even when their policy is the same. If local child suffix is `All/Secondary`, its archive destination must be `parent.archive_relative / All/Secondary`. This preserves one mapping per path. Moving an entire local tree requires updating its configured child roots consistently; loading never moves files.

## Lookup API on `Configuration`

The aggregate contains `source_file`, `app`, `local`, `phones`, `cameras`, `camera_cards`, `archive_groups`, `policies`, `collections` and `backup_profiles`.

| Method | Returns / contract |
| --- | --- |
| `phone(alias)` / `camera(alias)` | Source profile by exact alias |
| `collection(id)` / `policy(id)` / `backup_profile(id)` / `archive_group(id)` | Corresponding DTO by canonical ID |
| `ArchiveGroup.target(alias)` | Target by exact alias within that group |
| `profile_collections(profile_id)` | Root collection DTOs, in configured profile order; does not expand children |
| `month_collections(collection_id)` | Tuple of that collection and descendants whose layout is not `plain`, in registry order |
| `collection_for_path(collection_id, path: Path)` | Deepest configured collection within the selected scope containing the path |
| `policy_for_path(collection_id, path: Path)` | That nearest collection's policy DTO |
| `archive_mapping(collection_id, group_id)` | Normalized string `group.root_relative / collection.archive_relative` |

Missing IDs/aliases raise `KeyError`. Path lookup requires an absolute `Path` within the selected collection with no `..`; otherwise it raises `ValueError`. All methods operate in memory. `archive_mapping()` combines the requested objects; it does not verify backup-profile membership or persist a destination lock.

Example using the public template's IDs after loading an appropriately completed configuration:

```python
from pathlib import Path

phone = config.phone("main")
destination = config.collection(phone.collection_id).local_root
roots = config.profile_collections("data")
month_roots = config.month_collections("photos")

review_path = config.collection("review").local_root / Path("example.jpg")
policy = config.policy_for_path("photos", review_path)
mapping = config.archive_mapping("photos_main", "data")
```

`month_collections()` does not enumerate calendar months, check files/targets, filter completed months or deduplicate scanning of nested month-capable roots. `collection_for_path()` selects a policy boundary; it does not scan, hash, infer an EXIF date or authorize mutations. Actual transfer planning belongs to a later subsystem.

## Supported configuration format

Only the registry-based format in `.env.examples` is supported. Loading always builds registry models and validates their relationships; there is no schema detection, compatibility adapter or automatic migration. Collections own paths and source profiles reference collection IDs. Category-based keys such as `LOCAL_PHOTOS_ROOT`, `PHONE_MAIN_DESTINATION_ROOT`, `PHOTO_*`, `DATA_ARCHIVE_*` and global camera settings are unknown parameters and raise a safe `ConfigError`. An incomplete registry configuration fails validation rather than falling back to another format.

Tests cover registry parsing, required values, relationships, identity, privacy, read-only loading and rejection of obsolete keys. Existing configuration files must be manually expressed in the supported schema before use; loading never rewrites files.

## Camera naming and API changes

Camera configuration uses `CameraProfile`, `CameraCard`, `Configuration.cameras`, `Configuration.camera_cards` and `camera(alias)`. The registration DTO uses the `cameras` namespace. Settings use `CAMERA_PROFILES`, `CAMERA_CARDS`, `CAMERA_<ID>_*` and `CAMERA_CARD_<ID>_UUID`. The camera layout is `camera_day`, with unchanged day-layout semantics and source validation requirements; this naming does not add device support or arbitrary sorting rules.

Update Python callers, serialized configuration/registration DTO field names, setting prefixes and layout values to these names. The former brand-specific API, `GOPRO_*` settings and `gopro_day` layout are not supported. There is no compatibility loader or automatic migration. Do not rename aliases, collection IDs, UUIDs or paths: the example collection ID `gopro`, its `COLLECTION_GOPRO_*` settings and source folder `100GOPRO` remain valid data. Tests cover the camera API, unchanged collection references/archive mappings/card identities, and rejection of obsolete settings even alongside valid camera settings.

## Errors and current limits

`builders.build()` wraps Pydantic validation with `ConfigError` and hides the original cause from displayed diagnostics. It maps internal names such as `collection_id` to `.env` suffixes such as `COLLECTION`; unknown error locations fall back to a safe prefix. `builders.fields()` selects exact setting names, preventing shared entry prefixes from mixing fields.

Direct model construction can raise Pydantic `ValidationError`; only the loader/builders provide the application-level error contract. `hide_input_in_errors` protects displayed error text, not every possible serialization of a validation exception.

No current validation proves that a mount exists, a UUID matches hardware, a symlink is safe, a file is readable, a disk has space, two verified copies exist, or a destination matches registered history. In particular, destination locking, initialization/history persistence and pending-month detection require later implementation.
