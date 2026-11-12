"""Explicit configuration registration and read-only checks against persisted state."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.selectable import TableClause

from media_archive.config import (
    ConfigError, Configuration, ConfigurationIdentities, validate_registered_identities,
)
from media_archive.config.validation import validate_relationships

from . import _registry_tables as tables
from .database import open_database, transaction
from .errors import StateError
from .migrations import schema_version


@dataclass(frozen=True)
class DeviceKey:
    """Configured role and alias, scoped to a group for archive targets."""

    role: Literal["phone", "card", "local", "archive"]
    alias: str
    group_id: str | None = None


@dataclass(frozen=True)
class RegisteredDevice:
    """Current binding and evidence. Older device and episode IDs remain in SQLite."""

    key: DeviceKey
    device_id: int | None
    volume_id: int | None
    serial: str | None = field(repr=False)
    uuid: str | None = field(repr=False)


@dataclass(frozen=True)
class DeviceChange:
    """Approval for changing the previously reviewed device or volume episode."""

    previous: RegisteredDevice
    action: Literal["reformat", "replace"]


@dataclass(frozen=True)
class _ArchiveMapping:
    group_id: str
    backup_id: str
    parent_id: str | None
    relative: PurePosixPath


def _require_registry(connection: Connection) -> None:
    if schema_version(connection) == "0001":
        raise StateError("Rejestr konfiguracji wymaga jawnej migracji bazy stanu.")


def _registered_identities(connection: Connection) -> ConfigurationIdentities:
    sources = connection.execute(select(tables.sources.c.kind, tables.sources.c.alias)).all()
    return ConfigurationIdentities(
        phones=frozenset(alias for kind, alias in sources if kind == "phone"),
        cameras=frozenset(alias for kind, alias in sources if kind == "camera"),
        collections=frozenset(connection.scalars(select(tables.collections.c.id))),
        backup_profiles=frozenset(connection.scalars(select(tables.backup_profiles.c.id))),
        archive_groups=frozenset(connection.scalars(select(tables.archive_groups.c.id))),
    )


def load_registered_identities(path: str | Path) -> ConfigurationIdentities:
    """Read trusted keys, including identities absent from current configuration."""
    with open_database(path) as connection:
        _require_registry(connection)
        return _registered_identities(connection)


def _find(connection: Connection, table: TableClause, **key: object) -> RowMapping | None:
    return connection.execute(table.select().filter_by(**key)).mappings().one_or_none()


def _insert_device_row(connection: Connection, table: TableClause, **values: object) -> int:
    row_id = connection.execute(table.insert().values(**values)).lastrowid
    if row_id is None:
        raise StateError("Nie można odczytać ID zapisanego urządzenia.")
    return row_id


def _current_device(connection: Connection, key: DeviceKey, slot_id: int) -> RegisteredDevice:
    device = connection.execute(
        tables.devices.select().where(tables.devices.c.slot_id == slot_id)
        .order_by(tables.devices.c.id.desc()).limit(1)
    ).mappings().one_or_none()
    if device is None:
        return RegisteredDevice(key, None, None, None, None)
    volume = connection.execute(
        tables.volume_episodes.select().where(tables.volume_episodes.c.device_id == device.id)
        .order_by(tables.volume_episodes.c.id.desc()).limit(1)
    ).mappings().one_or_none()
    return RegisteredDevice(
        key, device.id, volume.id if volume is not None else None,
        device.serial, volume.uuid if volume is not None else None,
    )


def load_registered_devices(path: str | Path) -> tuple[RegisteredDevice, ...]:
    """Read current device bindings, including those absent from configuration."""
    with open_database(path) as connection:
        _require_registry(connection)
        slots = connection.execute(tables.device_slots.select().order_by(tables.device_slots.c.id)).mappings().all()
        return tuple(
            _current_device(connection, DeviceKey(slot.role, slot.alias, slot.group_id), slot.id)
            for slot in slots
        )


def _ensure_identity(
    connection: Connection, table: TableClause, key: dict[str, str], *, register: bool,
    binding: dict[str, str], label: str | None = None,
) -> RowMapping:
    row = _find(connection, table, **key)
    if row is None:
        if not register:
            raise ConfigError("Konfiguracja zawiera niezarejestrowane ID.")
        values = {**key, **binding}
        if label is not None:
            values["label"] = label
        connection.execute(table.insert().values(**values))
        return connection.execute(table.select().filter_by(**key)).mappings().one()
    if any(row[name] != value for name, value in binding.items()):
        raise ConfigError("Konfiguracja zmienia zapisane powiązanie profilu lub korzeń grupy archiwów.")
    if register and label is not None and row.label != label:
        connection.execute(table.update().where(table.c.id == row.id).values(label=label))
    return row


def _is_ancestor(ancestor: str, descendant: str, mappings: dict[str, _ArchiveMapping]) -> bool:
    parent = mappings[descendant].parent_id
    while parent is not None:
        if parent == ancestor:
            return True
        parent = mappings[parent].parent_id
    return False


def _check_archive_mappings(connection: Connection, config: Configuration, *, register: bool) -> None:
    stored = {
        row.collection_id: _ArchiveMapping(
            row.group_id, row.backup_id, row.parent_id, PurePosixPath(row.archive_relative),
        )
        for row in connection.execute(
            select(*tables.archive_mappings.c, tables.backup_profiles.c.group_id).join_from(
                tables.archive_mappings, tables.backup_profiles,
                tables.archive_mappings.c.backup_id == tables.backup_profiles.c.id,
            )
        ).mappings()
    }
    additions: dict[str, _ArchiveMapping] = {}
    for profile in config.backup_profiles:
        roots = config.profile_collections(profile.id)
        for collection in config.collections:
            if not any(collection.local_root.is_relative_to(root.local_root) for root in roots):
                continue
            mapping = _ArchiveMapping(
                profile.archive_group_id, profile.id, collection.parent_id, PurePosixPath(collection.archive_relative),
            )
            existing = stored.get(collection.id)
            if existing is None:
                if not register:
                    raise ConfigError("Cel archiwalny zbioru wymaga jawnej rejestracji.")
                additions[collection.id] = mapping
            elif existing != mapping:
                raise ConfigError("Konfiguracja zmienia zarejestrowane przypisanie celu archiwalnego.")

    all_mappings = stored | additions
    for collection_id, mapping in additions.items():
        for other_id, other in all_mappings.items():
            if collection_id == other_id or mapping.group_id != other.group_id:
                continue
            if mapping.relative.is_relative_to(other.relative):
                parent, child = other_id, collection_id
            elif other.relative.is_relative_to(mapping.relative):
                parent, child = collection_id, other_id
            else:
                continue
            if mapping.relative == other.relative or not _is_ancestor(parent, child, all_mappings):
                raise ConfigError("Cel koliduje z zarezerwowaną ścieżką archiwalną.")

    # Parent paths are strict prefixes, so insert parents before their children.
    for collection_id, mapping in sorted(additions.items(), key=lambda item: len(item[1].relative.parts)):
        connection.execute(tables.archive_mappings.insert().values(
            collection_id=collection_id, backup_id=mapping.backup_id, parent_id=mapping.parent_id,
            archive_relative=str(mapping.relative),
        ))


def _ensure_device_slot(
    connection: Connection, key: DeviceKey, source_id: int | None, *, register: bool,
) -> int:
    fields = {"role": key.role, "alias": key.alias, "group_id": key.group_id}
    slot = _find(connection, tables.device_slots, **fields)
    if slot is None:
        if not register:
            raise ConfigError("Urządzenie wymaga jawnej rejestracji.")
        return _insert_device_row(connection, tables.device_slots, **fields, source_id=source_id)
    if source_id is not None and slot.source_id != source_id:
        if not register or slot.source_id is not None:
            raise ConfigError("Karta wymaga rejestracji lub jest już przypisana do innego profilu.")
        connection.execute(tables.device_slots.update().where(tables.device_slots.c.id == slot.id).values(
            source_id=source_id,
        ))
    return slot.id


def _approved_device_action(
    current: RegisteredDevice, changes: dict[DeviceKey, DeviceChange],
) -> Literal["reformat", "replace"]:
    approval = changes.pop(current.key, None)
    if approval is None or approval.previous != current:
        raise ConfigError("Zmiana urządzenia wymaga jawnego potwierdzenia bieżącego powiązania.")
    if approval.action == "replace":
        return "replace"
    if approval.action == "reformat" and current.key.role != "phone":
        return "reformat"
    raise ConfigError("Wybrany rodzaj zmiany urządzenia jest niedozwolony.")


def _ensure_device(
    connection: Connection, key: DeviceKey, evidence: str | None, source_id: int | None,
    changes: dict[DeviceKey, DeviceChange], *, register: bool,
) -> None:
    slot_id = _ensure_device_slot(connection, key, source_id, register=register)
    current = _current_device(connection, key, slot_id)
    phone = key.role == "phone"
    if phone and evidence is None:
        # A disabled phone profile retains its last association.
        return
    known = current.serial if phone else current.uuid
    same_evidence = known == evidence
    if not phone and known is not None and evidence is not None:
        same_evidence = known.casefold() == evidence.casefold()
    if current.device_id is not None and same_evidence:
        return
    if not register:
        raise ConfigError("Konfiguracja urządzenia wymaga jawnej rejestracji.")

    new_device = current.device_id is None
    if known is not None:
        if evidence is None:
            raise ConfigError("Konfiguracja usuwa identyfikator zarejestrowanego woluminu.")
        new_device = _approved_device_action(current, changes) == "replace"

    device_id = current.device_id
    if new_device:
        serial = evidence if phone else None
        serial_key = serial.casefold() if serial is not None else None
        if serial_key is not None and _find(connection, tables.devices, serial_key=serial_key) is not None:
            raise ConfigError("Identyfikator telefonu należy już do zapisanego urządzenia.")
        device_id = _insert_device_row(
            connection, tables.devices, slot_id=slot_id, serial=serial, serial_key=serial_key,
        )
    if not phone and evidence is not None:
        uuid_key = evidence.casefold()
        if _find(connection, tables.volume_episodes, uuid_key=uuid_key) is not None:
            raise ConfigError("UUID należy już do zapisanego epizodu woluminu.")
        connection.execute(tables.volume_episodes.insert().values(device_id=device_id, uuid=evidence, uuid_key=uuid_key))


def _check_configuration(
    connection: Connection, config: Configuration, *, register: bool, changes: dict[DeviceKey, DeviceChange],
) -> None:
    for collection in config.collections:
        _ensure_identity(connection, tables.collections, {"id": collection.id}, register=register,
                         binding={}, label=collection.label)
    for group in config.archive_groups:
        _ensure_identity(connection, tables.archive_groups, {"id": group.id}, register=register,
                         binding={"root_relative": group.root_relative})
    for profile in config.backup_profiles:
        _ensure_identity(connection, tables.backup_profiles, {"id": profile.id}, register=register,
                         binding={"group_id": profile.archive_group_id}, label=profile.label)
    _check_archive_mappings(connection, config, register=register)

    for phone in config.phones:
        source = _ensure_identity(connection, tables.sources, {"kind": "phone", "alias": phone.alias},
                                  register=register, binding={"collection_id": phone.collection_id}, label=phone.label)
        _ensure_device(connection, DeviceKey("phone", phone.alias), phone.device.serial if phone.device else None,
                       source.id, changes, register=register)

    cards = {card.alias: card for card in config.camera_cards}
    assigned_cards: dict[str, int] = {}
    for camera in config.cameras:
        source = _ensure_identity(connection, tables.sources, {"kind": "camera", "alias": camera.alias},
                                  register=register, binding={"collection_id": camera.collection_id}, label=camera.label)
        for card in camera.cards:
            if cards.get(card.alias) != card:
                raise ConfigError("Profil kamery wskazuje niespójny wpis karty.")
            assigned_cards[card.alias] = source.id
    for card in config.camera_cards:
        _ensure_device(connection, DeviceKey("card", card.alias), card.uuid, assigned_cards.get(card.alias),
                       changes, register=register)
    for volume in config.local.volumes:
        _ensure_device(connection, DeviceKey("local", volume.alias), volume.uuid, None, changes, register=register)
    for group in config.archive_groups:
        for target in group.targets:
            _ensure_device(connection, DeviceKey("archive", target.alias, group.id), target.uuid, None,
                           changes, register=register)


def validate_registered_configuration(config: Configuration) -> None:
    """Check the candidate against one read-only snapshot of its existing database."""
    validate_relationships(config)
    with open_database(config.app.database_file) as connection:
        _require_registry(connection)
        validate_registered_identities(config, _registered_identities(connection))
        _check_configuration(connection, config, register=False, changes={})


def register_configuration(config: Configuration, *, device_changes: Iterable[DeviceChange] = ()) -> None:
    """Register an explicitly approved configuration in one short write transaction."""
    validate_relationships(config)
    changes: dict[DeviceKey, DeviceChange] = {}
    for change in device_changes:
        key = change.previous.key
        if key in changes:
            raise ConfigError("Powtórzone potwierdzenie zmiany urządzenia.")
        changes[key] = change
    with open_database(config.app.database_file, writable=True) as connection, transaction(connection):
        _check_configuration(connection, config, register=True, changes=changes)
        if changes:
            raise ConfigError("Potwierdzona zmiana urządzenia nie odpowiada konfiguracji.")
