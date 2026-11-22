"""Plan the destination of verified imports and accept explicit calendar dates."""

from dataclasses import dataclass, replace
from datetime import date
from hashlib import file_digest
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import Destination
from media_archive.config import Configuration
from media_archive.devices import MountedVolume, registered_volume, staging_volume, verify_volume
from media_archive.metadata import MediaMetadata, MetadataToolError, read_metadata
from media_archive.operations import check_application_paths
from media_archive.sources import LocalSource
from media_archive.state import (
    DeviceKey, StateError, history, open_database, schema_version, transaction, validate_registered_configuration,
)
from media_archive.state.locking import Resource, canonical_path, installation_lock, lock_resources
from media_archive.state.migrations import _head_revision
from media_archive.state.reconciliation import require_reconciled
from media_archive.state.sort_dates import load_sort_dates, record_sort_date


@dataclass(frozen=True)
class SortItem:
    import_id: int
    staging: Path
    name: str
    profile: str
    state: Literal["ready", "undated", "blocked"]
    target: Path | None = None
    metadata: MediaMetadata | None = None
    manual_date: date | None = None
    problem: str | None = None


def _pending(connection: Connection) -> tuple[RowMapping, ...]:
    if schema_version(connection) != _head_revision():
        raise StateError("Plan SORT wymaga aktualnej bazy. Użyj migrate.")
    require_reconciled(connection)
    imports = history.load_staged_imports(connection)
    latest = {(item.source_id, item.content_id): item for item in imports}
    return tuple(item for item in latest.values() if item.placement_id is None)


def _target(config: Configuration, item: RowMapping, profile: RowMapping, name: str, day: date | None) -> Path:
    try:
        collection = config.collection(profile.collection_id)
        configured = config.phone(profile.alias) if profile.kind == "phone" else config.camera(profile.alias)
        if configured.collection_id != collection.id:
            raise StateError("Bieżący profil wskazuje inny zbiór niż zapisany import.")
    except KeyError:
        raise StateError("Profil lub zbiór importu nie występuje w bieżącej konfiguracji.") from None
    if day is None:
        root = config.local.undated_root
        if not any(root.is_relative_to(volume.root) for volume in config.local.volumes):
            raise StateError("Katalog Do wyjaśnienia musi leżeć na zarejestrowanym woluminie lokalnym.")
        protected = [config.local.staging_root, *(child.local_root for child in config.collections),
                     *(disk.mount for group in config.archive_groups for disk in group.targets)]
        if any(root.is_relative_to(path) or path.is_relative_to(root) for path in map(canonical_path, protected)):
            raise StateError("Katalog Do wyjaśnienia koliduje z rzeczywistą ścieżką zbioru, stagingu lub archiwum.")
        target = root / profile.kind / str(profile.id) / item.sha256 / name
    else:
        root = collection.local_root
        year = f"{day.year:04d}"
        if profile.kind == "phone":
            target = root / year / f"{year}.{day.month:02d}" / name
        else:
            target = root / f"Różne {year}" / day.isoformat() / configured.camera_folder / name
        if (config.collection_for_path(collection.id, target).id != collection.id
                or any(child.local_root.is_relative_to(target) for child in config.collections)):
            raise StateError("Ścieżka SORT koliduje z katalogiem zarezerwowanym dla innego zbioru.")
    if canonical_path(target) != target:
        raise StateError("Ścieżka docelowa SORT prowadzi przez dowiązanie symboliczne.")
    return target


def _plan_item(config: Configuration, row: RowMapping, profile: RowMapping, manual: date | None,
               source: LocalSource, volume: MountedVolume, volume_id: int) -> SortItem:
    staging = Path(row.path)
    name = Path(row.source_path).name
    item = SortItem(row.id, staging, name, profile.alias, "blocked", manual_date=manual)
    root = config.local.staging_root
    if row.volume_id != volume_id or not staging.is_relative_to(root) or staging == root:
        return replace(item, problem="Kopia pochodzi z innego woluminu lub katalogu stagingu.")
    try:
        destination = Destination(volume, staging.relative_to(volume.root))
        size = destination.file_size()
        if size is None:
            return replace(item, problem="Brak kopii w stagingu. W razie potrzeby użyj jawnego --reimport.")
        if size != row.size:
            return replace(item, problem="Rozmiar kopii stagingu różni się od zweryfikowanego importu.")
        relative = staging.relative_to(root)
        entry = source.attributes(relative)
        with source.open_file(relative, expected=entry) as stream:
            if file_digest(stream, "sha256").hexdigest() != row.sha256:
                return replace(item, problem="SHA-256 stagingu różni się od zweryfikowanego importu.")
            metadata = read_metadata(stream, Path(name), config.app.timezone)
        item = replace(item, metadata=metadata)
        day = manual or (metadata.timestamp.date() if metadata.timestamp is not None else None)
        target = _target(config, row, profile, name, day)
    except MetadataToolError:
        raise
    except StateError as error:
        return replace(item, problem=str(error))
    except OSError:
        return replace(item, problem="Nie można bezpiecznie odczytać kopii stagingu.")
    return replace(item, target=target, state="undated" if day is None else "ready")


def plan_sort(config: Configuration) -> tuple[SortItem, ...]:
    """Read the latest unplaced imports and preview their destinations without writes."""
    check_application_paths(config)
    database = config.app.database_file
    with installation_lock(database):
        validate_registered_configuration(config)
        with open_database(database) as connection:
            rows = _pending(connection)
            if not rows:
                return ()
            profiles = {source_id: history.load_source_profile(connection, source_id)
                        for source_id in {row.source_id for row in rows}}
            dates = load_sort_dates(connection)
        local = staging_volume(config)
        key = DeviceKey("local", local.alias)
        volume_id = registered_volume(config, key).volume_id
        if volume_id is None:
            raise StateError("Staging nie ma zarejestrowanego woluminu.")
        with verify_volume(config, key) as volume:
            source = LocalSource(volume, config.local.staging_root)
            return tuple(_plan_item(config, row, profiles[row.source_id], dates.get(row.id), source,
                                    volume, volume_id) for row in rows)


def set_sort_date(config: Configuration, import_id: int, value: date) -> None:
    """Store a manual date for an import that still awaits final placement."""
    if type(import_id) is not int or not 0 < import_id <= 2**63 - 1 or type(value) is not date:
        raise StateError("Ręczna data wymaga ID importu i poprawnej daty kalendarzowej.")
    check_application_paths(config)
    database = config.app.database_file
    staging = Resource("collection", str(canonical_path(config.local.staging_root)))
    with lock_resources(database, [staging]), open_database(database, writable=True) as connection:
        validate_registered_configuration(config)
        with transaction(connection):
            if not any(item.id == import_id for item in _pending(connection)):
                raise StateError("Podany import nie oczekuje na SORT.")
            record_sort_date(connection, import_id, value)
