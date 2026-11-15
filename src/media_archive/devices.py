"""Verify Linux block volumes and confine file opens to a verified mount."""

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import subprocess
from typing import BinaryIO

from media_archive._encoding import require_utf8
from media_archive.config import ConfigError, Configuration
from media_archive.state import DeviceKey, RegisteredDevice, StateError, load_registered_devices
from media_archive.state._installation import installation_lock


@dataclass(frozen=True, repr=False)
class MountedVolume:
    root: Path
    mount_id: int
    uuid: str
    block_device: str
    physical_device: str
    hardware_ids: tuple[str, ...]
    read_only: bool
    _root_fd: int

    def hardware_id(self, previous: str | None = None) -> str | None:
        if previous is None:
            return next(iter(self.hardware_ids), None)
        kind = previous.partition(":")[0]
        return next((value for value in self.hardware_ids if value.startswith(f"{kind}:")), None)


@dataclass(frozen=True, repr=False)
class _VolumeSettings:
    uuid: str
    root: Path
    exact_mount: bool
    read_only: bool = False


def _volume_settings(config: Configuration, key: DeviceKey, target: Path | None) -> _VolumeSettings:
    if key.role == "card" and key.group_id is None:
        card = next((item for item in config.camera_cards if item.alias == key.alias), None)
        if card is not None and card.uuid is not None and target is not None:
            return _VolumeSettings(card.uuid, target, True, read_only=True)
    if key.role == "local" and key.group_id is None:
        volume = next((item for item in config.local.volumes if item.alias == key.alias), None)
        if volume is not None and (target is None or target == volume.root):
            return _VolumeSettings(volume.uuid, volume.root, False)
    if key.role == "archive":
        group = next((item for item in config.archive_groups if item.id == key.group_id), None)
        if group is not None:
            disk = next((item for item in group.targets if item.alias == key.alias), None)
            if disk is not None and (target is None or target == disk.mount):
                return _VolumeSettings(disk.uuid, disk.mount, True, disk.state == "read_only")
    raise ConfigError("Wskaż skonfigurowany wolumin, jego UUID i właściwą ścieżkę montowania.")


def _rows(command: list[str], key: str) -> list[dict[str, object]]:
    try:
        result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=10, check=False)
        if result.returncode:
            if command[0] == "findmnt" and result.returncode == 1 and not result.stderr:
                raise StateError("Pod wskazaną ścieżką nie ma oczekiwanego montowania.")
            raise StateError("Narzędzie Linuksa nie mogło sprawdzić nośnika.")
        rows = json.loads(result.stdout)[key]
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        return rows
    except (OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError):
        raise StateError("Nie można odczytać informacji o nośniku z narzędzi Linuksa.") from None


def _mount_id(descriptor: int) -> int:
    with open(f"/proc/self/fdinfo/{descriptor}", encoding="utf-8") as details:
        for line in details:
            if line.startswith("mnt_id:"):
                return int(line.split()[1])
    raise StateError("Linux nie udostępnia identyfikatora otwartego montowania.")


def _directory(stack: ExitStack, path: Path, *, parent: int | None = None,
               mount_id: int | None = None) -> int:
    flags = os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY
    descriptor = parent
    if descriptor is None:
        descriptor = os.open("/", flags)
        stack.callback(os.close, descriptor)
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        descriptor = os.open(part, flags, dir_fd=descriptor)
        stack.callback(os.close, descriptor)
        if mount_id is not None and _mount_id(descriptor) != mount_id:
            raise StateError("Ścieżka przechodzi do innego montowania.")
    return descriptor


def _absolute(path: Path) -> None:
    require_utf8(str(path), StateError)
    if path.anchor != "/" or ".." in path.parts or "\0" in str(path):
        raise StateError("Wymagana jest bezpieczna bezwzględna ścieżka woluminu.")


def _open_root(stack: ExitStack, root: Path, mount_id: int) -> int:
    descriptor = _directory(stack, root)
    if _mount_id(descriptor) != mount_id:
        raise StateError("Montowanie zmieniło się podczas sprawdzania.")
    return descriptor


