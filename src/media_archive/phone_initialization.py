"""Freeze a phone's initial inventory and compare it against saved content evidence."""

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat
from time import monotonic

from sqlalchemy.engine import Connection

from media_archive._media_formats import PHONE_MEDIA, media_extension
from media_archive.audit import _Scan, _scan
from media_archive.config import ArchiveGroup, ConfigError, Configuration, PhoneProfile
from media_archive.devices import registered_volume
from media_archive.full_audit import load_completed_target
from media_archive.inventory import local_hash_run_id
from media_archive.sources import hash_source_file, open_adb_source
from media_archive.state import DeviceKey, StateError, open_database, transaction, validate_registered_configuration
from media_archive.state.audit import load_full_audit
from media_archive.state.history import (
    archive_destinations, create_run, load_observed_contents, load_source_id, record_content, record_observation,
    record_occurrence,
)
from media_archive.state.initialization import (
    Initialization, finish_initialization, load_initialization, record_baseline_skip,
    record_initialization_progress, start_initialization,
)
from media_archive.state.inventory import scope_file_count
from media_archive.state.locking import Resource, collection_resource, lock_resources

_DECISIONS = {
    "existing_copy": "istniejąca kopia",
    "baseline_skip": "pominięcie bazowe",
    "missing_local": "brak kopii lokalnej",
}


@dataclass(frozen=True)
class PhoneInitializationResult:
    code: int
    message: str
    details: dict[str, object]


def _archive_group(config: Configuration, collection_id: str) -> ArchiveGroup:
    collection = config.collection(collection_id)
    while collection.parent_id is not None:
        collection = config.collection(collection.parent_id)
    for backup in config.backup_profiles:
        if collection.id in backup.collection_ids:
            return config.archive_group(backup.archive_group_id)
    raise ConfigError("Zbiór telefonu nie ma skonfigurowanej grupy archiwalnej.")


def _capture(config: Configuration, phone: PhoneProfile, source_id: int, connection: Connection,
             announce: Callable[[str], None]) -> None:
    if phone.device is None:
        raise StateError("Profil telefonu jest nieaktywny.")
    device = registered_volume(config, DeviceKey("phone", phone.alias))
    if device.device_id is None:
        raise StateError("Telefon wymaga rejestracji.")
    scope = phone.device.camera_dir
    files = []
    with open_adb_source(config, phone.alias) as source:
        scan = _Scan()
        _scan(source, scan)
        if scan.errors:
            raise StateError("Nie ukończono listowania katalogu telefonu: " + next(iter(scan.errors.values())))
        regular = [entry for entry in scan.entries.values() if stat.S_ISREG(entry.mode)]
        supported = [entry for entry in regular if media_extension(entry.path) in PHONE_MEDIA]
        unsupported = [entry.path.as_posix() for entry in regular if media_extension(entry.path) not in PHONE_MEDIA]
        total = sum(entry.size for entry in supported)
        done = 0
        last = monotonic()

        def progress(read: int, _size: int) -> None:
            nonlocal last
            now = monotonic()
            if now - last >= 1:
                announce(f"Telefon {phone.alias} · pliki {len(files)}/{len(supported)} · bajty {done + read}/{total}")
                last = now

        announce(f"Telefon {phone.alias} · do odczytu {len(supported)} plików, {total} bajtów.")
        for entry in supported:
            digest = hash_source_file(source, entry.path, expected=entry, progress=progress)
            files.append((entry, digest, datetime.now(timezone.utc)))
            done += digest.size

    with transaction(connection):
        run_id = create_run(connection)
        start_initialization(
            connection, source_id, run_id, policy=phone.initial_policy,
            boundary=json.dumps({"root": scope.as_posix(), "device_id": device.device_id, "unsupported": unsupported},
                                ensure_ascii=False),
            comparison_scopes=("copies",), processing_scopes=("baseline",),
            baseline_device_id=device.device_id if phone.initial_policy == "baseline_skip_missing" else None,
        )
        for entry, digest, observed_at in files:
            content_id = record_content(connection, digest.sha256, digest.size)
            occurrence = record_occurrence(connection, "source", (scope / entry.path).as_posix(),
                                           content_id=content_id, device_id=device.device_id)
            record_observation(connection, occurrence, run_id, observed_at=observed_at, present=True,
                               size=digest.size, mtime_ns=entry.modified_ns)


