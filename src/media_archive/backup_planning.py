"""Read-only BACKUP scope selection and monthly work discovery."""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
import stat

from media_archive._transfer_files import Destination
from media_archive.audit import _Scan, _scan
from media_archive.config import ConfigError, Configuration
from media_archive.config.collections import BackupProfile, LocalCollection
from media_archive.devices import MountedVolume, VolumeUnavailable, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sources import LocalSource, SourceEntry, hash_source_file
from media_archive.state import DeviceKey, StateError, history, open_database, validate_registered_configuration
from media_archive.state.locking import installation_lock


_YEAR_FOLDER_PROBLEM = "Katalog roku zastępuje wpis innego typu."
_DATE_FOLDER_PROBLEM = "Katalog daty zastępuje wpis innego typu."


@dataclass(frozen=True)
class BackupFile:
    path: Path
    archive_path: Path
    collection_id: str
    size: int
    policy_id: str
    changes: str
    deletions: str
    allow_delete: bool


@dataclass(frozen=True)
class BackupPlan:
    code: int
    message: str
    details: dict[str, object]
    files: tuple[BackupFile, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    file: BackupFile
    month: str | None
    source: LocalSource
    entry: SourceEntry


def _month(value: str) -> date:
    try:
        first = date.fromisoformat(f"{value}-01")
        if first.isoformat() == f"{value}-01":
            return first
    except ValueError:
        pass
    raise ConfigError("Podaj miesiąc w formacie RRRR-MM.")


def _layout_year(layout: LocalCollection, year: int) -> str:
    return f"{year:04d}" if layout.layout == "year_month" else f"Różne {year:04d}"


def _layout_month(layout: LocalCollection, year: str, folder: str) -> str | None:
    try:
        if layout.layout == "year_month":
            day = date.fromisoformat(f"{folder.replace('.', '-')}-01")
            expected = day.isoformat()[:7].replace("-", ".")
        else:
            day = date.fromisoformat(folder)
            expected = day.isoformat()
    except ValueError:
        return None
    if year == _layout_year(layout, day.year) and folder == expected:
        return day.isoformat()[:7]
    return None


def _month_for(path: Path, layouts: tuple[LocalCollection, ...]) -> str | None:
    for layout in layouts:
        if not path.is_relative_to(layout.local_root):
            continue
        parts = path.relative_to(layout.local_root).parts
        if len(parts) < 3:
            continue
        month = _layout_month(layout, *parts[:2])
        if month is not None:
            return month
    return None


def _date_folder_problem(path: Path, layouts: tuple[LocalCollection, ...]) -> str | None:
    for layout in layouts:
        if not path.is_relative_to(layout.local_root):
            continue
        parts = path.relative_to(layout.local_root).parts
        if len(parts) == 1:
            year = parts[0].removeprefix("Różne ")
            if len(year) == 4 and year.isascii() and year.isdecimal():
                number = int(year)
                if number > 0 and parts[0] == _layout_year(layout, number):
                    return _YEAR_FOLDER_PROBLEM
        if len(parts) == 2 and _layout_month(layout, *parts) is not None:
            return _DATE_FOLDER_PROBLEM
    return None


def _month_folders(volume: MountedVolume, layout: LocalCollection, first: date,
                   errors: list[dict[str, str]]) -> Iterator[Path]:
    source = LocalSource(volume, layout.local_root)
    directory = _layout_year(layout, first.year)
    year = next((entry for entry in source.list_directory() if entry.path.name == directory), None)
    if year is None:
        return
    if not stat.S_ISDIR(year.mode):
        path = layout.local_root / year.path
        errors.append({"ścieżka": str(path), "błąd": _YEAR_FOLDER_PROBLEM})
        return
    month = first.isoformat()[:7]
    for entry in source.list_directory(Path(directory)):
        if _layout_month(layout, directory, entry.path.name) != month:
            continue
        path = layout.local_root / entry.path
        if stat.S_ISDIR(entry.mode):
            yield path
        else:
            errors.append({"ścieżka": str(path), "błąd": _DATE_FOLDER_PROBLEM})


def _local_mount(config: Configuration, stack: ExitStack, mounts: dict[DeviceKey, MountedVolume],
                 path: Path) -> MountedVolume:
    volume = next(item for item in config.local.volumes if path.is_relative_to(item.root))
    key = DeviceKey("local", volume.alias)
    if key not in mounts:
        mounts[key] = stack.enter_context(verify_volume(config, key))
    return mounts[key]


def _archive_owner(path: Path, reserved: tuple[tuple[str, PurePosixPath], ...]) -> str | None:
    archive = PurePosixPath(path)
    if any(root.is_relative_to(archive) for _, root in reserved):
        return None
    return max(((len(root.parts), key) for key, root in reserved if archive.is_relative_to(root)),
               default=(0, None))[1]


def _collect(config: Configuration, stack: ExitStack, roots: tuple[LocalCollection, ...],
             layouts: tuple[LocalCollection, ...], first: date | None, archive_root: Path,
             reserved: tuple[tuple[str, PurePosixPath], ...], errors: list[dict[str, str]]) -> tuple[_Candidate, ...]:
    mounts: dict[DeviceKey, MountedVolume] = {}
    files = {}
    for root in roots:
        dated = tuple(item for item in layouts if item.local_root.is_relative_to(root.local_root))
        scopes = []
        if first is None:
            scopes.append(root.local_root)
        else:
            for layout in dated:
                try:
                    volume = _local_mount(config, stack, mounts, layout.local_root)
                    scopes.extend(_month_folders(volume, layout, first, errors))
                except (StateError, OSError) as error:
                    _issue(errors, layout.local_root, error)
        for scope in sorted(set(scopes)):
            try:
                source = LocalSource(_local_mount(config, stack, mounts, scope), scope)
                scan = _Scan()
                _scan(source, scan)
            except (StateError, OSError) as error:
                _issue(errors, scope, error)
                continue
            for path, error in scan.errors.items():
                errors.append({"ścieżka": str(scope / path), "błąd": error})
            if first is None:
                for item in config.collections:
                    if item.local_root != scope and item.local_root.is_relative_to(scope):
                        relative = item.local_root.relative_to(scope)
                        if relative not in scan.observed_at and scan.covers(relative):
                            errors.append({"ścieżka": str(item.local_root), "błąd": "Brak katalogu zbioru."})
            for entry in scan.entries.values():
                path = scope / entry.path
                if stat.S_ISDIR(entry.mode):
                    continue
                if first is None:
                    problem = _date_folder_problem(path, dated)
                    if problem is not None:
                        errors.append({"ścieżka": str(path), "błąd": problem})
                        continue
                if not stat.S_ISREG(entry.mode):
                    errors.append({"ścieżka": str(path), "błąd": "Wpis nie jest zwykłym plikiem ani katalogiem."})
                    continue
                owner = config.collection_for_path(root.id, path)
                policy = config.policy(owner.policy_id)
                destination = archive_root / owner.archive_relative / path.relative_to(owner.local_root)
                if _archive_owner(destination, reserved) != owner.id:
                    errors.append({"ścieżka": str(path), "błąd": "Ścieżka koliduje z zarezerwowanym celem archiwalnym."})
                    continue
                month = _month_for(path, dated)
                if first is not None and month != first.isoformat()[:7]:
                    continue
                files[path] = _Candidate(
                    BackupFile(path, destination, owner.id, entry.size, policy.id, policy.changes,
                               policy.deletions, owner.allow_delete and first is None),
                    month, source, entry,
                )
    return tuple(files[path] for path in sorted(files))


def _issue(errors: list[dict[str, str]], path: Path, error: StateError | OSError, *, alias: str = "") -> None:
    issue = {"ścieżka": str(path), "błąd": str(error) if isinstance(error, StateError) else "Nie udało się odczytać ścieżki."}
    if alias:
        issue["cel"] = alias
    errors.append(issue)


def _pending_months(candidates: tuple[_Candidate, ...], targets: dict[str, MountedVolume],
                    errors: list[dict[str, str]], announce: Callable[[str], None]) -> list[str]:
    dated: dict[str, list[_Candidate]] = {}
    for candidate in candidates:
        if candidate.month is not None:
            dated.setdefault(candidate.month, []).append(candidate)
    pending = []
    for month, files in sorted(dated.items(), reverse=True):
        announce(f"BACKUP · sprawdzanie miesiąca {month}")
        if _month_needs_work(files, targets, errors):
            pending.append(month)
    return pending


def _month_needs_work(files: list[_Candidate], targets: dict[str, MountedVolume],
                      errors: list[dict[str, str]]) -> bool:
    comparisons = []
    for item in files:
        same_size = []
        for alias, volume in targets.items():
            path = item.file.archive_path
            try:
                destination = Destination(volume, path)
                if destination.file_size() != item.entry.size:
                    return True
                same_size.append((alias, destination))
            except (StateError, OSError) as error:
                _issue(errors, path, error, alias=alias)
        if same_size:
            comparisons.append((item, same_size))
    for item, same_size in comparisons:
        try:
            source_digest = hash_source_file(item.source, item.entry.path, expected=item.entry)
        except (StateError, OSError) as error:
            _issue(errors, item.file.path, error)
            continue
        for alias, destination in same_size:
            try:
                if destination.recover_digest(sync=False) != source_digest:
                    return True
            except (StateError, OSError) as error:
                _issue(errors, item.file.archive_path, error, alias=alias)
    return False


def _probe_targets(config: Configuration, stack: ExitStack, group_ids: set[str],
                   aliases: tuple[str, ...]) -> tuple[dict[str, dict[str, MountedVolume]], list[dict[str, str]]]:
    connected: dict[str, dict[str, MountedVolume]] = {}
    status = []
    for group in config.archive_groups:
        if group.id not in group_ids:
            continue
        connected[group.id] = available = {}
        for target in group.targets:
            row = {"grupa": group.id, "cel": target.alias}
            status.append(row)
            if aliases and target.alias not in aliases:
                row["stan"] = "niewybrany"
            elif target.state == "read_only":
                row["stan"] = "tylko do odczytu"
            else:
                try:
                    key = DeviceKey("archive", target.alias, group.id)
                    available[target.alias] = stack.enter_context(verify_volume(config, key, writable=True))
                except VolumeUnavailable:
                    row["stan"] = "odłączony"
                except (ConfigError, StateError) as error:
                    row.update(stan="niedostępny do zapisu", uwaga=str(error))
                else:
                    row["stan"] = "dostępny"
    return connected, status


def plan_backup(config: Configuration, *, profile_id: str | None = None, collection_id: str | None = None,
                month: str | None = None, targets: tuple[str, ...] = (),
                announce: Callable[[str], None] = lambda _: None) -> BackupPlan:
    """Select a registered profile and build a read-only candidate manifest."""
    check_application_paths(config)
    first = _month(month) if month is not None else None
    if targets and (profile_id is None or len(targets) > 2 or len(set(targets)) != len(targets)):
        raise ConfigError("Podaj --profile i jeden albo dwa różne cele przez --targets.")
    try:
        profiles = (config.backup_profile(profile_id),) if profile_id is not None else config.backup_profiles
        if targets:
            group = config.archive_group(profiles[0].archive_group_id)
            for alias in targets:
                group.target(alias)
    except KeyError:
        raise ConfigError("Wybierz skonfigurowany profil i cele z jego grupy.") from None
    database = config.app.database_file
    with installation_lock(database), ExitStack() as stack:
        validate_registered_configuration(config)
        connected, target_status = _probe_targets(config, stack, {item.archive_group_id for item in profiles}, targets)
        matching = tuple(profile for profile in profiles if connected[profile.archive_group_id])
        details: dict[str, object] = {"Profile BACKUP": [item.id for item in matching], "Cele BACKUP": target_status}
        if not matching:
            return BackupPlan(3, "Brak dostępnego, zatwierdzonego celu BACKUP.", details)
        if len(matching) > 1:
            return BackupPlan(0, "Kilka profili pasuje do nośników. Wybierz jeden przez --profile.", details)
        profile, = matching
        selected = connected[profile.archive_group_id]
        details["Profil BACKUP"] = profile.id
        details["Wybrane cele"] = list(selected)
        if len(selected) > 2 or (targets and set(targets) != set(selected)):
            return BackupPlan(3, "Wybierz jeden albo dwa dostępne cele przez --profile i --targets.", details)
        return _plan_scope(config, stack, profile, selected, collection_id, first, details, announce)


def _plan_scope(config: Configuration, stack: ExitStack, profile: BackupProfile,
                selected: dict[str, MountedVolume], collection_id: str | None, first: date | None,
                details: dict[str, object], announce: Callable[[str], None]) -> BackupPlan:
    roots = config.profile_collections(profile.id)
    details["Zakresy BACKUP"] = [{"zbiór": "Wszystko", "label": profile.label}, *(
        {"zbiór": item.id, "label": item.label, "miesiące": bool(config.month_collections(item.id))} for item in roots
    )]
    if collection_id is not None:
        roots = tuple(root for root in roots if root.id == collection_id)
        if not roots:
            raise ConfigError("Wybierz zbiór przypisany bezpośrednio do wybranego profilu BACKUP.")
    layouts = tuple(sorted((layout for root in roots for layout in config.month_collections(root.id)),
                           key=lambda layout: -len(layout.local_root.parts)))
    if first is not None and not layouts:
        raise ConfigError("Wybrany zakres BACKUP nie udostępnia miesięcy.")
    archive_root = Path(config.archive_group(profile.archive_group_id).root_relative)
    with open_database(config.app.database_file) as connection:
        reserved = history.archive_destinations(connection, profile.archive_group_id, PurePosixPath(archive_root))
    errors: list[dict[str, str]] = []
    candidates = _collect(config, stack, roots, layouts, first, archive_root, reserved, errors)
    if layouts and (collection_id is not None or first is not None):
        months = _pending_months(candidates, selected, errors, announce)
        details["Miesiące z transferami"] = months
        if collection_id is not None and first is None:
            details["Zakresy zbioru"] = ["Wszystko", *months]
    files = tuple(item.file for item in candidates)
    details.update({
        "Zakres": collection_id or "Wszystko",
        "Pliki w zakresie": len(files),
        "Bajty w zakresie": sum(item.size for item in files),
        "Polityki w zakresie": dict(sorted(Counter(item.policy_id for item in files).items())),
        "Błędy planowania": errors,
    })
    if first is not None:
        details["Miesiąc"] = first.isoformat()[:7]
    if errors:
        return BackupPlan(3, "Podgląd BACKUP jest niepełny. Sprawdź błędy odczytu i zakresu.", details, files)
    return BackupPlan(0, "Podgląd zakresu BACKUP gotowy.", details, files)
