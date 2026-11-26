"""Durable operation attempts under process locks and short SQLite transactions."""

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Literal

from sqlalchemy import column, func, select, table
from sqlalchemy.engine import Connection, RowMapping

from media_archive._encoding import require_utf8
from media_archive.config import Configuration
from media_archive.config.validation import validate_relationships

from . import _history_tables as history, _registry_tables as registry
from ._history_queries import find, insert_id, require_row, require_write_transaction
from .errors import StateError
from .locking import OperationLocks, Resource, canonical_path, collection_resource, require_locks
from .migrations import schema_version
from .reconciliation import require_reconciled
from .sort_moves import moves as sort_moves
from .registry import _validate_registered_configuration

operations = table("journal_operations", column("id"), column("operation_key"), column("intent"),
                   column("target_occurrence_id"), schema="main")
resources = table("journal_resources", column("operation_id"), column("kind"), column("resource_id"), schema="main")
attempts = table("journal_attempts", column("id"), column("operation_id"), column("run_id"),
                 column("owner_token"), column("state"), column("reconciling"), column("verification_id"),
                 column("verification_floor"), column("observation_floor"), schema="main")
events = table("journal_events", column("id"), column("attempt_id"), column("state"), column("recorded_at"),
               column("error_code"), column("observation_id"), schema="main")
superseded_backups = table("journal_superseded_backups", column("operation_id"), column("verification_id"),
                           schema="main")

_NEXT = {
    "planned": "transferring",
    "transferring": "transferred",
    "transferred": "verifying",
    "verifying": "verified",
    "verified": "published",
}
_ACTIVE = (*_NEXT, "published")
_STOPPED = ("interrupted", "failed", "conflict", "needs_review")
_REGISTRIES = {"archive": registry.archive_groups, "source": registry.sources}
BACKUP_REPLACE_PREFIX = "backup:replace:"


def backup_retry_command(operation: RowMapping) -> str | None:
    if operation.operation_key.startswith("backup:"):
        return "backup --yes --full-read"
    return None


def _may_have_published(attempt: RowMapping) -> bool:
    return bool(attempt.reconciling) or attempt.state in ("verified", "published")


def recommended_recovery(attempt: RowMapping | None) -> Literal["restart", "reconcile"] | None:
    """Choose a recovery mode from the recorded state, without checking lock ownership."""
    if attempt is None:
        return None
    if attempt.state == "needs_review":
        return "reconcile"
    if attempt.state in _ACTIVE:
        return "reconcile" if _may_have_published(attempt) else "restart"
    return None


def _event(connection: Connection, attempt_id: int, state: str, error_code: str | None = None,
           observation_id: int | None = None) -> None:
    connection.execute(events.insert().values(
        attempt_id=attempt_id, state=state, recorded_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        error_code=error_code, observation_id=observation_id,
    ))


def _set_state(connection: Connection, attempt: RowMapping, state: str, *,
               verification_id: int | None = None, error_code: str | None = None) -> None:
    with connection.begin_nested():
        connection.execute(attempts.update().filter_by(id=attempt.id).values(
            state=state, verification_id=verification_id if verification_id is not None else attempt.verification_id,
        ))
        _event(connection, attempt.id, state, error_code)


def _require_locks(
    connection: Connection, locks: OperationLocks, requested: Iterable[Resource],
) -> str:
    database: Path = connection.info["state_path"]
    try:
        canonical = database.resolve(strict=True)
    except (OSError, RuntimeError):
        raise StateError("Nie można ustalić położenia bazy dla blokad.") from None
    return require_locks(locks, canonical, requested)


def _operation_resources(connection: Connection, operation_id: int) -> frozenset[Resource]:
    return frozenset(Resource(row.kind, row.resource_id) for row in connection.execute(
        resources.select().filter_by(operation_id=operation_id)
    ).mappings())


