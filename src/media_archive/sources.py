"""Read-only adapters for mounted Linux volumes and registered ADB phones."""

from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
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
_STAT_FORMAT = "%f %s %Y %Z %d %i"
_STAT = f'stat -c "{_STAT_FORMAT}" "./$name"'
_READ = _ENTER_ROOT + "name=$2\nexpected=$3\nshift 3\n" + _TRAVERSE + f"""
[ ! -L "./$name" ] && [ -f "./$name" ] || exit 1
exec 3<"./$name" || exit 1
[ -f /proc/self/fd/0 ] <&3 || exit 1
verify_open_file() {{
    [ -z "$expected" ] || {{
        actual=$(stat -L -c "{_STAT_FORMAT}" /proc/self/fd/0 <&3) && [ "$actual" = "$expected" ]
    }}
}}
verify_open_file || exit 1
cat <&3 || exit 1
verify_open_file
"""
_LIST = rf"""
[ -r . ] && [ -x . ] || exit 1
find . -mindepth 1 -maxdepth 1 -exec sh -c '
    for item; do
        data=$(stat -c "{_STAT_FORMAT}" "$item") || exit 1
        printf "%s\000%s\000" "$item" "$data" || exit 1
    done
' sh {{}} +
"""


@dataclass(frozen=True, repr=False)
class SourceEntry:
    """Attributes of one path relative to the source root."""

    path: Path
    mode: int
    size: int
    modified_ns: int
    changed_ns: int
    device: int
    inode: int

    @classmethod
    def from_stat(cls, path: Path, info: os.stat_result) -> "SourceEntry":
        return cls(path, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_dev, info.st_ino)


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

    @property
    def physical_device(self) -> str:
        return self._volume.physical_device

    @property
    def hardware_ids(self) -> tuple[str, ...]:
        return self._volume.hardware_ids

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
    def open_file(self, path: Path, *, expected: SourceEntry | None = None) -> Generator[BinaryIO, None, None]:
        """Keep the verified local file open for an operation using its descriptor."""
        with open_media_file(self._volume, _relative(path), scope=self._scope) as source:
            if expected is not None:
                _check_open_file(path, source, expected)
            yield source
            if expected is not None:
                _check_open_file(path, source, expected)

    @contextmanager
    def read_chunks(self, path: Path, *, expected: SourceEntry | None = None) -> Generator[Iterator[bytes], None, None]:
        with self.open_file(path, expected=expected) as source:
            yield _read_local(source)


def _check_open_file(path: Path, source: BinaryIO, expected: SourceEntry) -> None:
    try:
        observed = SourceEntry.from_stat(path, os.fstat(source.fileno()))
    except OSError:
        raise StateError("Nie można sprawdzić atrybutów otwartego pliku.") from None
    if observed != expected:
        raise StateError("Plik źródłowy zmienił się podczas odczytu.")


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
        mode, size, modified, changed, device, inode = data.split()
        length = int(size)
        if length < 0:
            raise ValueError
        return SourceEntry(path, int(mode, 16), length, int(modified) * 1_000_000_000,
                           int(changed) * 1_000_000_000, int(device), int(inode))
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
    def read_chunks(self, path: Path, *, expected: SourceEntry | None = None) -> Generator[Iterator[bytes], None, None]:
        path = _relative(path)
        metadata = ""
        if expected is not None:
            metadata = (f"{expected.mode:x} {expected.size} {expected.modified_ns // 1_000_000_000} "
                        f"{expected.changed_ns // 1_000_000_000} {expected.device} {expected.inode}")
        command = self._shell(_READ, path.name or ".", metadata, *path.parent.parts)
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


@dataclass(frozen=True)
class SourceDigest:
    sha256: str
    size: int


class ReadCancelled(StateError):
    """The caller cancelled the read before a digest was accepted."""


def _check_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ReadCancelled("Anulowano odczyt pliku.")


def hash_source_file(source: LocalSource | AdbSource, path: Path, *,
                     progress: Callable[[int, int], None] | None = None,
                     cancelled: Callable[[], bool] | None = None,
                     expected: SourceEntry | None = None,
                     on_chunk: Callable[[bytes], object] | None = None) -> SourceDigest:
    """Return SHA-256 and byte count after a complete read and matching attributes."""
    _check_cancelled(cancelled)
    before = source.attributes(path)
    if expected is not None and before != expected:
        raise StateError("Plik źródłowy zmienił się od inwentaryzacji.")
    if not stat.S_ISREG(before.mode):
        raise StateError("Haszowanie wymaga zwykłego pliku źródłowego.")
    if progress is not None:
        progress(0, before.size)
    _check_cancelled(cancelled)

    digest = sha256()
    size = 0
    with source.read_chunks(path, expected=before) as chunks:
        for chunk in chunks:
            size += len(chunk)
            if size > before.size:
                raise StateError("Odczytana liczba bajtów nie zgadza się z rozmiarem pliku.")
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
            if progress is not None:
                progress(size, before.size)
            _check_cancelled(cancelled)

    _check_cancelled(cancelled)
    if size != before.size:
        raise StateError("Odczytana liczba bajtów nie zgadza się z rozmiarem pliku.")
    if source.attributes(path) != before:
        raise StateError("Plik źródłowy zmienił się podczas odczytu.")
    _check_cancelled(cancelled)
    return SourceDigest(digest.hexdigest(), size)


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
