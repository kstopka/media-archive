"""Build one verified source manifest for import preview and execution."""

from collections.abc import Callable, Iterable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import stat
from time import monotonic
from typing import Literal

from sqlalchemy.engine import Connection

from media_archive._encoding import require_utf8
from media_archive._media_formats import CAMERA_MEDIA, PHONE_MEDIA, media_extension
from media_archive._transfer_files import Destination, TransferConflict
from media_archive.audit import _Scan, _scan
from media_archive.config import ConfigError, Configuration
from media_archive.devices import registered_volume, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sources import ReadCancelled, hash_source_file, open_adb_source, open_camera_source
from media_archive.state import DeviceKey, StateError, open_database, transaction, validate_registered_configuration
from media_archive.state import history
from media_archive.state.initialization import load_initialization
from media_archive.state.locking import (
    OperationLocks, Resource, canonical_path, installation_lock, lock_resources, require_locks,
)
from media_archive.state.reconciliation import load_reconciliation

ContentIdentity = tuple[str, int]
BaselineGaps = set[tuple[Path, ContentIdentity]]
Decision = Literal["copy", "reimport", "already_imported", "existing_baseline", "baseline_skip", "duplicate",
                   "awaiting_initialization", "ignored", "unsupported", "unreadable", "destination_conflict"]


@dataclass(frozen=True)
class ImportItem:
    relative_path: Path
    size: int
    modified_ns: int
    decision: Decision
    sha256: str | None = None
    destination: Path | None = None
    source_occurrence_id: int | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True)
class ImportPlan:
    profile: str
    kind: Literal["phone", "camera"]
    source_id: int
    items: tuple[ImportItem, ...]
    problems: tuple[str, ...]
    reserve_bytes: int
    available_bytes: int | None = None
    required_bytes: int = 0
    staging_volume_id: int | None = None
    observation_run_id: str | None = None

    @property
    def ready(self) -> bool:
        return not self.problems

    @property
    def transfers(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.destination is not None) if self.ready else ()


@dataclass(frozen=True)
class _Selection:
    kind: Literal["phone", "camera"]
    key: DeviceKey
    history_root: Path
    supported: frozenset[str]
    ignored: frozenset[str] = frozenset()


def _selection(config: Configuration, profile: str, card: str | None, mount: Path | None) -> _Selection:
    if card is not None or mount is not None:
        if card is None or mount is None:
            raise ConfigError("Do planowania importu z karty podaj jednocześnie --card i --mount.")
        try:
            camera = config.camera(profile)
        except KeyError:
            raise ConfigError("Nie znaleziono skonfigurowanego profilu kamery.") from None
        if not any(item.alias == card for item in camera.cards):
            raise ConfigError("Karta nie jest przypisana do wybranego profilu kamery.")
        return _Selection("camera", DeviceKey("card", card), Path(camera.source_relative), CAMERA_MEDIA,
                          frozenset(extension.casefold() for extension in camera.ignore_extensions))
    try:
        phone = config.phone(profile)
    except KeyError:
        if any(camera.alias == profile for camera in config.cameras):
            raise ConfigError("Import z kamery wymaga --card i --mount.")
        raise ConfigError("Nie znaleziono skonfigurowanego profilu źródła.") from None
    if phone.device is None:
        raise ConfigError("Wybrany profil telefonu jest nieaktywny.")
    return _Selection("phone", DeviceKey("phone", profile), phone.device.camera_dir, PHONE_MEDIA)


def _requested_paths(reimport: Iterable[str | Path]) -> frozenset[Path]:
    result = set()
    for value in reimport:
        name = require_utf8(str(value), ConfigError)
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or not path.parts or "\x00" in name:
            raise ConfigError("Ponowny import wymaga ścieżki względem katalogu źródłowego.")
        result.add(path)
    return frozenset(result)


def _baseline(connection: Connection, source_id: int, selection: _Selection, device_id: int,
              requested: frozenset[Path]) -> tuple[dict[ContentIdentity, str], BaselineGaps | None]:
    if selection.kind == "camera":
        return {}, None
    state = load_initialization(connection, source_id)
    comparison = next((scope for scope in state.scopes
                       if scope.phase == "comparison" and scope.scope == "copies" and scope.completed), None)
    if comparison is None:
        raise StateError("Najpierw ukończ porównanie bazowego spisu telefonu przez init-phone.")
    pending = state.status != "completed"
    if pending:
        boundary = json.loads(state.boundary)
        if boundary["device_id"] != device_id or boundary["root"] != selection.history_root.as_posix():
            raise StateError("Uzupełnienie bazowego spisu wymaga tego samego telefonu i katalogu źródłowego.")
        if requested:
            raise StateError("Ponowny import telefonu jest dostępny po zakończeniu jego inicjalizacji.")
    identities = history.load_baseline_contents(connection, state.started_run_id, source_id)
    decisions = {}
    gaps: BaselineGaps | None = set() if pending else None
    for row in json.loads(comparison.cursor)["files"]:
        digest, size, name = identities[row["occurrence_id"]]
        identity = digest, size
        decision = row["decision"]
        decisions[identity] = decision
        if gaps is not None and decision == "missing_local":
            gaps.add((Path(name).relative_to(selection.history_root), identity))
    return decisions, gaps