def plan_operation(
    connection: Connection, locks: OperationLocks, config: Configuration, *, operation_key: str, intent: str,
    target_occurrence_id: int, required_resources: Iterable[Resource],
) -> int:
    """Record one immutable intent. Reusing its stable key requires the same plan."""
    require_write_transaction(connection)
    require_reconciled(connection)
    require_utf8(operation_key, StateError)
    require_utf8(intent, StateError)
    requested = frozenset(required_resources)
    if not operation_key.strip() or not intent.strip() or not requested:
        raise StateError("Operacja wymaga klucza, zamiaru i listy zasobów.")
    _require_locks(connection, locks, requested)
    _require_configuration(connection, config)
    target = require_row(connection, history.occurrences, id=target_occurrence_id)
    if target.kind not in ("staging", "local", "archive", "undated") or target.content_id is None:
        raise StateError("Cel operacji wymaga znanej treści i wystąpienia docelowego.")
    if target.kind == "archive":
        device = require_row(connection, registry.devices, id=target.device_id)
        slot = require_row(connection, registry.device_slots, id=device.slot_id)
        if Resource("archive", slot.group_id) not in requested:
            raise StateError("Cel kopii wymaga blokady swojej grupy archiwalnej.")
    collection_roots = {str(canonical_path(item.local_root)) for item in config.collections if item.parent_id is None}
    collection_roots.update(str(canonical_path(path)) for path in (config.local.staging_root, config.local.undated_root))
    for resource in requested:
        if resource.kind == "collection":
            if resource.id not in collection_roots:
                raise StateError("Blokada zbioru musi wskazywać ścieżkę jego głównego zbioru.")
            continue
        row = require_row(connection, _REGISTRIES[resource.kind], id=resource.id)
        if str(row.id) != resource.id:
            raise StateError("Blokada wymaga kanonicznego ID zasobu.")
    if target.kind == "local":
        root = _collection_lock(config, target.collection_id)
        if root not in requested or not canonical_path(Path(target.path)).is_relative_to(Path(root.id)):
            raise StateError("Cel lokalny wymaga blokady swojego głównego zbioru.")
    if target.kind == "undated":
        root = canonical_path(config.local.undated_root)
        path = canonical_path(Path(target.path))
        if Resource("collection", str(root)) not in requested or path == root or not path.is_relative_to(root):
            raise StateError("Cel Do wyjaśnienia wymaga blokady swojego katalogu.")

    existing = find(connection, operations, operation_key=operation_key)
    if existing is not None:
        if (existing.intent != intent or existing.target_occurrence_id != target.id
                or _operation_resources(connection, existing.id) != requested):
            raise StateError("Klucz operacji ma już inny zapisany zamiar.")
        return existing.id
    if find(connection, operations, target_occurrence_id=target.id) is not None:
        raise StateError("Wystąpienie docelowe ma już zapisaną operację.")

    with connection.begin_nested():
        operation_id = insert_id(connection, operations, operation_key=operation_key, intent=intent,
                                 target_occurrence_id=target.id)
        connection.execute(resources.insert(), [
            dict(operation_id=operation_id, kind=resource.kind, resource_id=resource.id)
            for resource in requested
        ])
    return operation_id


def _require_configuration(connection: Connection, config: Configuration) -> None:
    try:
        same_database = config.app.database_file.resolve(strict=True) == connection.info["state_path"].resolve(strict=True)
    except (OSError, RuntimeError):
        raise StateError("Nie można ustalić położenia bazy konfiguracji.") from None
    if not same_database:
        raise StateError("Konfiguracja wskazuje inną bazę stanu.")
    validate_relationships(config)
    _validate_registered_configuration(connection, config)


def _collection_lock(config: Configuration, collection_id: str) -> Resource:
    try:
        return collection_resource(config, collection_id)
    except KeyError:
        raise StateError("Brak zbioru operacji w bieżącej konfiguracji.") from None


