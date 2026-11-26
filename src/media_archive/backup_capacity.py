"""Check required archive copies and budget peak disk allocation for BACKUP."""

from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal
import unicodedata

from media_archive._transfer_files import Destination
from media_archive.config import Configuration
from media_archive.devices import MountedVolume
from media_archive.sort_conflicts import _name_key, _validate_name, choose_name, require_destination_name
from media_archive.sources import SourceDigest, hash_source_file
from media_archive.state import StateError, history, open_database, schema_version
from media_archive.state.backup_reads import load_reads

if TYPE_CHECKING:
    from media_archive.backup_planning import _Candidate


_CopyAction = Literal["verified", "copy", "replace", "conflict", "check", "blocked", "known", "review"]
_ESTIMATED_HISTORY_PAGES_PER_CHECK = 16
PINNED_COPY_CONFLICT = "Zapisana ścieżka kopii zawiera inną treść."


@dataclass(frozen=True)
class RequiredPair:
    devices: frozenset[int]
    path: Path
    pinned: tuple[tuple[int, Path], ...] = ()


@dataclass(frozen=True)
class BackupCopy:
    source: Path
    target: str
    archive_path: Path
    action: _CopyAction
    required_pair: RequiredPair
    digest: SourceDigest | None
    elsewhere: tuple[str, ...] = ()
    previous: SourceDigest | None = None


