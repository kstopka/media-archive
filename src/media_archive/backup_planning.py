"""Read-only BACKUP scope, target and capacity planning."""

from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path, PurePosixPath
import stat

from media_archive.backup_capacity import BackupCopy, check_backup_capacity
from media_archive.audit import _Scan, _scan
from media_archive.config import ConfigError, Configuration
from media_archive.config.collections import BackupProfile, LocalCollection
from media_archive.devices import MountedVolume, VolumeUnavailable, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sources import LocalSource, SourceEntry
from media_archive.state import DeviceKey, StateError, history, open_database, validate_registered_configuration
from media_archive.state.locking import installation_lock
from media_archive.state.registry import load_registered_devices


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
    entry: SourceEntry


@dataclass(frozen=True)
class BackupPlan:
    code: int
    message: str
    details: dict[str, object]
    files: tuple[BackupFile, ...] = ()
    copies: tuple[BackupCopy, ...] = ()


@dataclass(frozen=True)
class _Candidate:
    file: BackupFile
    month: str | None
    source: LocalSource
    entry: SourceEntry
    volume_id: int


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
             reserved: tuple[tuple[str, PurePosixPath], ...],
             local_ids: dict[Path, int], errors: list[dict[str, str]]) -> tuple[tuple[_Candidate, ...], set[str]]:
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
                volume = _local_mount(config, stack, mounts, scope)
                source = LocalSource(volume, scope)
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
                               policy.deletions, owner.allow_delete and first is None, entry),
                    month, source, entry, local_ids[volume.root],
                )
    return tuple(files[path] for path in sorted(files)), {volume.physical_device for volume in mounts.values()}


def _issue(errors: list[dict[str, str]], path: Path, error: StateError | OSError, *, alias: str = "") -> None:
    issue = {"ścieżka": str(path), "błąd": str(error) if isinstance(error, StateError) else "Nie udało się odczytać ścieżki."}
    if alias:
        issue["cel"] = alias
    errors.append(issue)


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
                month: str | None = None, targets: tuple[str, ...] = (), pair: tuple[str, ...] = (),
                announce: Callable[[str], None] = lambda _: None, prepare_replacements: bool = False) -> BackupPlan:
    """Select a registered profile and build a read-only candidate manifest."""
    check_application_paths(config)
    first = _month(month) if month is not None else None
    if targets and (profile_id is None or len(targets) > 2 or len(set(targets)) != len(targets)):
        raise ConfigError("Podaj --profile i jeden albo dwa różne cele przez --targets.")
    if pair and (profile_id is None or len(pair) != 2 or len(set(pair)) != 2):
        raise ConfigError("Podaj --profile i dwa różne cele przez --pair.")
    try:
        profiles = (config.backup_profile(profile_id),) if profile_id is not None else config.backup_profiles
        if targets or pair:
            group = config.archive_group(profiles[0].archive_group_id)
            for alias in targets:
                group.target(alias)
            if any(group.target(alias).state != "active" for alias in pair):
                raise ConfigError("Nową parę można wybrać tylko spośród aktywnych celów.")
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
        return _plan_scope(config, stack, profile, selected, collection_id, first, details, pair, announce,
                           prepare_replacements)


