"""Read-only adapters for mounted Linux volumes and registered ADB phones."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import select
import shlex
import stat
import subprocess
from typing import BinaryIO

from media_archive._encoding import require_utf8
from media_archive.config import ConfigError, Configuration
from media_archive.devices import MountedVolume, _media_descriptor, _mount_id, open_media_file, registered_volume, verify_volume
from media_archive.state import DeviceKey, StateError, validate_registered_configuration
from media_archive.state._installation import installation_lock

_CHUNK_SIZE = 1024 * 1024
_ADB_TIMEOUT = 30

# The configured Android root may itself use a system alias such as /sdcard.
# Below the root, traverse one literal directory name at a time.
_ENTER_ROOT = 'cd "$1" || exit 1\n'
_TRAVERSE = """
for part; do
    [ ! -L "./$part" ] && cd "./$part" || exit 1
done
"""
_ENTER = _ENTER_ROOT + "shift\n" + _TRAVERSE
_ENTER_PARENT = _ENTER_ROOT + "name=$2\nshift 2\n" + _TRAVERSE
_STAT = 'stat -c "%f %s %Y" "./$name"'
_READ = '[ ! -L "./$name" ] && [ -f "./$name" ] || exit 1\nexec cat "./$name"'
_LIST = r"""
[ -r . ] && [ -x . ] || exit 1
find . -mindepth 1 -maxdepth 1 -exec sh -c '
    for item; do
        data=$(stat -c "%f %s %Y" "$item") || exit 1
        printf "%s\000%s\000" "$item" "$data" || exit 1
    done
