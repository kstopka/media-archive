# Stable identities and display labels

Import profiles use a stable `ALIAS` and a separate display `LABEL`. Collections and backup profiles already use stable registry IDs with separate labels. Changing what users see must not rename identity keys, disconnect history or restart initialization.

## Configuration contract

```dotenv
PHONE_MAIN_ALIAS='main'
PHONE_MAIN_LABEL='Travel phone'
PHONE_MAIN_COLLECTION='photos_main'
CAMERA_HERO8_ALIAS='hero8'
CAMERA_HERO8_LABEL='Travel camera'
```

In [models.py](../src/media_archive/config/models.py), `PhoneProfile.alias` and `CameraProfile.alias` remain persistent source identities. Their `label` fields are nonblank display strings and may contain spaces or duplicate another display name. Change `LABEL` to rename a source; preserve `ALIAS`. Collection IDs do not depend on labels.

The loader defaults omitted labels to the source alias. Explicitly blank labels fail validation. Named profiles accept PHONE_<NAME>_LABEL and CAMERA_<NAME>_LABEL. Direct DTO construction requires a label.

Registry entry names select setting prefixes. Source aliases select persistent import history; collection/group/backup-profile IDs select their own registries. `config.phone('main')` and `config.camera('hero8')` still look up exact aliases, not display labels. Existing source aliases are case-sensitive lookup keys; changing spelling/case is an identity change.

## In-memory registration guard

[identities.py](../src/media_archive/config/identities.py) exposes these functions through `media_archive.config`:

| API | Contract |
| --- | --- |
| `ConfigurationIdentities` | Immutable DTO with `phones`, `cameras`, `collections`, `backup_profiles` and `archive_groups` frozensets; fields default to empty sets |
| `configuration_identities(config)` | Return configured identity keys, excluding labels, paths, device identifiers and initialization-policy values; does not register anything |
| `validate_registered_identities(config, registered)` | Reject any current key absent from the supplied registration state, raising a safe `ConfigError` with namespace names only |

Source sets use aliases; the other sets use canonical registry IDs. Namespaces are distinct, so a phone and camera may have the same alias without sharing history. A supplied registration set may include removed/offline entries: the guard does not discard them or require them to be present in current configuration.

Usage at the future operation boundary:

```python
from media_archive.config import load_config, validate_registered_identities

candidate = load_config('/absolute/path/to/settings.env')
# registered must be loaded from trusted persisted registration state.
validate_registered_identities(candidate, registered)
# Only then may an operation continue, with all other runtime checks.
```

Never derive `registered` from the candidate configuration immediately before checking it; that would approve every new ID automatically. `configuration_identities()` describes candidate keys or accepted registrations for storage, not proof of prior registration. The guard compares key membership only: existing-ID reassignment, source-to-collection bindings, hardware trust, path registration and policy authorization require separate state/runtime checks.

## Required SQLite integration — planned for task 1.3

There is **no persistent identity registry yet**. The guard is implemented and tested with supplied in-memory registrations; `load_config()` and `--check-config` remain structural, read-only checks and do not invoke historical registration validation.

Task 1.3 must:

1. Persist stable identities and initialization/history associations at explicit registration. Store labels as editable descriptive fields, not keys.
2. Load trusted registrations and run the guard before operations can write media or initialize a source.
3. Treat unknown IDs as requiring explicit registration, never as automatic renames or a reason to restart initialization. A new profile and an accidental ID edit both require review; do not infer identity from a label, path or replacement device.
4. Preserve removed identities and their history. A source's completed initialization stays completed when its label, hardware, local path or configured initial policy changes.
5. Keep identity registration separate from immutable archive-destination mapping checks. Label edits do not change either key set or mapping.

An intentional ID rename/history reassignment needs a separately designed explicit operation. Editing `.env` alone must not perform it. The existing snapshots/restore design must preserve registrations and initialization state.

## Verification

`tests/test_config_identities.py` covers editable/default/blank labels, registration guards, device replacement and no-write behavior. Camera configuration tests also preserve collection IDs and archive mappings across technical naming changes.
