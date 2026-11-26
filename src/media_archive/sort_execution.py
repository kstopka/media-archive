"""Execute and resume verified SORT moves."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import json
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import Destination
from media_archive.config import Configuration
from media_archive.devices import registered_volume, verify_volume
from media_archive.metadata import MediaMetadata
from media_archive.operations import check_application_paths
from media_archive.sort_conflicts import choose_name, require_destination_name
from media_archive.sort_planning import SortItem, plan_sort
from media_archive.sources import LocalSource, ReadCancelled, SourceDigest
from media_archive.state import DeviceKey, StateError, history, journal, open_database, sort_moves, transaction
from media_archive.state.backup_reads import save_reads
from media_archive.state.locking import (
    OperationLocks, Resource, canonical_path, collection_resource, installation_lock, lock_resources,
)
from media_archive.state.reconciliation import require_reconciled
from media_archive.transfer import _destination, execute_transfer


@dataclass(frozen=True)
class SortResult:
    item: SortItem
    status: Literal["placed", "undated", "blocked", "interrupted"]


@dataclass(frozen=True)
class SortOperation:
    move: RowMapping
    operation: RowMapping
    origin: RowMapping
    target: RowMapping
    digest: SourceDigest


def load_sort_operation(connection: Connection, operation_id: int) -> SortOperation:
    move = sort_moves.find_move(connection, operation_id)
    if move is None:
        raise StateError("Operacja nie jest zapisanym przeniesieniem SORT.")
    operation = journal.load_operation(connection, operation_id)
    origin = history.load_occurrence(connection, move.source_id)
    target = history.load_occurrence(connection, operation.target_occurrence_id)
    if origin.kind not in ("staging", "undated") or target.kind not in ("local", "undated"):
        raise StateError("Operacja SORT ma nieprawidłowe wystąpienia.")
    if origin.content_id != target.content_id or origin.path == target.path:
        raise StateError("Zapisane źródło SORT nie odpowiada zaplanowanemu celowi.")
    content = history.load_content(connection, target.content_id)
    return SortOperation(move, operation, origin, target, SourceDigest(content.sha256, content.size))


def commit_sort(connection: Connection, operation_id: int, verification_id: int) -> None:
    operation = load_sort_operation(connection, operation_id)
    intent = json.loads(operation.operation.intent)
    if operation.target.kind == "local":
        history.record_placement(connection, operation.move.import_id, verification_id)
    if intent["group"] is not None:
        sort_moves.record_conflict_member(connection, intent["group"], intent["name"],
                                          operation.target.content_id, operation.target.volume_id,
                                          operation.target.path)
    sort_moves.verify_move(connection, operation_id, verification_id)


def _working_resources(config: Configuration) -> set[Resource]:
    return {Resource("collection", str(canonical_path(path)))
            for path in (config.local.staging_root, config.local.undated_root)}


def _local_key(config: Configuration, path: Path) -> DeviceKey:
    for volume in config.local.volumes:
        if path.is_relative_to(volume.root):
            return DeviceKey("local", volume.alias)
    raise StateError("Ścieżka SORT musi leżeć na zarejestrowanym woluminie lokalnym.")


def _schedule(config: Configuration, item: SortItem, locks: OperationLocks) -> int:
    target = item.target
    assert target is not None and item.origin_id is not None
    database = config.app.database_file
    target_kind = "undated" if item.state == "undated" else "local"
    with open_database(database) as connection:
        origin = history.load_occurrence(connection, item.origin_id)
        content = history.load_content(connection, origin.content_id)
        imported = history.load_import(connection, item.import_id)
        original = history.load_occurrence(connection, imported.source_occurrence_id)
        if history.load_placement(connection, item.import_id) is not None or original.content_id != origin.content_id:
            raise StateError("Import przestał oczekiwać na SORT.")
        profile = history.load_source_profile(connection, original.source_id)
    key = _local_key(config, target)
    volume_id = registered_volume(config, key).volume_id
    if volume_id is None:
        raise StateError("Docelowy wolumin nie ma zapisanej tożsamości.")
    with verify_volume(config, key, writable=True) as volume:
        choice = choose_name(Destination(volume, target.relative_to(volume.root)),
                             SourceDigest(content.sha256, content.size))
        target = volume.root / choice.path
        alternatives = [(volume.root / path, digest) for path, digest in choice.variants]
    metadata = item.metadata
    intent = json.dumps({
        "kind": "sort", "name": item.name, "group": choice.group_path,
        "manual_date": item.manual_date.isoformat() if item.manual_date else None,
        "metadata": {
            "tag": metadata.tag, "raw_date": metadata.raw_date, "tool_version": metadata.tool_version,
            "timestamp": metadata.timestamp.isoformat() if metadata.timestamp else None,
            "diagnostics": metadata.diagnostics,
        } if metadata else None,
    }, ensure_ascii=False)
    resources = _working_resources(config)
    if target_kind == "local":
        resources.add(collection_resource(config, profile.collection_id))
    with open_database(database, writable=True) as connection, transaction(connection):
        occurrence = history.record_occurrence(connection, target_kind, target.as_posix(),
                                               content_id=origin.content_id, volume_id=volume_id,
                                               collection_id=profile.collection_id if target_kind == "local" else None)
        operation = journal.plan_operation(
            connection, locks, config, operation_key=f"sort-{item.import_id}-{occurrence}", intent=intent,
            target_occurrence_id=occurrence, required_resources=resources,
        )
        sort_moves.record_move(connection, operation, item.import_id, origin.id)
        if alternatives:
            run = history.create_run(connection)
            observed_at = datetime.now(timezone.utc)
            for path, digest in alternatives:
                variant = history.record_content(connection, digest.sha256, digest.size)
                sort_moves.record_conflict_member(connection, choice.group_path, item.name, variant, volume_id,
                                                  path.as_posix())
                if target_kind == "local":
                    previous = next((row.id for row in history.load_occurrences(connection, "local", path.as_posix())
                                     if (row.content_id, row.volume_id, row.collection_id)
                                     == (variant, volume_id, profile.collection_id)), None)
                    if previous is None:
                        previous = history.record_occurrence(connection, "local", path.as_posix(), content_id=variant,
                                                             collection_id=profile.collection_id, volume_id=volume_id)
                    history.record_verification(connection, previous, run, verified_at=observed_at, scope="full",
                                                read_sha256=digest.sha256, bytes_read=digest.size)
    return operation


def _origin_location(config: Configuration, operation: SortOperation) -> tuple[DeviceKey, Path, Path]:
    path = Path(operation.origin.path)
    root = config.local.undated_root if operation.origin.kind == "undated" else config.local.staging_root
    if not path.is_relative_to(root) or path == root or canonical_path(path) != path:
        raise StateError("Kopia robocza SORT jest poza bezpiecznym katalogiem.")
    key = _local_key(config, root)
    if registered_volume(config, key).volume_id != operation.origin.volume_id:
        raise StateError("Wolumin kopii roboczej zmienił się od planowania SORT.")
    return key, root, path


@contextmanager
def _open_origin(config: Configuration, operation: SortOperation) -> Generator[tuple[LocalSource, Path], None, None]:
    key, root, path = _origin_location(config, operation)
    with verify_volume(config, key) as mounted:
        yield LocalSource(mounted, root), path.relative_to(root)


def _cleanup(config: Configuration, operation: SortOperation, resources: frozenset[Resource]) -> None:
    database = config.app.database_file
    with open_database(database) as connection:
        key, relative = _destination(connection, config, operation.target, resources)
    entry = None
    with verify_volume(config, key) as mounted:
        with Destination(mounted, relative).keep_verified(operation.digest) as (confirm, final_entry):
            source_key, _, path = _origin_location(config, operation)
            with verify_volume(config, source_key, writable=True) as source:
                Destination(source, path.relative_to(source.root)).remove_verified(operation.digest, confirm)
            if operation.target.kind == "local":
                entry = final_entry()
    with open_database(database, writable=True) as connection, transaction(connection):
        if entry is not None:
            save_reads(connection, [(operation.target.volume_id, operation.target.path,
                                     entry.volume_fingerprint(), operation.target.content_id)])
        sort_moves.finish_cleanup(connection, operation.operation.id)


def inspect_sort_recovery(config: Configuration, connection: Connection, operation: SortOperation) -> SourceDigest | None:
    """Read the destination of an unfinished restored move without changing media."""
    resources = journal.load_resources(connection, operation.operation.id)
    key, path = _destination(connection, config, operation.target, resources)
    try:
        with verify_volume(config, key) as volume:
            destination = Destination(volume, path)
            require_destination_name(destination)
            digest = destination.recover_digest()
        if digest == operation.digest:
            return digest
        if digest is not None or operation.move.verification_id is not None:
            raise StateError("Cel operacji SORT nie odpowiada przywróconemu stanowi.")
        origin_key, _, origin = _origin_location(config, operation)
        with verify_volume(config, origin_key) as volume:
            if Destination(volume, origin.relative_to(volume.root)).recover_digest(sync=False) != operation.digest:
                raise StateError("Brak zgodnej kopii roboczej i docelowej niedokończonego SORT.")
    except OSError:
        raise StateError("Nie można bezpiecznie odczytać plików niedokończonego SORT.") from None
    return None


def record_restored_sort(connection: Connection, locks: OperationLocks, operation: SortOperation,
                         digest: SourceDigest | None) -> None:
    if operation.move.verification_id is not None:
        return
    operation_id = operation.operation.id
    attempts = journal.load_attempts(connection, operation_id)
    recovery = journal.recommended_recovery(attempts[-1] if attempts else None)
    if digest is None and recovery != "reconcile":
        return
    run = history.create_run(connection)
    attempt = journal.start_attempt(connection, locks, operation_id, run, recovery=recovery, check_destination=True)
    now = datetime.now(timezone.utc)
    if digest is None:
        absence = history.record_observation(connection, operation.target.id, run, observed_at=now, present=False)
        journal.restart_after_missing_destination(connection, locks, attempt, absence)
        journal.stop_attempt(connection, locks, attempt, "interrupted", error_code="restored_destination_absent")
        return
    proof = history.record_verification(connection, operation.target.id, run, verified_at=now, scope="full",
                                        read_sha256=digest.sha256, bytes_read=digest.size)
    journal.advance_attempt(connection, locks, attempt, "verified", verification_id=proof)
    journal.advance_attempt(connection, locks, attempt, "published")
    journal.commit_result(connection, locks, attempt, lambda con, verification: commit_sort(con, operation_id, verification))


def _run_move(config: Configuration, operation_id: int, locks: OperationLocks, item: SortItem | None = None) -> SortResult:
    with open_database(config.app.database_file) as connection:
        require_reconciled(connection)
        operation = load_sort_operation(connection, operation_id)
        resources = journal.load_resources(connection, operation_id)
        if item is None:
            imported = history.load_import(connection, operation.move.import_id)
            occurrence = history.load_occurrence(connection, imported.source_occurrence_id)
            profile = history.load_source_profile(connection, occurrence.source_id)
    status = "undated" if operation.target.kind == "undated" else "placed"
    intent = json.loads(operation.operation.intent)
    if item is None:
        raw = intent["metadata"]
        metadata = None if raw is None else MediaMetadata(
            raw["tag"], raw["raw_date"], datetime.fromisoformat(raw["timestamp"]) if raw["timestamp"] else None,
            raw["tool_version"], tuple(raw["diagnostics"]),
        )
        manual = date.fromisoformat(intent["manual_date"]) if intent["manual_date"] else None
        item = SortItem(operation.move.import_id, Path(operation.origin.path), intent["name"], profile.alias,
                        "undated" if status == "undated" else "ready", metadata=metadata, manual_date=manual)
    item = replace(item, operation_id=operation_id, target=Path(operation.target.path), conflict_group=intent["group"])
    try:
        execute_transfer(config, operation_id, locks=locks, sort_copy=True,
                         open_source=lambda: _open_origin(config, operation),
                         record_result=lambda connection, proof: commit_sort(connection, operation_id, proof))
        if not operation.move.cleanup_done:
            _cleanup(config, operation, resources)
    except (KeyboardInterrupt, ReadCancelled):
        return SortResult(item, "interrupted")
    except (StateError, OSError) as error:
        problem = str(error) if isinstance(error, StateError) else "Nie można bezpiecznie dokończyć zapisu SORT."
        return SortResult(replace(item, problem=problem), "blocked")
    return SortResult(item, status)


def resume_sort(config: Configuration, operation_id: int) -> SortResult:
    check_application_paths(config)
    database = config.app.database_file
    with installation_lock(database):
        with open_database(database) as connection:
            resources = journal.load_resources(connection, operation_id)
        with lock_resources(database, resources) as locks:
            return _run_move(config, operation_id, locks)


def is_sort_operation(config: Configuration, operation_id: int) -> bool:
    with open_database(config.app.database_file) as connection:
        return sort_moves.supported(connection) and sort_moves.find_move(connection, operation_id) is not None


def execute_sort(config: Configuration) -> tuple[SortResult, ...]:
    check_application_paths(config)
    database = config.app.database_file
    resources = _working_resources(config)
    resources.update(collection_resource(config, collection.id) for collection in config.collections)
    with installation_lock(database):
        with open_database(database) as connection:
            require_reconciled(connection)
            if not sort_moves.supported(connection):
                raise StateError("Wykonanie SORT wymaga aktualnej bazy. Użyj migrate.")
            pending = [move for move in sort_moves.load_moves(connection) if not move.cleanup_done]
            for move in pending:
                resources.update(journal.load_resources(connection, move.operation_id))
        with lock_resources(database, resources) as locks:
            with open_database(database) as connection:
                pending = [move for move in sort_moves.load_moves(connection) if not move.cleanup_done]
            results = []
            resumed_imports = set()
            for move in pending:
                result = _run_move(config, move.operation_id, locks)
                results.append(result)
                resumed_imports.add(move.import_id)
                if result.status == "interrupted":
                    return tuple(results)
            for item in plan_sort(config):
                if item.import_id in resumed_imports:
                    continue
                if item.state == "blocked":
                    results.append(SortResult(item, "blocked"))
                    continue
                if item.target == item.staging:
                    results.append(SortResult(item, "undated"))
                    continue
                try:
                    operation_id = _schedule(config, item, locks)
                    result = _run_move(config, operation_id, locks, item)
                except KeyboardInterrupt:
                    result = SortResult(item, "interrupted")
                except (StateError, OSError) as error:
                    problem = str(error) if isinstance(error, StateError) else "Nie można sprawdzić celu SORT."
                    result = SortResult(replace(item, problem=problem), "blocked")
                results.append(result)
                if result.status == "interrupted":
                    break
    return tuple(results)
