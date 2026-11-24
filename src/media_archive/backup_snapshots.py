"""Preflight and publish one BACKUP state generation across archive volumes."""

from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import stat
from tempfile import TemporaryDirectory

from media_archive._transfer_files import Destination, _rename_noreplace, _signature
from media_archive.backup_capacity import snapshot_sizes
from media_archive.config import ConfigError, Configuration
from media_archive.config.loader import _parse_config
from media_archive.devices import MountedVolume, VolumeUnavailable, _mount_id, verify_volume
from media_archive.sort_conflicts import _name_key, require_destination_name
from media_archive.state import DeviceKey, StateError
from media_archive.state.snapshots import (
    Generation, SnapshotPublishedError, _CONFIG, _DATABASE, _MANIFEST, _copy_private, _sync_generation,
    prepare_snapshot, validate_snapshot,
)

_FILES = (_DATABASE, _CONFIG, _MANIFEST)
_KEEP = 5


def capture_backup_settings(config: Configuration) -> bytes:
    """Pin the configuration bytes before media operations start."""
    try:
        contents = config.source_file.read_bytes()
        current = _parse_config(config.source_file, contents.decode("utf-8-sig"))
    except (OSError, UnicodeError):
        raise StateError("Nie można odczytać konfiguracji do snapshotu BACKUP.") from None
    if current != config:
        raise StateError("Konfiguracja zmieniła się przed rozpoczęciem BACKUP.")
    return contents


def _directory(config: Configuration, key: DeviceKey, volume: MountedVolume) -> Destination:
    archive_dir = Path(config.app.archive_state_dir)
    data_root = Path(config.archive_group(key.group_id).root_relative)
    archive_key, data_key = (Path(*map(_name_key, path.parts)) for path in (archive_dir, data_root))
    if archive_key.is_relative_to(data_key) or data_key.is_relative_to(archive_key):
        raise StateError("Katalog snapshotów koliduje z korzeniem archiwum.")
    require_destination_name(Destination(volume, archive_dir))
    return Destination(volume, archive_dir / "snapshot-probe")


