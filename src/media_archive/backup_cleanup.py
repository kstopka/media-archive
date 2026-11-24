"""Plan, confirm and revalidate archive-only cleanup within an All scope."""

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
import re
import stat

from media_archive._transfer_files import Destination, is_transfer_part
from media_archive.audit import _Scan, _scan
from media_archive.backup_planning import BackupFile, _archive_owner, _local_mount
from media_archive.config import ConfigError, Configuration
from media_archive.config.collections import BackupProfile
from media_archive.devices import MountedVolume, registered_volume, verify_volume
from media_archive.sort_conflicts import _name_key, require_destination_name
from media_archive.sources import LocalSource, SourceDigest, SourceEntry, hash_source_file
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.locking import OperationLocks, Resource, canonical_path, collection_resource, require_locks


@dataclass(frozen=True)
class Removal:
    target: str
    collection_id: str
    path: Path
    local_paths: tuple[Path, ...]  # relative to this collection
    entry: SourceEntry
    digest: SourceDigest

    def report(self) -> dict[str, object]:
        return {"cel": self.target, "zbiór": self.collection_id, "ścieżka": self.path.as_posix(),
                "bajty": self.digest.size, "sha256": self.digest.sha256}


Approval = Callable[[tuple[Removal, ...]], bool]


@dataclass(frozen=True)
class _CleanupState:
    local: dict[str, dict[Path, SourceEntry]]
    archive: dict[tuple[str, str], dict[Path, SourceEntry]]
    mounts: dict[DeviceKey, tuple[int, str]]
    removals: tuple[Removal, ...]


def _key(path: PurePath) -> Path:
    return Path(*(_name_key(part) for part in path.parts))


def _entries(source: LocalSource) -> dict[Path, SourceEntry]:
    scan = _Scan()
    start = source.attributes(Path("."))
    _scan(source, scan)
    if scan.errors:
        raise StateError("Nie można potwierdzić pełnego, stabilnego skanu katalogu do sprzątania.")
    entries = {Path("."): start}
    for path, entry in scan.entries.items():
        if not (stat.S_ISREG(entry.mode) or stat.S_ISDIR(entry.mode)):
            raise StateError("Sprzątanie wymaga zwykłych plików i katalogów w sprawdzanym zakresie.")
        key = _key(path)
        if key in entries:
            raise StateError("Niejednoznaczna pisownia ścieżek w katalogu do sprzątania.")
        entries[key] = entry
    if any(source.attributes(entry.path) != entry for entry in entries.values() if stat.S_ISDIR(entry.mode)):
        raise StateError("Nie można potwierdzić pełnego, stabilnego skanu katalogu do sprzątania.")
    return entries


def _present(path: Path, entries: dict[Path, SourceEntry]) -> bool:
    key = _key(path)
    return key in entries or any(parent in entries and not stat.S_ISDIR(entries[parent].mode) for parent in key.parents)


def _archive_entries(volume: MountedVolume, root: Path) -> tuple[LocalSource, dict[Path, SourceEntry]]:
    source = LocalSource(volume, volume.root / root)
    require_destination_name(Destination(volume, root))
    # Check absence without swallowing other errors opening the archive subtree.
    try:
        with Destination(volume, root / ".cleanup-probe").parent():
            pass
    except FileNotFoundError:
        return source, {}
    return source, _entries(source)


def _check_local_absence(source: LocalSource, entries: dict[Path, SourceEntry], paths: tuple[Path, ...]) -> None:
    for path in paths:
        for parent in reversed(path.parents):
            entry = entries.get(_key(parent))
            if entry is None:
                break
            if source.attributes(entry.path) != entry:
                raise StateError("Katalog lokalny zmienił się przed usunięciem kopii.")