def start_attempt(
    connection: Connection, locks: OperationLocks, operation_id: int, run_id: str, *,
    recovery: Literal["restart", "reconcile"] | None = None,
    check_destination: bool = False,
) -> int:
    """Start work or explicitly take over an unfinished attempt under the same locks."""
    require_write_transaction(connection)
    require_reconciled(connection)
    if recovery not in (None, "restart", "reconcile"):
        raise StateError("Nieznany tryb odzyskiwania próby.")
    require_utf8(run_id, StateError)
    require_row(connection, history.runs, id=run_id)
    require_row(connection, operations, id=operation_id)
    if load_backup_supersession(connection, operation_id) is not None:
        raise StateError("Bieżąca zweryfikowana kopia zastąpiła zamiar tej podmiany.")
    owner = _require_locks(connection, locks, _operation_resources(connection, operation_id))
    previous = connection.execute(attempts.select().filter_by(operation_id=operation_id)
                                  .order_by(attempts.c.id.desc()).limit(1)).mappings().one_or_none()
    if find(connection, attempts, operation_id=operation_id, run_id=run_id) is not None:
        raise StateError("Kolejna próba wymaga nowego uruchomienia.")
    if previous is not None and previous.state == "committed":
        raise StateError("Operacja ma już zatwierdzony wynik.")
    if previous is not None and previous.state == "needs_review":
        if recovery != "reconcile":
            raise StateError("Sprawdzenie celu wymaga nowej próby w nowym uruchomieniu.")
    elif previous is not None and previous.state in _ACTIVE:
        if recovery is None or previous.owner_token == owner:
            raise StateError("Operacja ma niedokończoną próbę wymagającą przejęcia.")
        if recovery == "restart" and _may_have_published(previous):
            raise StateError("Próba mogła opublikować plik. Wymagana jest weryfikacja celu.")
    elif recovery is not None:
        raise StateError("Brak niedokończonej próby do przejęcia.")

    with connection.begin_nested():
        if recovery is not None and previous.state in _ACTIVE:
            _set_state(connection, previous, "interrupted")
        reconciling = recovery == "reconcile" or check_destination
        state = "verifying" if reconciling else "planned"
        attempt_id = insert_id(connection, attempts, operation_id=operation_id, run_id=run_id, owner_token=owner,
                               state=state, reconciling=reconciling,
                               verification_floor=connection.scalar(select(func.max(history.verifications.c.id))) or 0,
                               observation_floor=connection.scalar(select(func.max(history.observations.c.id))) or 0)
        _event(connection, attempt_id, state)
    return attempt_id


def _owned_attempt(connection: Connection, locks: OperationLocks, attempt_id: int) -> RowMapping:
    attempt = require_row(connection, attempts, id=attempt_id)
    owner = _require_locks(connection, locks, _operation_resources(connection, attempt.operation_id))
    if attempt.owner_token != owner:
        raise StateError("Próba nie należy do aktywnego właściciela blokad.")
    return attempt


def advance_attempt(
    connection: Connection, locks: OperationLocks, attempt_id: int,
    state: Literal["transferring", "transferred", "verifying", "verified", "published"],
    *, verification_id: int | None = None,
) -> None:
    """Record the next stage. Verification must match this attempt's run and destination."""
    require_write_transaction(connection)
    attempt = _owned_attempt(connection, locks, attempt_id)
    if attempt.state not in _NEXT or _NEXT[attempt.state] != state:
        raise StateError("Niedozwolone przejście między etapami operacji.")
    if state == "verified":
        proof = require_row(connection, history.verifications, id=verification_id)
        operation = require_row(connection, operations, id=attempt.operation_id)
        if (proof.id <= attempt.verification_floor or proof.result != "match" or proof.run_id != attempt.run_id
                or proof.occurrence_id != operation.target_occurrence_id):
            raise StateError("Próba wymaga zgodnej weryfikacji celu z bieżącego uruchomienia.")
    elif verification_id is not None:
        raise StateError("Dowód należy zapisać na etapie weryfikacji.")
    _set_state(connection, attempt, state, verification_id=verification_id)


def restart_after_missing_destination(
    connection: Connection, locks: OperationLocks, attempt_id: int, observation_id: int,
) -> None:
    """Return reconciliation to planned after a fresh observation of absence."""
    require_write_transaction(connection)
    attempt = _owned_attempt(connection, locks, attempt_id)
    if not attempt.reconciling or attempt.state != "verifying":
        raise StateError("Ponowny transfer wymaga trwającego sprawdzenia celu.")
    observation = require_row(connection, history.observations, id=observation_id)
    operation = require_row(connection, operations, id=attempt.operation_id)
    if (observation.id <= attempt.observation_floor or observation.present or observation.run_id != attempt.run_id
            or observation.occurrence_id != operation.target_occurrence_id):
        raise StateError("Ponowny transfer wymaga świeżego potwierdzenia braku pliku docelowego.")
    _restart_transfer(connection, attempt.id, observation_id=observation.id)