def _required(destination: Destination, sizes: tuple[int, ...]) -> tuple[int, int]:
    available, unit = destination.space()
    missing = destination.preflight(allow_existing=True)
    required = (sum((size + unit - 1) // unit for size in sizes) + len(missing) + 1) * unit
    return available, required


def preflight_snapshots(config: Configuration, selected: set[DeviceKey], checks: int
                        ) -> tuple[tuple[DeviceKey, ...], list[dict[str, object]], bool]:
    """Budget snapshot-only recipients, as well as the targets receiving backup files."""
    admitted = []
    rows = []
    blocked = False
    sizes = snapshot_sizes(config, checks)
    for group in config.archive_groups:
        for target in group.targets:
            if target.state != "active":
                continue
            key = DeviceKey("archive", target.alias, group.id)
            row: dict[str, object] = {"grupa": group.id, "cel": target.alias}
            rows.append(row)
            try:
                with verify_volume(config, key, writable=True) as volume:
                    admitted.append(key)
                    available, required = _required(_directory(config, key, volume), sizes)
                    required += config.app.min_free_bytes
                    deficit = max(0, required - available)
                    row.update(dostępne=available, wymagane=required, brakuje=deficit)
                    blocked |= deficit > 0
            except VolumeUnavailable as error:
                row["stan"] = "odłączony"
                if key in selected:
                    row["błąd"] = str(error)
                    blocked = True
            except (ConfigError, StateError, OSError) as error:
                row["błąd"] = str(error) if isinstance(error, (ConfigError, StateError)) else "Błąd odczytu miejsca."
                blocked |= key in admitted or key in selected
    return tuple(admitted), rows, blocked or not admitted


def _rotate(parent: int, current: str) -> list[str]:
    root = Path(f"/proc/self/fd/{parent}")
    older = []
    for path in root.iterdir():
        if path.name == current or not re.fullmatch(r"snapshot-[0-9a-f]{32}", path.name):
            continue
        try:
            status = path.lstat()
            if not stat.S_ISDIR(status.st_mode):
                continue
            fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            try:
                if _mount_id(fd) != _mount_id(parent) or not os.path.samestat(status, os.fstat(fd)):
                    continue
                if set(os.listdir(fd)) != set(_FILES):
                    continue
                files = tuple(_signature(os.stat(name, dir_fd=fd, follow_symlinks=False)) for name in _FILES)
                generation = validate_snapshot(path)
                if _signature(path.lstat()) != _signature(status):
                    continue
            finally:
                os.close(fd)
        except (ConfigError, StateError, OSError):
            continue
        if path.name == f"snapshot-{generation.generation_id}":
            older.append((datetime.fromisoformat(generation.created_at), path.name, _signature(status), files))
    removed = []
    for _, name, status, files in sorted(older, reverse=True)[_KEEP - 1:]:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            current_files = tuple(_signature(os.stat(filename, dir_fd=fd, follow_symlinks=False))
                                  for filename in _FILES)
            if (_signature(os.fstat(fd)) != status or _mount_id(fd) != _mount_id(parent)
                    or current_files != files or set(os.listdir(fd)) != set(_FILES)):
                raise StateError("Generacja zmieniła się przed rotacją.")
            for filename in _FILES:
                os.unlink(filename, dir_fd=fd)
            if not os.path.samestat(os.stat(name, dir_fd=parent, follow_symlinks=False), os.fstat(fd)):
                raise StateError("Katalog generacji zmienił się podczas rotacji.")
            os.rmdir(name, dir_fd=parent)
        finally:
            os.close(fd)
        removed.append(name)
    os.fsync(parent)
    return removed


def _publish(config: Configuration, key: DeviceKey, source: Path, generation: Generation) -> list[str]:
    name = f"snapshot-{generation.generation_id}"
    sizes = tuple((source / filename).stat().st_size for filename in _FILES)
    with verify_volume(config, key, writable=True) as volume:
        destination = _directory(config, key, volume)
        available, required = _required(destination, sizes)
        if available < required + config.app.min_free_bytes:
            raise StateError("Za mało miejsca na generację i wymaganą rezerwę.")
        with destination.parent(create=True) as parent:
            directory = Path(f"/proc/self/fd/{parent}")
            with TemporaryDirectory(prefix=".snapshot-", dir=directory) as temporary:
                staged = Path(temporary)
                for filename in _FILES:
                    _copy_private(source / filename, staged / filename)
                if validate_snapshot(staged) != generation:
                    raise StateError("Kopia snapshotu różni się od przygotowanej generacji.")
                _sync_generation(staged)
                destination.check_parent(parent)
                _rename_noreplace(parent, staged.name, parent, name)
                try:
                    destination.check_parent(parent, sync=True)
                except (OSError, StateError):
                    raise SnapshotPublishedError("Generacja jest widoczna. Trwałość zapisu wymaga sprawdzenia.") from None
            try:
                return _rotate(parent, name)
            except (OSError, StateError):
                raise SnapshotPublishedError("Generacja została zapisana. Rotacja wymaga sprawdzenia.") from None


def distribute_snapshot(config: Configuration, recipients: tuple[DeviceKey, ...],
                        settings: bytes) -> tuple[list[dict[str, object]], bool]:
    """Prepare once after media work, then publish independently to admitted volumes."""
    rows = []
    failed = False
    try:
        with TemporaryDirectory(prefix=".backup-snapshot-", dir=config.app.state_dir) as temporary:
            staged = Path(temporary)
            generation = prepare_snapshot(config, staged, settings=settings)
            for key in recipients:
                row: dict[str, object] = {"grupa": key.group_id, "cel": key.alias,
                                          "generacja": f"snapshot-{generation.generation_id}"}
                rows.append(row)
                try:
                    row["usunięte starsze generacje"] = _publish(config, key, staged, generation)
                except (ConfigError, StateError, OSError) as error:
                    row["błąd"] = str(error) if isinstance(error, (ConfigError, StateError)) else "Błąd zapisu generacji."
                    if isinstance(error, SnapshotPublishedError):
                        row["stan"] = "opublikowano, wymaga sprawdzenia"
                    failed = True
                else:
                    row["stan"] = "zweryfikowano i zapisano"
    except (ConfigError, StateError, OSError, sqlite3.Error) as error:
        rows.append({"błąd": str(error) if isinstance(error, (ConfigError, StateError))
                     else "Nie można przygotować generacji."})
        failed = True
    return rows, failed