def _read_manifest(config: Configuration, profile: str, selection: _Selection, mount: Path | None,
                   requested: frozenset[Path], announce: Callable[[str], None],
                   cancelled: Callable[[], bool] | None) -> tuple[list[ImportItem], list[str]]:
    opener = (open_adb_source(config, profile) if selection.kind == "phone"
              else open_camera_source(config, profile, selection.key.alias, mount))
    items = []
    with opener as source:
        scan = _Scan()
        _scan(source, scan)
        problems = ["Nie ukończono listowania źródła: " + error for error in scan.errors.values()]
        entries = sorted((entry for entry in scan.entries.values() if not stat.S_ISDIR(entry.mode)),
                         key=lambda entry: (entry.path not in requested, entry.path))
        announce(f"Źródło {profile} · wpisy do sprawdzenia: {len(entries)}.")
        last = monotonic()

        def progress(read: int, total: int) -> None:
            nonlocal last
            now = monotonic()
            if now - last >= 1:
                announce(f"Źródło {profile} · wpis {index}/{len(entries)} · bajty pliku {read}/{total}.")
                last = now

        for index, entry in enumerate(entries, start=1):
            if cancelled is not None and cancelled():
                raise ReadCancelled("Anulowano planowanie importu.")
            extension = media_extension(entry.path)
            if extension in selection.ignored:
                decision = "ignored"
            elif not stat.S_ISREG(entry.mode) or extension not in selection.supported:
                decision = "unsupported"
            else:
                try:
                    digest = hash_source_file(source, entry.path, expected=entry, progress=progress, cancelled=cancelled)
                except ReadCancelled:
                    raise
                except StateError as error:
                    items.append(ImportItem(entry.path, entry.size, entry.modified_ns, "unreadable"))
                    problems.append(str(error))
                else:
                    items.append(ImportItem(entry.path, digest.size, entry.modified_ns, "copy", digest.sha256,
                                            observed_at=datetime.now(timezone.utc)))
                continue
            items.append(ImportItem(entry.path, entry.size, entry.modified_ns, decision))
    return items, problems


def _decisions(config: Configuration, plan: ImportPlan, imported: dict[ContentIdentity, int],
               baseline: dict[ContentIdentity, str], gaps: BaselineGaps | None,
               requested: frozenset[Path]) -> ImportPlan:
    items = []
    problems = list(plan.problems)
    accepted = set()
    seen = set()
    root = config.local.staging_root / plan.kind / str(plan.source_id)
    for item in plan.items:
        if item.sha256 is None:
            items.append(item)
            continue
        identity = item.sha256, item.size
        prior = imported.get(identity)
        disposition = baseline.get(identity)
        selected = item.relative_path in requested
        if selected and (prior is not None or disposition in ("existing_copy", "baseline_skip")):
            accepted.add(item.relative_path)
            decision = "reimport"
        elif prior is not None:
            decision = "already_imported"
        elif disposition == "existing_copy":
            decision = "existing_baseline"
        elif disposition == "baseline_skip":
            decision = "baseline_skip"
        elif gaps is not None and (item.relative_path, identity) not in gaps:
            decision = "awaiting_initialization"
        else:
            decision = "copy"
        destination = None
        if decision in ("copy", "reimport"):
            if identity in seen:
                decision = "duplicate"
            else:
                seen.add(identity)
                directory = root / item.sha256
                if selected and decision == "reimport":
                    directory /= f"reimport-{prior if prior is not None else 'baseline'}"
                destination = directory / item.relative_path.name
        items.append(replace(item, decision=decision, destination=destination))
    if requested - accepted:
        problems.append("Ponowny import wymaga dokładnej ścieżki do odczytanego pliku znanego w tym profilu.")
    if gaps is not None and not any(item.destination is not None for item in items):
        problems.append("Inicjalizacja telefonu pozostaje otwarta; nie ma dostępnych plików do uzupełnienia.")
    return replace(plan, items=tuple(items), problems=tuple(dict.fromkeys(problems)))