def restart_before_replacement(
    connection: Connection, locks: OperationLocks, attempt_id: int, verification_id: int,
) -> None:
    """Permit replacement after a fresh full read matches the immutable preimage."""
    require_write_transaction(connection)
    attempt = _owned_attempt(connection, locks, attempt_id)
    operation = require_row(connection, operations, id=attempt.operation_id)
    proof = require_row(connection, history.verifications, id=verification_id)
    if (not operation.operation_key.startswith(BACKUP_REPLACE_PREFIX) or not attempt.reconciling
            or attempt.state != "verifying" or proof.id <= attempt.verification_floor
            or proof.run_id != attempt.run_id or proof.occurrence_id != operation.target_occurrence_id
            or proof.result != "mismatch"
            or json.loads(operation.intent).get("previous") != [proof.read_sha256, proof.bytes_read]):
        raise StateError("Podmiana wymaga świeżego pełnego odczytu zgodnego z zapisaną starszą kopią.")
    _restart_transfer(connection, attempt.id)


def _restart_transfer(connection: Connection, attempt_id: int, *, observation_id: int | None = None) -> None:
    with connection.begin_nested():
        connection.execute(attempts.update().filter_by(id=attempt_id).values(
            state="planned", reconciling=False,
            verification_floor=connection.scalar(select(func.max(history.verifications.c.id))) or 0,
        ))
        _event(connection, attempt_id, "planned", observation_id=observation_id)


def stop_attempt(
    connection: Connection, locks: OperationLocks, attempt_id: int,
    state: Literal["interrupted", "failed", "conflict", "needs_review"], *, error_code: str,
) -> None:
    """Retain the attempt and a diagnostic code without private paths or exception text."""
    require_write_transaction(connection)
    attempt = _owned_attempt(connection, locks, attempt_id)
    if attempt.state not in _ACTIVE or state not in _STOPPED:
        raise StateError("Nie można zakończyć próby w tym stanie.")
    if _may_have_published(attempt) and state != "needs_review":
        raise StateError("Próba mogła opublikować plik i wymaga sprawdzenia celu.")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code):
        raise StateError("Błąd próby wymaga krótkiego kodu diagnostycznego.")
    _set_state(connection, attempt, state, error_code=error_code)


def commit_result(
    connection: Connection, locks: OperationLocks, attempt_id: int,
    record_result: Callable[[Connection, int], None],
) -> None:
    """Write history and mark the published attempt committed as one unit."""
    require_write_transaction(connection)
    attempt = _owned_attempt(connection, locks, attempt_id)
    if attempt.state != "published":
        raise StateError("Zatwierdzenie wyniku wymaga zapisanej publikacji.")
    with connection.begin_nested():
        record_result(connection, attempt.verification_id)
        operation = require_row(connection, operations, id=attempt.operation_id)
        target = require_row(connection, history.occurrences, id=operation.target_occurrence_id)
        results = {"staging": history.imports, "local": history.placements, "archive": history.copies,
                   "undated": sort_moves}
        if connection.execute(results[target.kind].select().filter_by(
            verification_id=attempt.verification_id,
        ).limit(1)).first() is None:
            raise StateError("Zatwierdzenie wymaga zapisu wyniku w historii.")
        _set_state(connection, attempt, "committed")


def load_operation(connection: Connection, operation_id: int) -> RowMapping:
    return require_row(connection, operations, id=operation_id)


def find_operation(connection: Connection, operation_key: str) -> RowMapping | None:
    """Find a previously planned operation by its stable key."""
    return find(connection, operations, operation_key=operation_key)


def load_resources(connection: Connection, operation_id: int) -> frozenset[Resource]:
    require_row(connection, operations, id=operation_id)
    return _operation_resources(connection, operation_id)