def _archive_matches(config: Configuration, phone: PhoneProfile, group: ArchiveGroup, audit_id: str,
                     connection: Connection, wanted: set[int]) -> dict[int, list[dict[str, object]]]:
    run, targets = load_full_audit(connection, audit_id)
    aliases = {item.alias for item in group.targets}
    if run.group_id != group.id or run.root != group.root_relative or any(target.alias not in aliases for target in targets):
        raise StateError("Pełny audyt dotyczy innej pary nośników lub innego korzenia.")
    if not run.distinct_disks:
        raise StateError("Pełny audyt nie potwierdził odrębności fizycznej nośników.")
    audit_root = Path(run.root)
    collection_root = Path(config.archive_mapping(phone.collection_id, group.id))
    relative_scope = collection_root.relative_to(audit_root)
    results: dict[int, list[dict[str, object]]] = defaultdict(list)
    with connection.begin():
        destinations = sorted(archive_destinations(connection, group.id, audit_root),
                              key=lambda item: len(item[1].parts), reverse=True)
    for target in targets:
        device = registered_volume(config, DeviceKey("archive", target.alias, group.id))
        if target.volume_id != device.volume_id:
            raise StateError("Pełny audyt dotyczy poprzedniego woluminu. Wykonaj audyt aktualnej pary.")
        entries, checkpoints = load_completed_target(connection, run.id, target)
        for path in (relative_scope, *relative_scope.parents):
            if path in entries and not stat.S_ISDIR(entries[path].mode):
                raise StateError("Zakres zbioru w pełnym audycie nie wskazuje na katalog.")
        for path, checkpoint in checkpoints.items():
            if checkpoint.content_id not in wanted or not path.is_relative_to(relative_scope):
                continue
            disk_path = audit_root / path
            owner = next((name for name, root in destinations if disk_path.is_relative_to(root)), None)
            if owner == phone.collection_id:
                results[checkpoint.content_id].append({
                    "volume_id": target.volume_id, "path": disk_path.as_posix(), "read_at": checkpoint.read_at,
                })
    return results


def _state(connection: Connection, source_id: int) -> Initialization:
    with connection.begin():
        return load_initialization(connection, source_id)


def _compare_manifest(config: Configuration, phone: PhoneProfile, source_id: int, group: ArchiveGroup,
                      audit_id: str, state: Initialization, connection: Connection) -> None:
    local_run = local_hash_run_id(config, phone.collection_id)
    if local_run is None or scope_file_count(connection, local_run) is None:
        raise StateError("Brak kompletnego spisu lokalnego SHA-256. Użyj inventory --hash-local.")
    if state.started_run_id is None:
        raise StateError("Brak zapisanego spisu telefonu.")
    with connection.begin():
        manifest = load_observed_contents(connection, state.started_run_id, "source")
        local = load_observed_contents(connection, local_run, "local")
    archive = _archive_matches(config, phone, group, audit_id, connection, {row.content_id for row in manifest})
    wanted_local: dict[int, list[int]] = defaultdict(list)
    for row in local:
        if row.collection_id == phone.collection_id:
            wanted_local[row.content_id].append(row.id)

    decisions = []
    for row in manifest:
        local_ids = wanted_local.get(row.content_id, [])
        copies = archive.get(row.content_id, [])
        if local_ids or (copies and state.policy == "baseline_skip_missing"):
            decision = "existing_copy"
        elif state.policy == "baseline_skip_missing":
            decision = "baseline_skip"
        else:
            decision = "missing_local"
        decisions.append({"occurrence_id": row.id, "path": row.path, "decision": decision,
                          "local_occurrence_ids": local_ids, "archive_reads": copies})

    with transaction(connection):
        run_id = create_run(connection)
        evidence = json.dumps({"local_run_id": local_run, "audit_id": audit_id, "files": decisions}, ensure_ascii=False)
        record_initialization_progress(connection, source_id, run_id, phase="comparison", scope="copies",
                                       cursor=evidence, completed=True)
        for item in decisions:
            if item["decision"] == "baseline_skip":
                record_baseline_skip(connection, item["occurrence_id"], run_id)
        if not any(item["decision"] == "missing_local" for item in decisions):
            record_initialization_progress(connection, source_id, run_id, phase="processing", scope="baseline",
                                           completed=True)
            finish_initialization(connection, source_id, run_id)