def _inspect(stack: ExitStack, settings: _VolumeSettings, *, writable: bool) -> MountedVolume:
    root = settings.root
    _absolute(root)
    if writable and settings.read_only:
        raise StateError("Rola nośnika zezwala wyłącznie na odczyt.")
    mounts = _rows([
        "findmnt", "--kernel", "--json", "--list", "--nofsroot",
        "--output", "ID,FSROOT,SOURCE,OPTIONS,TARGET,MAJ:MIN,FSTYPE",
        "--mountpoint" if settings.exact_mount else "--target", str(root),
    ], "filesystems")
    if len(mounts) != 1 or mounts[0].get("fsroot") != "/":
        raise StateError("Nie można jednoznacznie ustalić montowania całego woluminu.")
    mount = mounts[0]
    source, target, options, ident = (mount.get(field) for field in ("source", "target", "options", "id"))
    if (not isinstance(source, str) or not source.startswith("/dev/") or not isinstance(target, str)
            or not isinstance(options, str) or type(ident) is not int
            or not root.is_relative_to(Path(target))
            or (settings.exact_mount and Path(target) != root)):
        raise StateError("Montowanie nie wskazuje na oczekiwany lokalny wolumin.")
    try:
        descriptor = _open_root(stack, root, ident)
    except OSError:
        raise StateError("Nie można bezpiecznie otworzyć katalogu woluminu.") from None

    rows = _rows([
        "lsblk", "--json", "--list",
        "--output", "PATH,KNAME,PKNAME,MAJ:MIN,TYPE,UUID,WWN,SERIAL,RO",
    ], "blockdevices")
    device_path = os.path.realpath(source)
    matches = [row for row in rows if row.get("path") == device_path]
    if len(matches) != 1:
        raise StateError("Nie można jednoznacznie odnaleźć zamontowanego urządzenia blokowego.")
    row = matches[0]
    uuid = row.get("uuid")
    if not isinstance(uuid, str) or uuid.casefold() != settings.uuid.casefold():
        raise StateError("UUID zamontowanego woluminu nie zgadza się z oczekiwanym.")
    if sum(isinstance(value := item.get("uuid"), str) and value.casefold() == uuid.casefold()
           for item in rows) != 1:
        raise StateError("Podłączone woluminy mają niejednoznaczny UUID.")
    if row.get("type") == "part" and isinstance(row.get("pkname"), str):
        parents = [item for item in rows if item.get("kname") == row["pkname"] and item.get("type") == "disk"]
    else:
        parents = [row] if row.get("type") == "disk" else []
    block = row.get("maj:min")
    if len(parents) != 1 or not isinstance(block, str) or not isinstance(parents[0].get("maj:min"), str):
        raise StateError("Nie można jednoznacznie ustalić fizycznego dysku woluminu.")
    # NTFS mounted through ntfs-3g has a FUSE filesystem device number.
    if mount.get("fstype") != "fuseblk" and mount.get("maj:min") != block:
        raise StateError("Montowanie zmieniło urządzenie blokowe podczas sprawdzania.")
    disk = parents[0]
    physical = str(disk["maj:min"])
    hardware = tuple(
        require_utf8(f"{kind}:{value.strip()}", StateError)
        for kind in ("wwn", "serial")
        if isinstance(value := disk.get(kind), str) and value.strip()
        and sum(item.get("type") == "disk" and isinstance(other := item.get(kind), str)
                and other.strip() == value.strip() for item in rows) == 1
    )
    read_only = settings.read_only or "ro" in options.split(",") or row.get("ro") is True or disk.get("ro") is True
    mode = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if (writable and read_only) or not os.access(f"/proc/self/fd/{descriptor}", mode, effective_ids=True):
        raise StateError("Brak wymaganych uprawnień do woluminu.")
    return MountedVolume(root, ident, uuid, block, physical, hardware, read_only, descriptor)


def registered_volume(config: Configuration, key: DeviceKey) -> RegisteredDevice:
    registered = next((device for device in load_registered_devices(config.app.database_file)
                       if device.key == key and device.device_id is not None), None)
    if registered is None:
        raise ConfigError("Wolumin nie ma zarejestrowanego powiązania.")
    return registered


@contextmanager
def inspect_volume(config: Configuration, key: DeviceKey,
                   target: Path | None = None) -> Generator[MountedVolume, None, None]:
    """Read the currently attached candidate against configured settings."""
    with ExitStack() as stack:
        yield _inspect(stack, _volume_settings(config, key, target), writable=False)


@contextmanager
def verify_volume(config: Configuration, key: DeviceKey, target: Path | None = None,
                  *, writable: bool = False) -> Generator[MountedVolume, None, None]:
    """Check role, registered UUID, approved hardware, mount and access."""
    settings = _volume_settings(config, key, target)
    with installation_lock(config.app.database_file), ExitStack() as stack:
        registered = registered_volume(config, key)
        if registered.uuid is None or registered.uuid.casefold() != settings.uuid.casefold():
            raise ConfigError("UUID konfiguracji wymaga jawnej rejestracji.")
        volume = _inspect(stack, settings, writable=writable)
        if registered.hardware_id is not None and registered.hardware_id not in volume.hardware_ids:
            raise StateError("Identyfikator fizycznego urządzenia nie zgadza się z rejestrem.")
        if writable and registered.hardware_id is None:
            raise StateError("Zapis wymaga zatwierdzonego identyfikatora fizycznego urządzenia.")
        yield volume


def open_media_file(volume: MountedVolume, relative: Path, *, scope: Path | None = None) -> BinaryIO:
    """Open a regular file below a verified root without traversing links or nested mounts."""
    require_utf8(str(relative), StateError)
    if relative.anchor or not relative.parts or ".." in relative.parts or "\0" in str(relative):
        raise StateError("Wymagana jest bezpieczna ścieżka pliku względem korzenia woluminu.")
    if scope is not None:
        _absolute(scope)
        if not scope.is_relative_to(volume.root):
            raise StateError("Zakres odczytu wykracza poza sprawdzony wolumin.")
        relative = scope.relative_to(volume.root) / relative
    try:
        with ExitStack() as stack:
            root = _open_root(stack, volume.root, volume.mount_id)
            if not os.path.samestat(os.fstat(root), os.fstat(volume._root_fd)):
                raise StateError("Korzeń woluminu zmienił się podczas sprawdzania.")
            parent = _directory(stack, relative.parent, parent=root, mount_id=volume.mount_id)
            descriptor = os.open(relative.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=parent)
            stack.callback(os.close, descriptor)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or _mount_id(descriptor) != volume.mount_id:
                raise StateError("Ścieżka nie wskazuje na zwykły plik w sprawdzonym montowaniu.")
            return open(f"/proc/self/fd/{descriptor}", "rb")
    except (OSError, ValueError):
        raise StateError("Nie można bezpiecznie otworzyć pliku na woluminie.") from None