def load_attempts(connection: Connection, operation_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(attempts.select().filter_by(operation_id=operation_id)
                                    .order_by(attempts.c.id)).mappings())


def load_events(connection: Connection, attempt_id: int) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(events.select().filter_by(attempt_id=attempt_id)
                                    .order_by(events.c.id)).mappings())


def load_unfinished(connection: Connection) -> tuple[RowMapping, ...]:
    """Return pending/retryable intents. A listed operation may still be owned."""
    query = operations.select().where(~operations.c.id.in_(
        attempts.select().with_only_columns(attempts.c.operation_id).filter_by(state="committed")
    ))
    if schema_version(connection) >= "0013":
        query = query.where(~operations.c.id.in_(select(superseded_backups.c.operation_id)))
    return tuple(connection.execute(query.order_by(operations.c.id)).mappings())


def load_backup_supersession(connection: Connection, operation_id: int) -> RowMapping | None:
    if schema_version(connection) < "0013":
        return None
    return find(connection, superseded_backups, operation_id=operation_id)


def supersede_backup(connection: Connection, locks: OperationLocks, operation_id: int,
                     verification_id: int) -> None:
    """Close a restored replacement intent with verified different content on its exact target."""
    require_write_transaction(connection)
    operation = load_operation(connection, operation_id)
    _require_locks(connection, locks, _operation_resources(connection, operation_id))
    target = require_row(connection, history.occurrences, id=operation.target_occurrence_id)
    proof = require_row(connection, history.verifications, id=verification_id)
    current = require_row(connection, history.occurrences, id=proof.occurrence_id)
    last = load_attempts(connection, operation_id)
    if (not operation.operation_key.startswith(BACKUP_REPLACE_PREFIX)
            or target.kind != "archive" or current.kind != "archive"
            or (current.volume_id, current.path) != (target.volume_id, target.path)
            or current.content_id == target.content_id or proof.result != "match"
            or (last and last[-1].state == "committed")
            or load_backup_supersession(connection, operation_id) is not None):
        raise StateError("Zastąpienie zamiaru wymaga zgodnego odczytu innej treści z tej samej ścieżki i woluminu.")
    if last and last[-1].state in _ACTIVE:
        _set_state(connection, last[-1], "interrupted", error_code="superseded_by_current_copy")
    connection.execute(superseded_backups.insert().values(operation_id=operation_id, verification_id=verification_id))


def load_pending_replacements(connection: Connection, volume_id: int, path: str) -> tuple[RowMapping, ...]:
    """Find unfinished replacement intents for one physical archive location."""
    target = history.occurrences
    query = (select(*operations.c, history.contents.c.sha256, history.contents.c.size)
             .join_from(operations, target, operations.c.target_occurrence_id == target.c.id)
             .join(history.contents, history.contents.c.id == target.c.content_id)
             .where(target.c.kind == "archive", target.c.volume_id == volume_id, target.c.path == path,
                    operations.c.operation_key.startswith(BACKUP_REPLACE_PREFIX),
                    ~select(attempts.c.id).where(attempts.c.operation_id == operations.c.id,
                                                attempts.c.state == "committed").exists())
             .order_by(operations.c.id))
    if schema_version(connection) >= "0013":
        query = query.where(~operations.c.id.in_(select(superseded_backups.c.operation_id)))
    return tuple(connection.execute(query).mappings())


def load_backup_locations(connection: Connection) -> tuple[tuple[int, str], ...]:
    """Return journal targets and fully verified archive paths, including read-only copies."""
    target = history.occurrences
    transfers = (select(target.c.volume_id, target.c.path)
             .join_from(operations, target, operations.c.target_occurrence_id == target.c.id)
             .where(operations.c.operation_key.startswith("backup:"), target.c.kind == "archive"))
    reads = (select(target.c.volume_id, target.c.path)
             .join_from(history.verifications, target, history.verifications.c.occurrence_id == target.c.id)
             .where(target.c.kind == "archive", history.verifications.c.result == "match"))
    query = transfers.union(reads).order_by("volume_id", "path")
    return tuple((row.volume_id, row.path) for row in connection.execute(query))