@dataclass
class _Budget:
    volume: MountedVolume
    available: int
    unit: int
    snapshot_parents: int
    media_bytes: int = 0
    transfer_parents: set[Path] = field(default_factory=set)
    results: Counter[str] = field(default_factory=Counter)

    def allocated(self, size: int) -> int:
        return ((size + self.unit - 1) // self.unit) * self.unit


def _path_key(path: Path) -> Path:
    return Path(unicodedata.normalize("NFC", path.as_posix()).casefold())


def snapshot_sizes(config: Configuration, checks: int) -> tuple[int, int, int]:
    with open_database(config.app.database_file) as connection:
        page_size = connection.exec_driver_sql("PRAGMA main.page_size").scalar_one()
        pages = connection.exec_driver_sql("PRAGMA main.page_count").scalar_one()
    try:
        settings = config.source_file.stat().st_size
    except OSError:
        raise StateError("Nie można oszacować wielkości konfiguracji w snapshocie.") from None
    return (page_size * (pages + _ESTIMATED_HISTORY_PAGES_PER_CHECK * checks),
            settings, 1024 + 6 * len(str(config.source_file).encode("utf-8")))


def check_backup_capacity(
    config: Configuration, candidates: tuple["_Candidate", ...],
    selected: dict[str, MountedVolume], source_disks: set[str], devices: dict[str, int], proposed_pair: frozenset[int],
    errors: list[dict[str, str]], *, prepare_replacements: bool = False,
    proof_only: frozenset[str] | set[str] = frozenset(),
    isolate_conflicts: bool = False,
    full_read: bool, prepare_transfers: bool, archive_group: str, archive_root: Path,
    archive_ids: dict[str, int],
) -> tuple[tuple[BackupCopy, ...], set[str], list[dict[str, object]], int]:
    target_disks = {volume.physical_device for volume in selected.values()}
    if len(target_disks) != len(selected) or target_disks & source_disks:
        raise StateError("Cele BACKUP muszą leżeć na różnych fizycznych dyskach, osobnych od źródeł.")
    with open_database(config.app.database_file) as connection:
        if schema_version(connection) < "0015":
            raise StateError("BACKUP wymaga aktualnej bazy. Użyj migrate.")
        assignments = history.load_backup_assignments(connection, {item.file.collection_id for item in candidates})
        reads = {} if full_read or not candidates else load_reads(
            connection, {item.volume_id for item in candidates} | set(archive_ids.values()),
            archive_group=archive_group, archive_root=archive_root,
        )
    saved: dict[tuple[Path, int, str], dict[SourceDigest, set[RequiredPair]]] = {}
    for row in assignments:
        contents = saved.setdefault((Path(row.path), row.volume_id, row.collection_id), {})
        digest = SourceDigest(row.sha256, row.size)
        contents.setdefault(digest, set()).add(RequiredPair(
            frozenset((row.first_device_id, row.second_device_id)), Path(row.required_path),
            tuple((device, Path(path)) for device, path in
                  ((row.first_device_id, row.first_path), (row.second_device_id, row.second_path)) if path is not None),
        ))

    budgets = {}
    for alias, volume in selected.items():
        if alias in proof_only:
            continue
        path = Path(config.app.archive_state_dir) / "snapshot-probe"
        try:
            snapshot = Destination(volume, path)
            available, unit = snapshot.space()
            parents = snapshot.preflight(allow_existing=True)
        except (OSError, StateError) as error:
            _issue(errors, path, error, alias=alias)
        else:
            budgets[alias] = _Budget(volume, available, unit, len(parents))

    copies = []
    months = set()
    outside_selected = 0
    missing_pair = False
    assignments_to_check = []
    reserved_paths: set[tuple[str, Path]] = set()
    reserved_directories: set[tuple[str, Path]] = set()
    planned_paths: dict[tuple[str, Path], Path] = {}
    planned_directories: set[tuple[str, Path]] = set()
    uncertain_paths: set[tuple[str, Path]] = set()
    uncertain_directories: set[tuple[str, Path]] = set()
    for item in candidates:
        file = item.file
        try:
            digest = None
            prior = saved.get((file.path, item.volume_id, file.collection_id), {})
            required = set()
            existing = {identity for identity in prior if identity.size == file.size}
            if full_read and existing:
                digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
            elif not full_read:
                sha256 = reads.get((item.volume_id, file.path, item.entry.volume_fingerprint()))
                if sha256 is not None:
                    digest = SourceDigest(sha256, file.size)
            if existing and digest is None:
                possible = {pair for identity in existing for pair in prior[identity]}
                has_selected_path = False
                for alias in selected:
                    paths = {dict(pair.pinned).get(devices[alias], pair.path)
                             for pair in possible if devices[alias] in pair.devices}
                    if devices[alias] in proposed_pair or not proposed_pair:
                        paths.add(file.archive_path)
                    for path in sorted(paths):
                        has_selected_path = True
                        normalized = _path_key(path)
                        uncertain_paths.add((alias, normalized))
                        uncertain_directories.update((alias, parent) for parent in normalized.parents)
                        copies.append(BackupCopy(file.path, alias, path, "review",
                                                 RequiredPair(frozenset(), path), None))
                outside_selected += int(not has_selected_path)
                continue
            if digest is not None:
                required = prior.get(digest, set())
            if len(required) > 1:
                raise StateError("Ta treść i lokalizacja mają sprzeczne zapisane przypisania archiwalne.")
            if not required and not proposed_pair:
                if not missing_pair:
                    _issue(errors, file.path, StateError("Wybierz parę dla nowych wystąpień przez --pair."))
                    missing_pair = True
                continue
            pair = next(iter(required)) if required else RequiredPair(proposed_pair, file.archive_path)
        except (OSError, StateError) as error:
            _issue(errors, file.path, error)
            continue
        aliases = [alias for alias in selected if devices[alias] in pair.devices]
        if not aliases:
            outside_selected += 1
        assignments_to_check.append((item, pair, digest, aliases))
        pinned_paths = dict(pair.pinned)
        for alias in aliases:
            normalized = _path_key(pinned_paths.get(devices[alias], pair.path))
            reserved_paths.add((alias, normalized))
            reserved_directories.update((alias, parent) for parent in normalized.parents)

    def suffix_reserved(alias: str, path: Path) -> bool:
        normalized = _path_key(path)
        key = (alias, normalized)
        return (key in reserved_paths or key in reserved_directories or key in planned_paths
                or key in planned_directories)

    directory_names: dict[tuple[str, Path], dict[str, list[str]]] = {}
    for item, pair, digest, aliases in assignments_to_check:
        file = item.file
        pinned_paths = dict(pair.pinned)
        for alias in aliases:
            budget = budgets.get(alias)
            if budget is None and alias not in proof_only:
                continue
            pinned = pinned_paths.get(devices[alias])
            path = pinned or pair.path
            try:
                reused = False
                if (full_read and digest is None
                        and (file.changes == "keep_both" or prepare_replacements or alias in proof_only)):
                    digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
                if full_read and budget is not None:
                    destination = Destination(budget.volume, path)
                    if file.changes == "keep_both" and pinned is None:
                        assert digest is not None
                        choice = choose_name(destination, digest, suffix_reserved=lambda name: suffix_reserved(alias, name))
                        path, reused = choice.path, choice.reused
                    else:
                        require_destination_name(destination)
                destination = Destination(selected[alias], path)
                if not full_read:
                    if file.changes == "keep_both" and pinned is None:
                        _validate_name(path.name)
                    parent = (alias, path.parent)
                    if parent not in directory_names:
                        names: dict[str, list[str]] = {}
                        for name in destination.sibling_names(_name_key):
                            names.setdefault(_name_key(name), []).append(name)
                        directory_names[parent] = names
                    matches = directory_names[parent].get(_name_key(path.name), [])
                    if matches and matches != [path.name]:
                        copies.append(BackupCopy(file.path, alias, path, "review", pair, digest))
                        continue
                normalized = _path_key(path)
                key = (alias, normalized)
                parents = {(alias, parent) for parent in normalized.parents}
                if key in planned_paths and planned_paths[key] != file.path:
                    raise StateError("Dwa pliki w zakresie mają tę samą wymaganą ścieżkę archiwalną.")
                if key in planned_directories or parents.intersection(planned_paths):
                    raise StateError("W partii ta sama ścieżka archiwalna jest wymagana jako plik i katalog.")
                planned_paths[key] = file.path
                planned_directories.update(parents)
                if not full_read:
                    entry = destination.file_attributes()
                    if entry is not None:
                        volume_id = archive_ids[alias]
                        previous_sha = reads.get((volume_id, path, entry.volume_fingerprint()))
                        action = ("known" if digest is not None and entry.size == digest.size
                                  and previous_sha == digest.sha256 else "review")
                    elif (budget is None or key in uncertain_paths or key in uncertain_directories
                          or parents.intersection(uncertain_paths)):
                        action = "review"
                    else:
                        action = "copy"
                    if action == "copy":
                        assert budget is not None
                        budget.transfer_parents.update(destination.preflight())
                        budget.media_bytes += budget.allocated(file.size)
                        budget.results[action] += 1
                        if item.month is not None:
                            months.add(item.month)
                    copies.append(BackupCopy(file.path, alias, path, action, pair, digest))
                    continue
                if budget is None:
                    assert digest is not None
                    copies.append(BackupCopy(file.path, alias, path, "check", pair, digest))
                    continue
                size = destination.file_size()
                reused = reused and size == file.size
                previous = None
                read_preimage = prepare_replacements and file.changes == "replace_verified"
                if not reused and size is not None and (size == file.size or read_preimage):
                    if digest is None:
                        digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
                    previous = destination.recover_digest(sync=False)
                    if previous is None:
                        raise StateError("Cel zmienił się podczas planowania BACKUP.")
                    reused = previous == digest
                if reused:
                    budget.results["verified"] += 1
                    copies.append(BackupCopy(file.path, alias, path, "verified", pair, digest))
                    continue
                if file.changes == "keep_both" and pinned is not None and size is not None:
                    if not isolate_conflicts:
                        raise StateError(PINNED_COPY_CONFLICT)
                    copies.append(BackupCopy(file.path, alias, path, "blocked", pair, digest))
                    continue
                if path != pair.path and pinned is None:
                    action = "conflict"
                elif size is None:
                    action = "copy"
                elif file.changes == "replace_verified":
                    action = "replace"
                else:
                    action = "conflict"
                budget.transfer_parents.update(destination.preflight(allow_existing=True))
                budget.media_bytes += budget.allocated(file.size)
                budget.results[action] += 1
                copies.append(BackupCopy(file.path, alias, path, action, pair, digest,
                                         previous=previous if action == "replace" else None))
                if item.month is not None:
                    months.add(item.month)
            except (OSError, StateError) as error:
                _issue(errors, path, error, alias=alias)

    # Every future transfer and recorded check can add history rows before the generation is taken.
    sizes = snapshot_sizes(config, sum(copy.action not in ("known", "review") for copy in copies))
    capacity = []
    for alias, budget in budgets.items():
        snapshot_bytes = (sum(map(budget.allocated, sizes))
                          + (budget.snapshot_parents + 1) * budget.unit)
        transfers = sum(budget.results[action] for action in ("copy", "replace", "conflict"))
        # One work directory is live at a time. Allow one block for each new directory entry.
        work_bytes = (len(budget.transfer_parents) + transfers + int(transfers > 0)) * budget.unit
        required = budget.media_bytes + snapshot_bytes + work_bytes + config.app.min_free_bytes
        capacity.append({
            "cel": alias, "dostępne": budget.available, "pliki": budget.media_bytes, "katalogi_i_części": work_bytes,
            "snapshot": snapshot_bytes, "rezerwa": config.app.min_free_bytes, "wymagane": required,
            "brakuje": max(0, required - budget.available), "zgodne": budget.results["verified"],
            "nowe": budget.results["copy"], "podmiany": budget.results["replace"],
            "warianty_konfliktowe": budget.results["conflict"],
        })
        if required > budget.available:
            errors.append({"cel": alias, "ścieżka": ".", "błąd": "Za mało miejsca na cały BACKUP i wymaganą rezerwę."})
    if prepare_transfers and not errors:
        by_path = {item.file.path: item for item in candidates}
        digests: dict[Path, SourceDigest | None] = {}
        for index, copy in enumerate(copies):
            if copy.action != "copy" or copy.digest is not None:
                continue
            if copy.source not in digests:
                item = by_path[copy.source]
                try:
                    digests[copy.source] = hash_source_file(item.source, item.entry.path, expected=item.entry)
                except (OSError, StateError) as error:
                    _issue(errors, copy.source, error)
                    digests[copy.source] = None
            copies[index] = replace(copy, digest=digests[copy.source])
    return tuple(copies), months, capacity, outside_selected


def _issue(errors: list[dict[str, str]], path: Path, error: OSError | StateError, *, alias: str | None = None) -> None:
    row = {"ścieżka": str(path), "błąd": str(error) if isinstance(error, StateError) else "Nie można odczytać danych BACKUP."}
    if alias is not None:
        row["cel"] = alias
    errors.append(row)