def execute_cleanup(config: Configuration, profile: BackupProfile, collection_id: str | None,
                    targets: tuple[str, ...], files: tuple[BackupFile, ...],
                    approve: Approval, locks: OperationLocks) -> tuple[int, str, dict[str, object]]:
    """Run after a verified transfer phase while the caller holds BACKUP locks."""
    roots = tuple(item for item in config.profile_collections(profile.id)
                  if collection_id is None or item.id == collection_id)
    database = config.app.database_file
    require_locks(locks, canonical_path(database), {
        Resource("archive", profile.archive_group_id), *(collection_resource(config, item.id) for item in roots),
    })
    eligible = {item.id: item for item in config.collections
                if item.allow_delete and config.policy(item.policy_id).deletions == "confirm_each_run"
                and any(item.local_root.is_relative_to(root.local_root) for root in roots)}
    if not eligible:
        return 0, "Brak zbiorów z potwierdzanym sprzątaniem w tym zakresie.", {"Plan usunięć": []}
    group = config.archive_group(profile.archive_group_id)
    root = Path(group.root_relative)
    keys = {alias: DeviceKey("archive", alias, group.id) for alias in targets}
    bindings = {alias: registered_volume(config, key) for alias, key in keys.items()}
    with open_database(database, writable=True) as connection:
        reserved = history.archive_destinations(connection, group.id, root)
        reserved = tuple((owner, _key(path)) for owner, path in reserved)
        assignments = history.load_backup_assignments(connection, set(eligible))
        pending = {(entry.volume_id, _key(Path(entry.path)))
                    for operation in journal.load_unfinished(connection)
                    if (entry := history.load_occurrence(connection, operation.target_occurrence_id)).kind == "archive"}
        removals_to_reconcile = [
            (alias, intent) for alias, binding in bindings.items()
            for intent in history.load_pending_archive_removals(connection, binding.volume_id)
            if _archive_owner(_key(Path(intent.path)), reserved) in eligible
        ]
    protected: dict[tuple[int, Path], set[Path]] = {}
    for assignment in assignments:
        for device, path in ((assignment.first_device_id, assignment.first_path),
                             (assignment.second_device_id, assignment.second_path)):
            protected.setdefault((device, _key(Path(path or assignment.required_path))), set()).add(Path(assignment.path))

    expected: dict[str, dict[Path, SourceEntry]] = {owner: {} for owner in eligible}
    for file in files:
        if file.collection_id in expected:
            relative = file.path.relative_to(eligible[file.collection_id].local_root)
            expected[file.collection_id][file.path] = replace(file.entry, path=relative)

    def capture(stack: ExitStack) -> tuple[_CleanupState, dict[str, LocalSource], dict[str, MountedVolume]]:
        local_volumes: dict[DeviceKey, MountedVolume] = {}
        if len({path for _, path in reserved}) != len(reserved):
            raise StateError("Zarezerwowane cele archiwalne kolidują po normalizacji.")
        if any(registered_volume(config, key) != bindings[alias] for alias, key in keys.items()):
            raise StateError("Powiązanie celu archiwalnego zmieniło się przed sprzątaniem.")
        volumes = {alias: stack.enter_context(verify_volume(config, key, writable=True)) for alias, key in keys.items()}
        sources, scans, archive_scans, removals = {}, {}, {}, []
        for owner, collection in sorted(eligible.items()):
            local = _local_mount(config, stack, local_volumes, collection.local_root)
            source = sources[owner] = LocalSource(local, collection.local_root)
            entries = scans[owner] = _entries(source)
            archive_root = root / collection.archive_relative
            observed = {collection.local_root / entry.path: entry for entry in entries.values()
                        if stat.S_ISREG(entry.mode) and _archive_owner(_key(archive_root / entry.path), reserved) == owner}
            if observed != expected[owner]:
                raise StateError("Zawartość lokalnego zbioru zmieniła się od planowania transferów.")
            for alias, volume in volumes.items():
                archive, archived = _archive_entries(volume, archive_root)
                archive_scans[alias, owner] = archived
                for entry in archived.values():
                    path = archive_root / entry.path
                    if (not stat.S_ISREG(entry.mode) or any(is_transfer_part(_name_key(part)) for part in path.parts)
                            or _archive_owner(_key(path), reserved) != owner):
                        continue
                    links = protected.get((bindings[alias].device_id, _key(path)), set())
                    if any(not link.is_relative_to(collection.local_root) for link in links):
                        continue
                    paths = {entry.path, *(link.relative_to(collection.local_root) for link in links)}
                    suffix = re.fullmatch(r"(.+)-[a-fA-F0-9]{8,64}", entry.path.stem)
                    if not links and suffix and config.policy(collection.policy_id).changes == "keep_both":
                        paths.add(entry.path.with_name(suffix[1] + entry.path.suffix))
                    if any(_present(candidate, entries) for candidate in paths):
                        continue
                    if (bindings[alias].volume_id, _key(path)) in pending:
                        raise StateError("Kandydat do usunięcia ma niedokończoną operację BACKUP.")
                    destination = Destination(volume, path)
                    destination.preflight(allow_existing=True)
                    digest = hash_source_file(archive, entry.path, expected=entry)
                    removals.append(Removal(alias, owner, path, tuple(sorted(paths)), entry, digest))
        disks = [volume.physical_device for volume in volumes.values()]
        if len(set(disks)) != len(disks) or set(disks) & {volume.physical_device for volume in local_volumes.values()}:
            raise StateError("Sprzątanie wymaga niezależnych nośników źródła i archiwów.")
        mounts = {**local_volumes, **{keys[alias]: volume for alias, volume in volumes.items()}}
        identity = {key: (volume.mount_id, volume.physical_device) for key, volume in mounts.items()}
        return _CleanupState(scans, archive_scans, identity, tuple(removals)), sources, volumes

    proposed, removed, reconciled = (), [], []
    try:
        with ExitStack() as stack:
            before, _, volumes = capture(stack)
            if removals_to_reconcile:
                connection = stack.enter_context(open_database(database, writable=True))
                for alias, intent in removals_to_reconcile:
                    path = Path(intent.path)
                    owner = _archive_owner(_key(path), reserved)
                    relative = path.relative_to(root / eligible[owner].archive_relative)
                    absent = _key(relative) not in before.archive[alias, owner]
                    if absent:
                        destination = Destination(volumes[alias], path)
                        require_destination_name(destination)
                        destination.sync_absence()
                    with transaction(connection):
                        history.finish_archive_removal(connection, intent.volume_id, intent.path, absent=absent)
                    reconciled.append({"cel": alias, "ścieżka": intent.path,
                                       "stan": "brak potwierdzony" if absent else "ścieżka istnieje"})
        proposed = before.removals
        recovery = {"Uzgodnione zamiary usunięcia": reconciled} if reconciled else {}
        if not proposed:
            return 0, "Brak plików do usunięcia.", {"Plan usunięć": [], **recovery}
        if not approve(proposed):
            return 0, "Zakończono transfery. Lista usunięć nie została zatwierdzona.", {
                "Plan usunięć": [item.report() for item in proposed], "Usunięte pliki": [], **recovery,
            }
        with ExitStack() as stack:
            after, sources, volumes = capture(stack)
            if before != after:
                raise StateError("Stan zbiorów lub nośników zmienił się po zatwierdzeniu listy.")
            connection = stack.enter_context(open_database(database, writable=True))
            with transaction(connection):
                run = history.create_run(connection)
            for item in proposed:
                source = sources[item.collection_id]
                entries = after.local[item.collection_id]
                volume_id = bindings[item.target].volume_id
                path = item.path.as_posix()
                with transaction(connection):
                    history.plan_archive_removal(connection, volume_id, path, run)
                Destination(volumes[item.target], item.path).remove_verified(
                    item.digest, lambda: _check_local_absence(source, entries, item.local_paths),
                    approved_entry=item.entry,
                )
                removed.append(item.report())
                with transaction(connection):
                    history.finish_archive_removal(connection, volume_id, path, absent=True)
    except (ConfigError, StateError, OSError, KeyboardInterrupt) as error:
        interrupted = isinstance(error, KeyboardInterrupt)
        return 130 if interrupted else 3, "Sprzątanie przerwane." if interrupted else "Sprzątanie zatrzymane.", {
            "Plan usunięć": [item.report() for item in proposed], "Usunięte pliki": removed,
            "Uzgodnione zamiary usunięcia": reconciled,
            "Błąd sprzątania": (str(error) if isinstance(error, (ConfigError, StateError)) else
                                "Nie potwierdzono zakończenia sprzątania."),
        }
    return 0, "Zakończono zatwierdzone sprzątanie archiwów.", {
        "Plan usunięć": [item.report() for item in proposed], "Usunięte pliki": removed, **recovery,
    }
