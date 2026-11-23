"""Check required archive copies and budget peak disk allocation for BACKUP."""

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal
import unicodedata

from media_archive._transfer_files import Destination
from media_archive.config import Configuration
from media_archive.devices import MountedVolume
from media_archive.sources import SourceDigest, hash_source_file
from media_archive.state import StateError, history, open_database

if TYPE_CHECKING:
    from media_archive.backup_planning import _Candidate


_CopyAction = Literal["verified", "copy", "replace", "conflict"]
_ESTIMATED_HISTORY_PAGES_PER_CHECK = 16


@dataclass(frozen=True)
class RequiredPair:
    devices: frozenset[int]
    path: Path


@dataclass(frozen=True)
class BackupCopy:
    source: Path
    target: str
    archive_path: Path
    action: _CopyAction
    required_pair: RequiredPair
    digest: SourceDigest | None


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


def check_backup_capacity(
    config: Configuration, candidates: tuple["_Candidate", ...],
    selected: dict[str, MountedVolume], source_disks: set[str], devices: dict[str, int], proposed_pair: frozenset[int],
    errors: list[dict[str, str]],
) -> tuple[tuple[BackupCopy, ...], set[str], list[dict[str, object]], int]:
    target_disks = {volume.physical_device for volume in selected.values()}
    if len(target_disks) != len(selected) or target_disks & source_disks:
        raise StateError("Cele BACKUP muszą leżeć na różnych fizycznych dyskach, osobnych od źródeł.")
    with open_database(config.app.database_file) as connection:
        assignments = history.load_backup_assignments(connection, {item.file.collection_id for item in candidates})
        page_size = connection.exec_driver_sql("PRAGMA main.page_size").scalar_one()
        pages = connection.exec_driver_sql("PRAGMA main.page_count").scalar_one()
    saved: dict[tuple[Path, int, str], dict[SourceDigest, set[RequiredPair]]] = {}
    for row in assignments:
        contents = saved.setdefault((Path(row.path), row.volume_id, row.collection_id), {})
        digest = SourceDigest(row.sha256, row.size)
        contents.setdefault(digest, set()).add(RequiredPair(
            frozenset((row.first_device_id, row.second_device_id)), Path(row.required_path),
        ))

    try:
        settings_bytes = config.source_file.stat().st_size
    except OSError:
        raise StateError("Nie można oszacować wielkości konfiguracji w snapshocie.") from None
    manifest_bytes = 1024 + 6 * len(str(config.source_file).encode("utf-8"))
    budgets = {}
    for alias, volume in selected.items():
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
    planned_paths: dict[tuple[str, Path], Path] = {}
    planned_directories: set[tuple[str, Path]] = set()
    for item in candidates:
        file = item.file
        try:
            digest = None
            prior = saved.get((file.path, item.volume_id, file.collection_id), {})
            required = set()
            if any(identity.size == file.size for identity in prior):
                digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
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
        for alias in aliases:
            budget = budgets.get(alias)
            if budget is None:
                continue
            try:
                normalized = Path(unicodedata.normalize("NFC", pair.path.as_posix()).casefold())
                key = (alias, normalized)
                parents = {(alias, parent) for parent in normalized.parents}
                if key in planned_paths and planned_paths[key] != file.path:
                    raise StateError("Dwa pliki w zakresie mają tę samą wymaganą ścieżkę archiwalną.")
                if key in planned_directories or parents.intersection(planned_paths):
                    raise StateError("W partii ta sama ścieżka archiwalna jest wymagana jako plik i katalog.")
                planned_paths[key] = file.path
                planned_directories.update(parents)
                destination = Destination(budget.volume, pair.path)
                size = destination.file_size()
                if size == file.size:
                    if digest is None:
                        digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
                    if destination.recover_digest(sync=False) == digest:
                        budget.results["verified"] += 1
                        copies.append(BackupCopy(file.path, alias, pair.path, "verified", pair, digest))
                        continue
                if size is None:
                    action = "copy"
                elif file.changes == "replace_verified":
                    action = "replace"
                else:
                    action = "conflict"
                budget.transfer_parents.update(destination.preflight(allow_existing=True))
                budget.media_bytes += budget.allocated(file.size)
                budget.results[action] += 1
                copies.append(BackupCopy(file.path, alias, pair.path, action, pair, digest))
                if item.month is not None:
                    months.add(item.month)
            except (OSError, StateError) as error:
                _issue(errors, pair.path, error, alias=alias)

    # Every future transfer and recorded check can add history rows before the generation is taken.
    database_bytes = page_size * (pages + _ESTIMATED_HISTORY_PAGES_PER_CHECK * len(copies))
    capacity = []
    for alias, budget in budgets.items():
        snapshot_bytes = (sum(map(budget.allocated, (database_bytes, settings_bytes, manifest_bytes)))
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
    return tuple(copies), months, capacity, outside_selected


def _issue(errors: list[dict[str, str]], path: Path, error: OSError | StateError, *, alias: str | None = None) -> None:
    row = {"ścieżka": str(path), "błąd": str(error) if isinstance(error, StateError) else "Nie można odczytać danych BACKUP."}
    if alias is not None:
        row["cel"] = alias
    errors.append(row)