' sh {} +
"""


@dataclass(frozen=True, repr=False)
class SourceEntry:
    """Attributes of one path relative to the source root."""

    path: Path
    mode: int
    size: int
    modified_ns: int

    @classmethod
    def from_stat(cls, path: Path, info: os.stat_result) -> "SourceEntry":
        return cls(path, info.st_mode, info.st_size, info.st_mtime_ns)


@dataclass(frozen=True)
class SourceCapabilities:
    modification_time_resolution_ns: int
    source_hashing: bool = False
    byte_offset_resume: bool = False


def _relative(path: Path) -> Path:
    text = require_utf8(str(path), StateError)
    if path.anchor or ".." in path.parts or "\0" in text:
        raise StateError("Ścieżka musi pozostać w skonfigurowanym katalogu źródłowym.")
    return path


@dataclass(frozen=True, repr=False)
class LocalSource:
    _volume: MountedVolume
    _scope: Path | None
    capabilities = SourceCapabilities(modification_time_resolution_ns=1)

    def list_directory(self, path: Path = Path(".")) -> tuple[SourceEntry, ...]:
        path = _relative(path)
        with _media_descriptor(self._volume, path, self._scope) as descriptor:
            directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=descriptor)
            try:
                with os.scandir(directory) as entries:
                    result = []
                    for entry in entries:
                        require_utf8(entry.name, StateError)
                        child = os.open(entry.name, os.O_PATH | os.O_NOFOLLOW, dir_fd=directory)
                        try:
                            if _mount_id(child) != self._volume.mount_id:
                                raise StateError("Katalog źródłowy zawiera inne montowanie.")
                            result.append(SourceEntry.from_stat(path / entry.name, os.fstat(child)))
                        finally:
                            os.close(child)
            finally:
                os.close(directory)
        return tuple(sorted(result, key=lambda entry: entry.path))

    def attributes(self, path: Path) -> SourceEntry:
        path = _relative(path)
        with _media_descriptor(self._volume, path, self._scope) as descriptor:
            return SourceEntry.from_stat(path, os.fstat(descriptor))

    @contextmanager
    def read_chunks(self, path: Path) -> Generator[Iterator[bytes], None, None]:
        with open_media_file(self._volume, _relative(path), scope=self._scope) as source:
            yield _read_local(source)


def _read_local(source: BinaryIO) -> Iterator[bytes]:
    try:
        yield from iter(lambda: source.read(_CHUNK_SIZE), b"")
    except OSError:
        raise StateError("Nie można odczytać pliku z lokalnego źródła.") from None


@contextmanager
def open_local_source(config: Configuration, key: DeviceKey, *, target: Path | None = None,
                      scope: Path | None = None) -> Generator[LocalSource, None, None]:
    with verify_volume(config, key, target) as volume:
        source = LocalSource(volume, scope)
        if not stat.S_ISDIR(source.attributes(Path(".")).mode):
            raise StateError("Lokalne źródło musi wskazywać na katalog.")
        yield source


@contextmanager
def open_camera_source(config: Configuration, camera_alias: str, card_alias: str,
                       mount: Path) -> Generator[LocalSource, None, None]:
    try:
        camera = config.camera(camera_alias)
    except KeyError:
        raise ConfigError("Nie znaleziono skonfigurowanego profilu kamery.") from None
    if not any(card.alias == card_alias for card in camera.cards):
        raise ConfigError("Karta nie jest przypisana do wybranego profilu kamery.")
    key = DeviceKey("card", card_alias)
    with open_local_source(config, key, target=mount, scope=mount / camera.source_relative) as source:
        validate_registered_configuration(config)
        yield source


def _adb(arguments: list[str], *, reject_stderr: bool = False) -> bytes:
    try:
        result = subprocess.run(["adb", *arguments], capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=_ADB_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        raise StateError("Nie można uruchomić lub zakończyć polecenia ADB.") from None
    if result.returncode or (reject_stderr and result.stderr):
        raise StateError("ADB nie ukończyło odczytu źródła.")
    return result.stdout


def _require_phone(serial: str) -> None:
    expected = require_utf8(serial, ConfigError).encode("utf-8")
    rows = [row for line in _adb(["devices", "-l"]).splitlines()
            if (row := line.split()) and row[0] == expected]
    if len(rows) != 1:
        raise StateError("Telefon ADB jest odłączony lub jego identyfikator jest niejednoznaczny.")
    row = rows[0]
    if row[1:2] == [b"unauthorized"]:
        raise StateError("Telefon ADB wymaga autoryzacji na urządzeniu.")
    if row[1:2] != [b"device"]:
        raise StateError("Telefon ADB nie jest gotowy do odczytu.")


def _remote_attributes(path: Path, data: bytes) -> SourceEntry:
    try:
        mode, size, seconds = data.split()
        length = int(size)
        if length < 0:
            raise ValueError
        return SourceEntry(path, int(mode, 16), length, int(seconds) * 1_000_000_000)
    except ValueError:
        raise StateError("ADB zwróciło nieprawidłowe atrybuty pliku.") from None


def _remote_listing(path: Path, data: bytes) -> tuple[SourceEntry, ...]:
    fields = data.split(b"\0")
    if fields[-1] or len(fields) % 2 != 1:
        raise StateError("ADB zwróciło niekompletną listę plików.")
    entries = {}
    for raw, attributes in zip(fields[:-1:2], fields[1::2], strict=True):
        if not raw.startswith(b"./") or b"/" in raw[2:] or raw[2:] in (b"", b".", b".."):
            raise StateError("ADB zwróciło ścieżkę spoza katalogu źródłowego.")
        try:
            relative = path / raw[2:].decode("utf-8")
        except UnicodeDecodeError:
            raise StateError("Nazwy plików źródła muszą być zapisane w UTF-8.") from None
        if relative in entries:
            raise StateError("ADB zwróciło zduplikowaną nazwę pliku.")
        entries[relative] = _remote_attributes(relative, attributes)
    return tuple(entries[key] for key in sorted(entries))


@dataclass(frozen=True, repr=False)
class AdbSource:
    _serial: str
    _root: Path
    capabilities = SourceCapabilities(modification_time_resolution_ns=1_000_000_000)

    def _shell(self, script: str, *parts: str) -> list[str]:
        command = shlex.join(["sh", "-c", script, "sh", str(self._root), *parts])
        return ["-s", self._serial, "shell", "-T", "-n", command]

    def list_directory(self, path: Path = Path(".")) -> tuple[SourceEntry, ...]:
        path = _relative(path)
        return _remote_listing(path, _adb(self._shell(_ENTER + _LIST, *path.parts), reject_stderr=True))

    def attributes(self, path: Path) -> SourceEntry:
        path = _relative(path)
        command = self._shell(_ENTER_PARENT + _STAT, path.name or ".", *path.parent.parts)
        return _remote_attributes(path, _adb(command, reject_stderr=True))

    @contextmanager
    def read_chunks(self, path: Path) -> Generator[Iterator[bytes], None, None]:
        path = _relative(path)
        command = self._shell(_ENTER_PARENT + _READ, path.name or ".", *path.parent.parts)
        try:
            process = subprocess.Popen(["adb", *command], stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            raise StateError("Nie można uruchomić odczytu pliku przez ADB.") from None
        with process:
            try:
                yield _read_remote(process)
            finally:
                if process.poll() is None:
                    process.kill()


def _read_remote(process: subprocess.Popen[bytes]) -> Iterator[bytes]:
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    try:
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        while poller.poll(_ADB_TIMEOUT * 1000):
            chunk = os.read(descriptor, _CHUNK_SIZE)
            if not chunk:
                if process.wait(timeout=_ADB_TIMEOUT) != 0:
                    raise StateError("ADB przerwało odczyt pliku.")
                return
            yield chunk
    except (OSError, subprocess.TimeoutExpired):
        raise StateError("Nie można ukończyć odczytu pliku przez ADB.") from None
    raise StateError("Przekroczono czas oczekiwania na dane z ADB.")


@contextmanager
def open_adb_source(config: Configuration, phone_alias: str) -> Generator[AdbSource, None, None]:
    try:
        phone = config.phone(phone_alias)
    except KeyError:
        raise ConfigError("Nie znaleziono skonfigurowanego profilu telefonu.") from None
    device = phone.device
    if device is None:
        raise ConfigError("Wybrany profil telefonu jest nieaktywny.")
    with installation_lock(config.app.database_file):
        registered = registered_volume(config, DeviceKey("phone", phone_alias))
        if registered.serial != device.serial:
            raise ConfigError("Identyfikator telefonu wymaga jawnej rejestracji.")
        _require_phone(device.serial)
        if b"shell_v2" not in _adb(["-s", device.serial, "features"]).splitlines():
            raise StateError("Telefon ADB nie udostępnia wymaganego protokołu odczytu.")
        source = AdbSource(device.serial, device.camera_dir)
        if not stat.S_ISDIR(source.attributes(Path(".")).mode):
            raise StateError("Źródło ADB musi wskazywać na katalog.")
        yield source