def initialize_phone(config: Configuration, profile: str, *, audit_id: str | None,
                     announce: Callable[[str], None]) -> PhoneInitializationResult:
    try:
        phone = config.phone(profile)
    except KeyError:
        raise ConfigError("Nie znaleziono skonfigurowanego profilu telefonu.") from None
    if audit_id is not None and not re.fullmatch(r"[0-9a-f]{32}", audit_id):
        raise ConfigError("Podaj ID pełnego audytu z raportu.")
    database = config.app.database_file
    group = _archive_group(config, phone.collection_id)
    with open_database(database) as connection:
        source_id = load_source_id(connection, "phone", profile)
    resources = [Resource("source", str(source_id)), Resource("archive", group.id),
                 collection_resource(config, phone.collection_id)]
    with lock_resources(database, resources):
        validate_registered_configuration(config)
        with open_database(database, writable=True) as connection:
            state = _state(connection, source_id)
            details: dict[str, object] = {"Profil telefonu": profile}
            code = 3
            message = "Zapisano spis telefonu. Podaj ID ukończonego pełnego audytu przez --audit."
            try:
                if state.status == "pending":
                    _capture(config, phone, source_id, connection, announce)
                    state = _state(connection, source_id)
                compared = next((scope for scope in state.scopes
                                 if scope.phase == "comparison" and scope.scope == "copies" and scope.completed), None)
                if compared is None and audit_id is not None and state.status == "in_progress":
                    _compare_manifest(config, phone, source_id, group, audit_id, state, connection)
                    state = _state(connection, source_id)
                    compared = next(scope for scope in state.scopes if scope.phase == "comparison")
                if compared is not None:
                    evidence = json.loads(compared.cursor)
                    files = evidence["files"]
                    details.update({"Spis lokalny SHA-256": evidence["local_run_id"],
                                    "Pełny audyt": evidence["audit_id"], "Pliki bazowe": len(files),
                                    "Rozpoznane pliki": sum(row["decision"] == "existing_copy" for row in files),
                                    "Pominięcia bazowe": sum(row["decision"] == "baseline_skip" for row in files),
                                    "Braki lokalne": sum(row["decision"] == "missing_local" for row in files),
                                    "Decyzje spisu telefonu": [
                                        {"wystąpienie": row["occurrence_id"], "ścieżka": row["path"],
                                         "decyzja": _DECISIONS[row["decision"]],
                                         "lokalne wystąpienia": row["local_occurrence_ids"],
                                         "odczyty archiwalne": [
                                             {"wolumin": read["volume_id"], "ścieżka": read["path"],
                                              "odczytano": read["read_at"]} for read in row["archive_reads"]
                                         ]} for row in files
                                    ]})
                    message = "Porównanie ukończone. Inicjalizacja czeka na uzupełnienie brakujących plików."
                if state.status == "completed":
                    code, message = 0, "Inicjalizacja telefonu ukończona."
            except KeyboardInterrupt:
                code, message = 130, "Przerwano inicjalizację telefonu."
            except StateError as error:
                message = "Inicjalizacja telefonu oczekuje: " + str(error)
            if state.boundary is not None:
                unsupported = json.loads(state.boundary).get("unsupported", [])
                if unsupported:
                    details["Liczba nieobsługiwanych plików telefonu"] = len(unsupported)
                    details["Nieobsługiwane pliki telefonu"] = unsupported
            details.update({"Stan inicjalizacji": state.status, "Polityka": state.policy or phone.initial_policy,
                            "ID spisu telefonu": state.started_run_id})
    return PhoneInitializationResult(code, message, details)
