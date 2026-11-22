"""Record media history using the caller's SQLite connection and transaction."""

from datetime import datetime, timezone
from pathlib import PurePosixPath
import re
from typing import Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.engine import Connection, RowMapping

from media_archive._encoding import require_utf8

from . import _history_tables as history, _registry_tables as registry
from ._history_queries import find, insert_id, require_row, require_write_transaction
from .errors import StateError


OccurrenceKind = Literal["source", "staging", "local", "archive"]
ReadScope = Literal["full", "partial"]


def _hash(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise StateError("Historia wymaga pełnego skrótu SHA-256.")
    return value.lower()


def _integer(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        raise StateError("Niepoprawna wartość liczbowa w historii.")


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise StateError("Czas zapisu historii wymaga strefy czasowej.")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _path(value: str, *, absolute: bool) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise StateError("Niepoprawna ścieżka wystąpienia.")
    require_utf8(value, StateError)
    path = PurePosixPath(value)
    if path.is_absolute() != absolute or path.anchor == "//" or ".." in path.parts or path.name == "":
        raise StateError("Niepoprawna ścieżka wystąpienia.")
    return path


def archive_destinations(connection: Connection, group_id: str, root: PurePosixPath,
                         ) -> tuple[tuple[str, PurePosixPath], ...]:
    """Read all reserved destinations, including collections absent from current settings."""
    destinations = connection.execute(
        select(registry.archive_mappings.c.collection_id, registry.archive_mappings.c.archive_relative)
        .join_from(registry.archive_mappings, registry.backup_profiles,
                   registry.archive_mappings.c.backup_id == registry.backup_profiles.c.id)
        .where(registry.backup_profiles.c.group_id == group_id)
    )
    return tuple((collection_id, root / relative) for collection_id, relative in destinations)


def _archive_path_owner(connection: Connection, group: RowMapping, path: PurePosixPath) -> str | None:
    """Resolve the owner of a file path and reject paths reserved for directories."""
    matches = []
    for collection_id, destination in archive_destinations(connection, group.id, PurePosixPath(group.root_relative)):
        if destination.is_relative_to(path):
            raise StateError("Ścieżka kopii musi pozostać katalogiem dla zarejestrowanego celu.")
        if path.is_relative_to(destination):
            matches.append((len(destination.parts), collection_id))
    return max(matches)[1] if matches else None


def _device_slot(connection: Connection, device_id: int) -> RowMapping:
    device = require_row(connection, registry.devices, id=device_id)
    return require_row(connection, registry.device_slots, id=device.slot_id)


def create_run(connection: Connection) -> str:
    """Allocate a durable run ID for evidence and the future operation journal."""
    require_write_transaction(connection)
    run_id = uuid4().hex
    connection.execute(history.runs.insert().values(id=run_id))
    return run_id


def record_content(connection: Connection, sha256: str, size: int) -> int:
    """Return the shared identity of the bytes described by a full hash and size."""
    require_write_transaction(connection)
    sha256 = _hash(sha256)
    _integer(size)
    existing = find(connection, history.contents, sha256=sha256, size=size)
    if existing is not None:
        return existing.id
    return insert_id(connection, history.contents, sha256=sha256, size=size)


def record_occurrence(
    connection: Connection, kind: OccurrenceKind, path: str, *, content_id: int | None = None,
    collection_id: str | None = None, device_id: int | None = None, volume_id: int | None = None,
) -> int:
    """Create a distinct occurrence, optionally with unknown content for quick inventory."""
    require_write_transaction(connection)
    if volume_id is not None:
        volume = require_row(connection, registry.volume_episodes, id=volume_id)
        if device_id is not None and device_id != volume.device_id:
            raise StateError("Wolumin nie należy do wskazanego urządzenia.")
        device_id = volume.device_id
    if device_id is not None and volume_id is None and kind != "source":
        raise StateError("Lokalizacja na urządzeniu wymaga wskazania woluminu.")
    slot = _device_slot(connection, device_id) if device_id is not None else None
    source_id = None
    absolute = kind in ("staging", "local")
    if kind == "source":
        if slot is None or slot.role not in ("phone", "card") or slot.source_id is None:
            raise StateError("Wystąpienie źródłowe wymaga urządzenia przypisanego do profilu.")
        absolute = slot.role == "phone"
        if (volume_id is None) != absolute:
            raise StateError("Niepoprawne wskazanie woluminu dla źródła.")
        source_id = slot.source_id
    elif kind == "archive":
        if slot is None or slot.role != "archive" or volume_id is None:
            raise StateError("Wystąpienie archiwalne wymaga woluminu archiwum.")
    elif kind in ("local", "staging"):
        if slot is not None and slot.role != "local":
            raise StateError("Wystąpienie lokalne wymaga woluminu lokalnego.")
    else:
        raise StateError("Nieznany rodzaj wystąpienia.")
    if (kind == "local") != (collection_id is not None):
        raise StateError("ID zbioru jest wymagane wyłącznie dla wystąpienia w zbiorze.")
    if collection_id is not None:
        require_utf8(collection_id, StateError)
    return insert_id(
        connection, history.occurrences, kind=kind, path=str(_path(path, absolute=absolute)),
        content_id=content_id, collection_id=collection_id, source_id=source_id,
        device_id=device_id, volume_id=volume_id,
    )


def record_observation(
    connection: Connection, occurrence_id: int, run_id: str, *, observed_at: datetime,
    present: bool, size: int | None = None, mtime_ns: int | None = None,
) -> int:
    """Record observed presence independently from any verification or transfer."""
    require_write_transaction(connection)
    require_utf8(run_id, StateError)
    if type(present) is not bool or (not present and (size is not None or mtime_ns is not None)):
        raise StateError("Niepoprawne dane obserwacji obecności.")
    if size is not None:
        _integer(size)
    if mtime_ns is not None:
        _integer(mtime_ns, minimum=-(2**63))
    return insert_id(
        connection, history.observations, occurrence_id=occurrence_id, run_id=run_id,
        observed_at=_timestamp(observed_at), present=present, size=size, mtime_ns=mtime_ns,
    )


def record_verification(
    connection: Connection, occurrence_id: int, run_id: str, *, verified_at: datetime,
    scope: ReadScope, read_sha256: str | None = None, bytes_read: int | None = None,
) -> int:
    """Record evidence. Only a full digest and matching byte count can prove a match."""
    require_write_transaction(connection)
    require_utf8(run_id, StateError)
    occurrence = load_occurrence(connection, occurrence_id)
    content = require_row(connection, history.contents, id=occurrence.content_id)
    if scope not in ("full", "partial"):
        raise StateError("Nieznany zakres odczytu weryfikacji.")
    result = "incomplete"
    if bytes_read is not None:
        _integer(bytes_read)
    if read_sha256 is not None:
        read_sha256 = _hash(read_sha256)
    if scope == "full" and read_sha256 is not None and bytes_read is not None:
        result = "match" if (read_sha256, bytes_read) == (content.sha256, content.size) else "mismatch"
    return insert_id(
        connection, history.verifications, occurrence_id=occurrence_id, run_id=run_id,
        verified_at=_timestamp(verified_at), scope=scope, expected_sha256=content.sha256,
        read_sha256=read_sha256, bytes_read=bytes_read, result=result,
    )


def _verified_destination(
    connection: Connection, original: RowMapping, verification_id: int, kind: OccurrenceKind,
) -> RowMapping:
    proof = require_row(connection, history.verifications, id=verification_id)
    target = load_occurrence(connection, proof.occurrence_id)
    if proof.result != "match" or target.kind != kind or target.content_id != original.content_id:
        raise StateError("Wynik wymaga zgodnego pełnego odczytu właściwego wystąpienia.")
    return target


def record_import(connection: Connection, source_occurrence_id: int, verification_id: int) -> int:
    """Record one successful import into the staging occurrence proven by verification."""
    require_write_transaction(connection)
    source = load_occurrence(connection, source_occurrence_id)
    if source.kind != "source":
        raise StateError("Import wymaga wystąpienia źródłowego.")
    staging = _verified_destination(connection, source, verification_id, "staging")
    existing = find(connection, history.imports, staging_occurrence_id=staging.id)
    if existing is not None:
        if (existing.source_occurrence_id, existing.verification_id) == (source.id, verification_id):
            return existing.id
        raise StateError("Wystąpienie w stagingu ma już zapisany import.")
    return insert_id(
        connection, history.imports, source_occurrence_id=source.id,
        staging_occurrence_id=staging.id, verification_id=verification_id,
    )


def record_placement(connection: Connection, import_id: int, verification_id: int) -> None:
    """Record placement of an imported item in its source profile's collection."""
    require_write_transaction(connection)
    imported = require_row(connection, history.imports, id=import_id)
    source = load_occurrence(connection, imported.source_occurrence_id)
    local = _verified_destination(connection, source, verification_id, "local")
    profile = require_row(connection, registry.sources, id=source.source_id)
    if local.collection_id != profile.collection_id:
        raise StateError("Umieszczenie pliku wymaga zbioru przypisanego do profilu.")
    existing = find(connection, history.placements, import_id=import_id)
    if existing is not None:
        if existing.verification_id != verification_id:
            raise StateError("Import ma już zapisane umieszczenie w zbiorze.")
        return
    connection.execute(history.placements.insert().values(
        import_id=import_id, local_occurrence_id=local.id, verification_id=verification_id,
    ))


def assign_archive_pair(
    connection: Connection, local_occurrence_id: int, first_device_id: int, second_device_id: int,
    required_path: str,
) -> None:
    """Pin two registered archive devices and the required disk-relative path."""
    require_write_transaction(connection)
    local = load_occurrence(connection, local_occurrence_id)
    if local.kind != "local" or local.content_id is None or first_device_id == second_device_id:
        raise StateError("Przypisanie wymaga znanej treści w zbiorze i dwóch różnych urządzeń.")
    mapping = require_row(connection, registry.archive_mappings, collection_id=local.collection_id)
    profile = require_row(connection, registry.backup_profiles, id=mapping.backup_id)
    group = require_row(connection, registry.archive_groups, id=profile.group_id)
    required = _path(required_path, absolute=False)
    root = PurePosixPath(group.root_relative) / mapping.archive_relative
    if required == root or not required.is_relative_to(root):
        raise StateError("Ścieżka kopii wykracza poza zarejestrowany cel zbioru.")
    first, second = sorted((first_device_id, second_device_id))
    existing = find(connection, history.pairs, local_occurrence_id=local.id)
    if existing is not None:
        if (existing.first_device_id, existing.second_device_id, existing.required_path) != (
            first, second, str(required),
        ):
            raise StateError("Wystąpienie ma już zapisane wymagane cele archiwalne.")
        return
    if _archive_path_owner(connection, group, required) != local.collection_id:
        raise StateError("Ścieżka kopii należy do innego zarejestrowanego zbioru.")
    slots = [_device_slot(connection, device_id) for device_id in (first, second)]
    if any(slot.role != "archive" or slot.group_id != group.id for slot in slots) or slots[0].id == slots[1].id:
        raise StateError("Para wymaga dwóch różnych celów z przypisanej grupy archiwalnej.")
    connection.execute(history.pairs.insert().values(
        local_occurrence_id=local.id, first_device_id=first, second_device_id=second, required_path=str(required),
    ))


def record_archive_copy(connection: Connection, local_occurrence_id: int, verification_id: int) -> None:
    """Link a verified archive occurrence to its previously recorded requirement."""
    require_write_transaction(connection)
    local = load_occurrence(connection, local_occurrence_id)
    pair = require_row(connection, history.pairs, local_occurrence_id=local.id)
    archive = _verified_destination(connection, local, verification_id, "archive")
    if archive.path != pair.required_path or archive.device_id not in (pair.first_device_id, pair.second_device_id):
        raise StateError("Zweryfikowana kopia nie odpowiada zapisanemu celowi.")
    if find(connection, history.copies, verification_id=verification_id, local_occurrence_id=local.id) is not None:
        return
    connection.execute(history.copies.insert().values(
        local_occurrence_id=local.id, verification_id=verification_id,
    ))


def load_occurrence(connection: Connection, occurrence_id: int) -> RowMapping:
    return require_row(connection, history.occurrences, id=occurrence_id)


def load_occurrences(connection: Connection, kind: OccurrenceKind, path: str) -> tuple[RowMapping, ...]:
    """Read distinct historical occurrences at a path, including reused paths."""
    require_utf8(kind, StateError)
    absolute = kind in ("staging", "local") or (kind == "source" and isinstance(path, str) and path.startswith("/"))
    return tuple(connection.execute(
        history.occurrences.select().filter_by(kind=kind, path=str(_path(path, absolute=absolute)))
        .order_by(history.occurrences.c.id)
    ).mappings())


def load_observed_contents(connection: Connection, run_id: str, kind: OccurrenceKind,
                           ) -> tuple[RowMapping, ...]:
    """Read known-content occurrences positively observed in a specific run."""
    return tuple(connection.execute(
        select(history.occurrences)
        .join_from(history.occurrences, history.observations,
                   history.occurrences.c.id == history.observations.c.occurrence_id)
        .where(history.observations.c.run_id == run_id, history.observations.c.present.is_(True),
               history.occurrences.c.kind == kind, history.occurrences.c.content_id.is_not(None))
        .order_by(history.occurrences.c.id)
    ).mappings())


def load_content(connection: Connection, content_id: int) -> RowMapping:
    return require_row(connection, history.contents, id=content_id)


def load_source_id(connection: Connection, kind: Literal["phone", "camera"], alias: str) -> int:
    """Resolve the stable registered profile ID before recording history or progress."""
    require_utf8(kind, StateError)
    require_utf8(alias, StateError)
    return require_row(connection, registry.sources, kind=kind, alias=alias).id


def load_source_profile(connection: Connection, source_id: int) -> RowMapping:
    return require_row(connection, registry.sources, id=source_id)


def load_observations(connection: Connection, occurrence_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(
        history.observations.select().filter_by(occurrence_id=occurrence_id)
        .order_by(history.observations.c.observed_at, history.observations.c.id)
    ).mappings())


def load_verifications(connection: Connection, occurrence_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(
        history.verifications.select().filter_by(occurrence_id=occurrence_id)
        .order_by(history.verifications.c.verified_at, history.verifications.c.id)
    ).mappings())


def load_imports(connection: Connection, source_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(
        select(*history.imports.c).join_from(
            history.imports, history.occurrences, history.imports.c.source_occurrence_id == history.occurrences.c.id,
        ).where(history.occurrences.c.source_id == source_id).order_by(history.imports.c.id)
    ).mappings())


def load_staged_imports(connection: Connection, source_id: int | None = None) -> tuple[RowMapping, ...]:
    """Read staging locations and placement state alongside each recorded import."""
    source = history.occurrences.alias("import_source")
    staging = history.occurrences.alias("import_staging")
    query = (
        select(history.imports.c.id, source.c.source_id, source.c.content_id, staging.c.id.label("occurrence_id"),
               staging.c.path, staging.c.volume_id, history.contents.c.sha256, history.contents.c.size,
               history.placements.c.import_id.label("placement_id"))
        .select_from(history.imports)
        .join(source, source.c.id == history.imports.c.source_occurrence_id)
        .join(staging, staging.c.id == history.imports.c.staging_occurrence_id)
        .join(history.contents, history.contents.c.id == source.c.content_id)
        .outerjoin(history.placements, history.placements.c.import_id == history.imports.c.id)
        .order_by(history.imports.c.id)
    )
    if source_id is not None:
        query = query.where(source.c.source_id == source_id)
    return tuple(connection.execute(query).mappings())


def load_imported_contents(connection: Connection, source_id: int, *,
                           baseline_paths: set[tuple[str, int, str]] | None = None,
                           device_id: int | None = None) -> dict[tuple[str, int], int]:
    """Map content to its latest successful import in this source profile."""
    query = (
        select(history.contents.c.sha256, history.contents.c.size, history.imports.c.id, history.occurrences.c.path)
        .select_from(history.imports)
        .join(history.occurrences, history.imports.c.source_occurrence_id == history.occurrences.c.id)
        .join(history.contents, history.occurrences.c.content_id == history.contents.c.id)
        .where(history.occurrences.c.source_id == source_id).order_by(history.imports.c.id)
    )
    if device_id is not None:
        query = query.where(history.occurrences.c.device_id == device_id)
    return {(digest, size): import_id for digest, size, import_id, path in connection.execute(query)
            if baseline_paths is None or (digest, size, path) in baseline_paths}


def load_baseline_contents(connection: Connection, run_id: str, source_id: int,
                           ) -> dict[int, tuple[str, int, str]]:
    """Map a profile's observed baseline occurrence IDs to content identities and paths."""
    rows = connection.execute(
        select(history.occurrences.c.id, history.contents.c.sha256, history.contents.c.size, history.occurrences.c.path)
        .select_from(history.observations)
        .join(history.occurrences, history.observations.c.occurrence_id == history.occurrences.c.id)
        .join(history.contents, history.occurrences.c.content_id == history.contents.c.id)
        .where(history.observations.c.run_id == run_id, history.observations.c.present.is_(True),
               history.occurrences.c.kind == "source", history.occurrences.c.source_id == source_id)
    )
    return {occurrence_id: (digest, size, path) for occurrence_id, digest, size, path in rows}


def load_placement(connection: Connection, import_id: int) -> RowMapping | None:
    return find(connection, history.placements, import_id=import_id)


def load_archive_pair(connection: Connection, local_occurrence_id: int) -> RowMapping | None:
    return find(connection, history.pairs, local_occurrence_id=local_occurrence_id)


def load_archive_copies(connection: Connection, local_occurrence_id: int) -> tuple[RowMapping, ...]:
    """Return recorded copy evidence with device and volume IDs, independent of present availability."""
    return tuple(connection.execute(
        select(*history.verifications.c, history.occurrences.c.device_id, history.occurrences.c.volume_id,
               history.occurrences.c.path)
        .join_from(history.copies, history.verifications, history.copies.c.verification_id == history.verifications.c.id)
        .join(history.occurrences, history.verifications.c.occurrence_id == history.occurrences.c.id)
        .where(history.copies.c.local_occurrence_id == local_occurrence_id).order_by(history.verifications.c.id)
    ).mappings())