def _plan_scope(config: Configuration, stack: ExitStack, profile: BackupProfile,
                selected: dict[str, MountedVolume], collection_id: str | None, first: date | None,
                details: dict[str, object], pair: tuple[str, ...], announce: Callable[[str], None],
                prepare_replacements: bool) -> BackupPlan:
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
    group = config.archive_group(profile.archive_group_id)
    archive_root = Path(group.root_relative)
    bindings = {item.key: item for item in load_registered_devices(config.app.database_file)}
    devices = {key.alias: item.device_id for key, item in bindings.items()
               if key.role == "archive" and key.group_id == group.id and item.device_id is not None}
    local_ids = {}
    for volume in config.local.volumes:
        binding = bindings[DeviceKey("local", volume.alias)]
        if binding.volume_id is not None:
            local_ids[volume.root] = binding.volume_id
    if not devices.keys() >= {target.alias for target in group.targets} or len(local_ids) != len(config.local.volumes):
        raise StateError("Plan BACKUP wymaga zarejestrowanych urządzeń i woluminów.")
    active = tuple(target.alias for target in group.targets if target.state == "active")
    pair = pair or (active if len(active) == 2 else ())
    proposed_pair = frozenset(devices[alias] for alias in pair)
    pair_keys = tuple(DeviceKey("archive", alias, group.id) for alias in pair)
    hardware_ids = [bindings[key].hardware_id for key in pair_keys if bindings[key].hardware_id is not None]
    if len(hardware_ids) != len(set(hardware_ids)):
        raise StateError("Nowa para BACKUP wymaga dwóch różnych fizycznych dysków.")
    pair_disks = []
    for key in pair_keys:
        volume = selected.get(key.alias)
        if volume is None:
            try:
                volume = stack.enter_context(verify_volume(config, key))
            except VolumeUnavailable:
                continue
        pair_disks.append(volume.physical_device)
    if len(pair_disks) != len(set(pair_disks)):
        raise StateError("Nowa para BACKUP wymaga dwóch różnych fizycznych dysków.")
    with open_database(config.app.database_file) as connection:
        reserved = history.archive_destinations(connection, profile.archive_group_id, PurePosixPath(archive_root))
    errors: list[dict[str, str]] = []
    candidates, source_disks = _collect(config, stack, roots, layouts, first, archive_root, reserved, local_ids, errors)
    if source_disks.intersection(pair_disks):
        raise StateError("Nowa para BACKUP wymaga dysków osobnych od źródeł.")
    announce("BACKUP · sprawdzanie kopii i miejsca na wybranych nośnikach")
    copies, pending_months, capacity, outside_selected = check_backup_capacity(
        config, candidates, selected, source_disks, devices, proposed_pair, errors,
        prepare_replacements=prepare_replacements,
    )
    locations = []
    annotated = []
    owners = {item.file.path: item.file.collection_id for item in candidates}
    with open_database(config.app.database_file) as connection:
        for copy in copies:
            if _archive_owner(copy.archive_path, reserved) != owners[copy.source]:
                _issue(errors, copy.archive_path, StateError("Planowana kopia koliduje z zarezerwowanym celem."),
                       alias=copy.target)
            if copy.action == "verified" or copy.digest is None:
                annotated.append(copy)
                continue
            content = history.find_content(connection, copy.digest.sha256, copy.digest.size)
            episode = bindings[DeviceKey("archive", copy.target, group.id)].volume_id
            other = () if content is None else tuple(path for path in history.load_archive_locations(
                connection, episode, content.id,
            ) if path != copy.archive_path.as_posix())
            annotated.append(replace(copy, elsewhere=other))
            if other:
                locations.append({"źródło": str(copy.source), "cel": copy.target, "zapisane ścieżki": list(other)})
    copies = tuple(annotated)
    if locations:
        details["Historyczne kopie pod inną ścieżką"] = locations
    if layouts and (collection_id is not None or first is not None):
        months = sorted(pending_months, reverse=True)
        details["Miesiące z transferami"] = months
        if collection_id is not None and first is None:
            details["Zakresy zbioru"] = ["Wszystko", *months]
    files = tuple(item.file for item in candidates)
    details.update({
        "Zakres": collection_id or "Wszystko",
        "Pliki w zakresie": len(files),
        "Bajty w zakresie": sum(item.size for item in files),
        "Polityki w zakresie": dict(sorted(Counter(item.policy_id for item in files).items())),
        "Para dla nowych plików": list(pair),
        "Pliki przypisane poza wybranymi celami": outside_selected,
        "Pojemność BACKUP": capacity,
        "Błędy planowania": errors,
    })
    if first is not None:
        details["Miesiąc"] = first.isoformat()[:7]
    if errors:
        return BackupPlan(3, "Podgląd BACKUP jest niepełny. Sprawdź błędy odczytu i zakresu.", details, files, copies)
    return BackupPlan(0, "Podgląd zakresu BACKUP gotowy.", details, files, copies)
