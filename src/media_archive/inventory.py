"""The initial read-only inventory of local collections and archives."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
from time import monotonic
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from media_archive._progress import format_count, format_size
from media_archive.audit import _Scan, _scan
from media_archive.config import Configuration
from media_archive.devices import VolumeUnavailable, registered_volume
from media_archive.sources import SourceEntry, hash_source_file, open_local_source
from media_archive.state import DeviceKey, StateError, open_database, validate_registered_configuration
from media_archive.state.inventory import InventoryFile, save_scope, scope_file_count
from media_archive.state.locking import Resource, collection_resource, lock_resources


@dataclass(frozen=True)
class InventoryResult:
    code: int
    message: str
    details: dict[str, object]


@dataclass(frozen=True)
class _Scope:
    kind: Literal["local", "archive"]
    key: DeviceKey
    root: Path
    history_root: Path
    # Deepest collection first, so nested paths are attributed only once.
    collections: tuple[tuple[str, Path], ...] = ()

    @property
    def label(self) -> str:
        if self.kind == "local":
            return f"zbiór {self.collections[-1][0]}"
        return f"archiwum {self.key.group_id}/{self.key.alias}"

    def run_id(self, volume_id: int, *, hash_files: bool) -> str:
        # A dedicated run identity doubles as a completion marker in the same transaction
        # as its observations. The archive mount path may change for the same volume.
        identity = json.dumps([self.kind, volume_id, str(self.history_root), self.key.group_id,
                               [(name, str(path)) for name, path in self.collections], hash_files])
        return uuid5(NAMESPACE_URL, "media-archive:initial-inventory:" + identity).hex


def _scopes(config: Configuration) -> Iterator[_Scope]:
    for collection in config.collections:
        if collection.parent_id is not None:
            continue
        root = collection.local_root
        volume = next(volume for volume in config.local.volumes if root.is_relative_to(volume.root))
        children = sorted(
            ((item.id, item.local_root) for item in config.collections if item.local_root.is_relative_to(root)),
            key=lambda item: (-len(item[1].parts), item[0]),
        )
        yield _Scope("local", DeviceKey("local", volume.alias), root, root, tuple(children))
    for group in config.archive_groups:
        root = Path(group.root_relative)
        for target in group.targets:
            yield _Scope("archive", DeviceKey("archive", target.alias, group.id), target.mount / root, root)


def local_hash_run_id(config: Configuration, collection_id: str) -> str | None:
    """Identify the full local inventory for the configured collection and current volume."""
    for scope in _scopes(config):
        if any(name == collection_id for name, _ in scope.collections):
            volume = registered_volume(config, scope.key)
            if volume.volume_id is not None:
                return scope.run_id(volume.volume_id, hash_files=True)
    return None


def _read_scope(config: Configuration, scope: _Scope, hash_files: bool, announce: Callable[[str], None],
                errors: list[dict[str, str]]) -> list[InventoryFile]:
    files = []
    directories = scanned_files = 0
    last_message = monotonic()

    def on_directory(entries: tuple[SourceEntry, ...]) -> None:
        nonlocal directories, scanned_files, last_message
        directories += 1
        scanned_files += sum(stat.S_ISREG(entry.mode) for entry in entries)
        now = monotonic()
        if now - last_message >= 1:
            announce(f"{scope.label} · skan metadanych · katalogi {format_count(directories)} · "
                     f"pliki {format_count(scanned_files)}")
            last_message = now

    with open_local_source(config, scope.key, scope=scope.root) as source:
        scan = _Scan()
        _scan(source, scan, on_directory=on_directory)
        for _, root in scope.collections:
            path = root.relative_to(scope.root)
            if path not in scan.observed_at and scan.covers(path):
                scan.errors[path] = "Nie odczytano katalogu skonfigurowanego zbioru."
        for path, error in sorted(scan.errors.items()):
            errors.append({"ścieżka": path.as_posix(), "błąd": error})
        if errors:
            return files
        regular = [entry for entry in scan.entries.values() if stat.S_ISREG(entry.mode)]
        last_message = monotonic()
        total_bytes = sum(entry.size for entry in regular)
        completed_bytes = 0

        def progress(read: int, _size: int) -> None:
            nonlocal last_message
            now = monotonic()
            if now - last_message >= 1:
                announce(f"{scope.label} · pliki {format_count(len(files))}/{format_count(len(regular))} · "
                         f"odczytano {format_size(completed_bytes + read)} / {format_size(total_bytes)}")
                last_message = now

        if hash_files:
            announce(f"{scope.label} · SHA-256: {format_count(len(regular))} plików, {format_size(total_bytes)}.")
        for entry in regular:
            path = scope.history_root / entry.path
            owner = next((name for name, root in scope.collections if path.is_relative_to(root)), None)
            observed_at = scan.observed_at[entry.path.parent]
            sha256 = None
            if hash_files:
                try:
                    sha256 = hash_source_file(source, entry.path, expected=entry, progress=progress).sha256
                except StateError as error:
                    errors.append({"ścieżka": entry.path.as_posix(), "błąd": str(error)})
                    return files
                observed_at = datetime.now(timezone.utc)
                completed_bytes += entry.size
            attributes = entry.volume_fingerprint() if sha256 is not None else None
            files.append(InventoryFile(path.as_posix(), owner, entry.size, entry.modified_ns, observed_at,
                                       sha256, attributes))
    return files


def inventory(config: Configuration, *, hash_local: bool, announce: Callable[[str], None]) -> InventoryResult:
    """Continue the initial inventory; an unfinished scope is retried in full."""
    database = config.app.database_file
    scopes = tuple(_scopes(config))
    resources = [
        *(collection_resource(config, scope.collections[-1][0]) for scope in scopes if scope.kind == "local"),
        *(Resource("archive", group.id) for group in config.archive_groups),
    ]
    results: list[dict[str, object]] = [
        {"zakres": scope.label, "stan": "oczekuje"} for scope in scopes
    ]
    with lock_resources(database, resources):
        validate_registered_configuration(config)
        with open_database(database, writable=True) as connection:
            for scope, result in zip(scopes, results, strict=True):
                hash_files = hash_local and scope.kind == "local"
                registered = registered_volume(config, scope.key)
                volume_id = registered.volume_id
                if volume_id is None:
                    result["uwaga"] = "Brak zarejestrowanego woluminu."
                    continue
                run_id = scope.run_id(volume_id, hash_files=hash_files)
                try:
                    count = scope_file_count(connection, run_id)
                    if count is None and not hash_files and scope.kind == "local":
                        hashed_run = scope.run_id(volume_id, hash_files=True)
                        count = scope_file_count(connection, hashed_run)
                        if count is not None:
                            run_id = hashed_run
                    if count is None:
                        announce(f"Spis · {scope.label}")
                        errors: list[dict[str, str]] = []
                        files = _read_scope(config, scope, hash_files, announce, errors)
                        if errors:
                            result["błędy odczytu"] = errors
                            continue
                        announce(f"{scope.label} · zapis spisu · pliki {format_count(len(files))}")
                        save_scope(connection, run_id, scope.kind, volume_id, files)
                        count = len(files)
                    result.update({"stan": "ukończono", "ID obserwacji": run_id, "pliki": count})
                except VolumeUnavailable as error:
                    result["uwaga"] = str(error)
                except StateError as error:
                    result["błąd"] = str(error)
                except KeyboardInterrupt:
                    return InventoryResult(130, "Przerwano spis. Uruchom ponownie to samo polecenie.",
                                           {"Zakresy spisu": results})
    if any(result["stan"] != "ukończono" for result in results):
        return InventoryResult(3, "Spis niepełny. Sprawdź raport, podłącz nośniki i ponów polecenie.",
                               {"Zakresy spisu": results})
    return InventoryResult(0, "Pierwszy spis istniejących danych ukończony.", {"Zakresy spisu": results})
