# Persistent configuration registration

The state registry stores stable source, collection, backup profile, archive group and device identities in SQLite. It checks a loaded configuration against previously approved registrations. Use the Python API in `media_archive.state` with an existing database. Operator commands and physical device recognition belong to later stages.

## Entry points

| API | Behavior |
| --- | --- |
| `load_registered_identities(path)` | Read the trusted key sets used by the [identity guard](identities.md#in-memory-registration-guard), including entries removed from the current configuration. |
| `load_registered_devices(path)` | Read the latest registered device and volume episode for each role and alias. Returns immutable `RegisteredDevice` records with a `DeviceKey`, device ID, volume ID and configured identity evidence. An archive target's key includes its group ID. |
| `validate_registered_configuration(config)` | Read one snapshot of the database selected by `config.app.database_file`. Check key membership, registered bindings, archive destinations and device evidence. |
| `register_configuration(config, *, device_changes=())` | Record an explicitly approved configuration. Register new keys, update display labels, record first assignments and apply approved device changes in one SQLite transaction. |
| `DeviceChange(previous, action)` | Confirm a change for the previously reviewed `RegisteredDevice`. Use `"reformat"` for a new filesystem on the same device or `"replace"` for replacement hardware. |

The caller reviews the candidate configuration and obtains explicit approval before calling `register_configuration()`. An example of a caller applying an already approved card format follows.

```python
from media_archive.config import load_config
from media_archive.state import (
    DeviceChange, DeviceKey, load_registered_devices, register_configuration,
    validate_registered_configuration,
)

candidate = load_config("/absolute/path/to/settings.env")
card = DeviceKey("card", "a")
previous = next(
    device for device in load_registered_devices(candidate.app.database_file)
    if device.key == card
)

register_configuration(candidate, device_changes=[DeviceChange(previous, "reformat")])
validate_registered_configuration(candidate)
```

SQLite serializes registration writers. The API rechecks each supplied previous device and volume under the write lock. If another registration changed the binding after review, the stale approval is rejected. An invalid or unused approval fails the whole batch. Repeating registration of an unchanged configuration without change approvals leaves the registry unchanged.

Database creation and migrations are explicit [state lifecycle operations](state.md). Revision `0002` adds empty registry tables. Loading settings and running `--check-config` read configuration only. Registry checks read the database without changing it. Registration writes only database state.

## Bindings and destinations

- A phone and camera can share an alias and keep distinct source IDs. A registered source keeps its collection binding. Display labels can be edited without changing identity.
- A collection can be registered before an archive assignment. First assignment reserves its destination, including the effective destination of every child. The database stores the group root separately from each collection's relative path. The registered root fixes the audit scope.
- Removing a collection, assignment or group from configuration retains its registrations. In the same archive group, unrelated collections cannot reserve identical or overlapping historical destinations. The registered parent and its descendants can reserve their nested paths.
- A registered archive mapping keeps its group, backup profile, relative path and parent relationship. Moving an assigned collection between backup profiles or a registered card between camera profiles is deferred beyond the MVP. Changing a local path or the mount of a known disk leaves the registry identity intact.

## Devices and filesystem episodes

Local volumes, archive targets, phones and camera cards have separate roles. Archive targets with the same alias in different groups have separate bindings. The current device is the most recently registered device under that role and alias. For filesystem-backed devices, the latest episode of that device records the current UUID. Replacement and reformatting append records so later media history can refer to the actual device and episode used at the time.

A disabled phone profile retains its previous association. A card can be registered before its UUID is available. Its first registered UUID establishes the first volume episode. Clearing a known card UUID in configuration fails the registry check. UUID comparisons are case-insensitive. A known historical UUID or phone serial remains reserved to prevent accidentally registering the same evidence for a new device or episode. Resolving reused hardware identifiers requires a separate workflow.

Registered evidence records an operator-approved configuration association. Physical presence, mount safety, physical independence and cloned-device detection require runtime verification. Registration leaves media history and initialization to their respective stages.

## Errors and verification

Configuration mismatches raise a Polish `ConfigError` without echoing private paths, aliases, UUIDs or serials. Database failures, including a missing database or a revision that requires explicit migration, raise [`StateError`](state.md#entry-points-and-ownership). A failed registration rolls back the batch.

[Registry tests](../tests/test_state_registry.py) use synthetic settings and temporary databases. They cover explicit migration, read-only validation, retained reservations, nested destinations, audit roots, scoped identities, hardware history, stale concurrent approvals and atomic rollback.