def _preflight(config: Configuration, plan: ImportPlan) -> ImportPlan:
    staging = config.local.staging_root
    volume = next((volume for volume in config.local.volumes if staging.is_relative_to(volume.root)), None)
    if volume is None:
        return replace(plan, problems=(*plan.problems, "Staging musi leżeć na zarejestrowanym woluminie lokalnym."))
    key = DeviceKey("local", volume.alias)
    try:
        volume_id = registered_volume(config, key).volume_id
        with verify_volume(config, key, writable=True) as mounted:
            available, unit = Destination(mounted, staging.relative_to(volume.root) / "probe").space()
            required = 0
            new_parents: set[Path] = set()
            items = []
            problems = []
            for item in plan.items:
                if item.destination is not None:
                    required += (((item.size + unit - 1) // unit) + 1) * unit  # File and work directory.
                    try:
                        destination = Destination(mounted, item.destination.relative_to(volume.root))
                        new_parents.update(destination.preflight())
                    except TransferConflict:
                        item = replace(item, decision="destination_conflict")
                        problems.append("Zaplanowany plik stagingu już istnieje; nie zostanie nadpisany.")
                items.append(item)
            required += len(new_parents) * unit
            if available < required + plan.reserve_bytes:
                problems.append("Za mało wolnego miejsca na całą partię i wymaganą rezerwę stagingu.")
            return replace(plan, items=tuple(items), problems=tuple(dict.fromkeys(problems)),
                           available_bytes=available, required_bytes=required, staging_volume_id=volume_id)
    except StateError as error:
        return replace(plan, problems=(str(error),))
    except OSError:
        return replace(plan, problems=("Nie można bezpiecznie sprawdzić katalogów i miejsca stagingu.",))


def _record(connection: Connection, plan: ImportPlan, selection: _Selection, device_id: int,
            volume_id: int | None) -> ImportPlan:
    with transaction(connection):
        run_id = history.create_run(connection)
        result = []
        for item in plan.items:
            if item.sha256 is not None:
                content_id = history.record_content(connection, item.sha256, item.size)
                occurrence = history.record_occurrence(
                    connection, "source", (selection.history_root / item.relative_path).as_posix(),
                    content_id=content_id, device_id=device_id, volume_id=volume_id,
                )
                history.record_observation(connection, occurrence, run_id, observed_at=item.observed_at, present=True,
                                           size=item.size, mtime_ns=item.modified_ns)
                item = replace(item, source_occurrence_id=occurrence)
            result.append(item)
    return replace(plan, items=tuple(result), observation_run_id=run_id)


def plan_import(config: Configuration, profile: str, *, card: str | None = None, mount: Path | None = None,
                reimport: Iterable[str | Path] = (), record: bool = False,
                locks: OperationLocks | None = None,
                announce: Callable[[str], None] = lambda _: None,
                cancelled: Callable[[], bool] | None = None) -> ImportPlan:
    """Build the same plan for a preview or a caller that records source observations."""
    selection = _selection(config, profile, card, mount)
    requested = _requested_paths(reimport)
    database = config.app.database_file
    check_application_paths(config, device_mount=mount)
    with installation_lock(database):
        validate_registered_configuration(config)
        with open_database(database) as connection:
            source_id = history.load_source_id(connection, selection.kind, profile)
        resources = [Resource("source", str(source_id))]
        if locks is not None:
            require_locks(locks, canonical_path(database), resources)
        ownership = lock_resources(database, resources) if record and locks is None else nullcontext()
        with ownership, open_database(database, writable=record) as connection:
            device = registered_volume(config, selection.key)
            plan = ImportPlan(profile, selection.kind, source_id, (), (), config.app.min_free_bytes)
            with connection.begin():
                if load_reconciliation(connection) is not None:
                    return replace(plan, problems=("Przed planowaniem importu uzgodnij stan po przywróceniu bazy.",))
                try:
                    baseline, gaps = _baseline(connection, source_id, selection, device.device_id, requested)
                except StateError as error:
                    return replace(plan, problems=(str(error),))
                imported = history.load_imported_contents(connection, source_id)
            items, problems = _read_manifest(config, profile, selection, mount, requested, announce, cancelled)
            plan = _decisions(config, replace(plan, items=tuple(items), problems=tuple(problems)),
                              imported, baseline, gaps, requested)
            if plan.ready and any(item.destination is not None for item in plan.items):
                plan = _preflight(config, plan)
            if record and plan.ready and any(item.sha256 is not None for item in plan.items):
                return _record(connection, plan, selection, device.device_id, device.volume_id)
            return plan
