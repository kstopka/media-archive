"""Execute one planned transfer and reconcile a possibly published copy."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import stat

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import Destination, Part, TransferConflict, _file, is_transfer_part
from media_archive.config import Configuration
from media_archive.devices import registered_volume, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sources import AdbSource, LocalSource, ReadCancelled, SourceDigest, hash_source_file
from media_archive.state import DeviceKey, StateError, open_database, transaction, validate_registered_configuration
from media_archive.state import history, journal
from media_archive.state.locking import (
    OperationLocks, Resource, canonical_path, installation_lock, lock_resources, require_locks,
)

SourceOpener = Callable[[], AbstractContextManager[tuple[LocalSource | AdbSource, Path]]]
ResultWriter = Callable[[Connection, int], None]


@dataclass(frozen=True)
class TransferResult:
    attempt_id: int
    verification_id: int


def _destination(connection: Connection, config: Configuration, target: RowMapping,
                 resources: frozenset[Resource]) -> tuple[DeviceKey, Path]:
    path = Path(target.path)
    if ".." in path.parts or any(is_transfer_part(part) for part in path.parts):
        raise StateError("Ścieżka celu koliduje z katalogiem części transferu.")
    if target.kind == "archive":
        for group in config.archive_groups:
            root = Path(group.root_relative)
            if Resource("archive", group.id) not in resources or not path.is_relative_to(root) or path == root:
                continue
            for item in group.targets:
                key = DeviceKey("archive", item.alias, group.id)
                if registered_volume(config, key).volume_id == target.volume_id:
                    if any(reserved.is_relative_to(path)
                           for _, reserved in history.archive_destinations(connection, group.id, root)):
                        raise StateError("Plik docelowy blokowałby zarezerwowany katalog archiwum.")
                    return key, path
        raise StateError("Cel archiwalny nie należy do aktualnego zarejestrowanego woluminu operacji.")

    scope = (config.local.staging_root if target.kind == "staging"
             else config.collection(target.collection_id).local_root)
    if not path.is_relative_to(scope) or path == scope:
        raise StateError("Plik docelowy wykracza poza skonfigurowany katalog.")
    if target.kind == "local":
        if any(collection.local_root.is_relative_to(path) for collection in config.collections):
            raise StateError("Plik docelowy blokowałby zarezerwowany katalog zbioru.")
        if config.collection_for_path(target.collection_id, path).id != target.collection_id:
            raise StateError("Cel lokalny należy do innego skonfigurowanego podzbioru.")
    for volume in config.local.volumes:
        if path.is_relative_to(volume.root):
            key = DeviceKey("local", volume.alias)
            if target.volume_id is not None and registered_volume(config, key).volume_id != target.volume_id:
                raise StateError("Lokalny wolumin celu zmienił się od planowania transferu.")
            return key, path.relative_to(volume.root)
    raise StateError("Transfer wymaga celu na zarejestrowanym woluminie lokalnym.")


def _copy_source(part: Part, source: LocalSource | AdbSource, path: Path, expected: SourceDigest,
                 progress: Callable[[int, int], None] | None, cancelled: Callable[[], bool] | None) -> None:
    entry = source.attributes(path)
    if not stat.S_ISREG(entry.mode) or entry.size != expected.size:
        raise StateError("Źródło transferu ma inny typ lub rozmiar niż zaplanowano.")
    if isinstance(source, LocalSource):
        if hash_source_file(source, path, expected=entry, progress=progress, cancelled=cancelled) != expected:
            raise StateError("SHA-256 źródła nie zgadza się z zamiarem transferu.")
        with source.open_file(path, expected=entry) as stream:
            part.rsync_from(stream.fileno())
        if source.attributes(path) != entry:
            raise StateError("Źródło lokalne zmieniło się od rozpoczęcia transferu.")
    else:
        fd = os.open("copy", os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=part.directory)
        with _file(fd, part.mount_id), os.fdopen(os.dup(fd), "wb") as stream:
            digest = hash_source_file(source, path, expected=entry, progress=progress, cancelled=cancelled,
                                      on_chunk=stream.write)
            stream.flush()
            os.utime(fd, ns=(entry.modified_ns, entry.modified_ns))
        if digest != expected:
            raise StateError("SHA-256 źródła nie zgadza się z zamiarem transferu.")


def execute_transfer(config: Configuration, operation_id: int, *, open_source: SourceOpener,
                     record_result: ResultWriter, progress: Callable[[int, int], None] | None = None,
                     cancelled: Callable[[], bool] | None = None,
                     locks: OperationLocks | None = None) -> TransferResult:
    """Acquire the planned locks, publish without overwriting, and commit the verified result."""
    database = config.app.database_file
    check_application_paths(config)
    with installation_lock(database):
        with open_database(database) as connection:
            resources = journal.load_resources(connection, operation_id)
        if locks is not None:
            require_locks(locks, canonical_path(database), resources)
        ownership = lock_resources(database, resources) if locks is None else nullcontext(locks)
        with ownership as locks, open_database(database, writable=True) as connection:
            validate_registered_configuration(config)
            with connection.begin():
                operation = journal.load_operation(connection, operation_id)
                target = history.load_occurrence(connection, operation.target_occurrence_id)
                content = history.load_content(connection, target.content_id)
                attempts = journal.load_attempts(connection, operation_id)
            if attempts and attempts[-1].state == "committed":
                return TransferResult(attempts[-1].id, attempts[-1].verification_id)
            with connection.begin():
                key, path = _destination(connection, config, target, resources)
            expected = SourceDigest(content.sha256, content.size)
            recovery = journal.recommended_recovery(attempts[-1] if attempts else None)
            with transaction(connection):
                run_id = history.create_run(connection)
                attempt = journal.start_attempt(connection, locks, operation_id, run_id, recovery=recovery)

            def advance(state):
                with transaction(connection):
                    journal.advance_attempt(connection, locks, attempt, state)

            def verify(digest: SourceDigest) -> int:
                if digest != expected:
                    raise StateError("Niezależny odczyt kopii nie zgadza się z zaplanowaną treścią.")
                with transaction(connection):
                    proof = history.record_verification(
                        connection, target.id, run_id, verified_at=datetime.now(timezone.utc),
                        scope="full", read_sha256=digest.sha256, bytes_read=digest.size,
                    )
                    journal.advance_attempt(connection, locks, attempt, "verified", verification_id=proof)
                return proof

            at_risk = recovery == "reconcile"
            try:
                with verify_volume(config, key, writable=True) as volume:
                    destination = Destination(volume, path)
                    proof = None
                    if at_risk:
                        found = destination.recover_digest()
                        if found is not None:
                            if found != expected:
                                raise TransferConflict("Zastany cel ma inną treść. Pozostawiono go bez zmian.")
                            proof = verify(found)
                        else:
                            with transaction(connection):
                                absent = history.record_observation(connection, target.id, run_id,
                                                                    observed_at=datetime.now(timezone.utc), present=False)
                                journal.restart_after_missing_destination(connection, locks, attempt, absent)
                            at_risk = False
                    if proof is None:
                        destination.ensure_absent()
                        if cancelled is not None and cancelled():
                            raise ReadCancelled("Anulowano transfer.")
                        advance("transferring")
                        with (open_source() as (source, source_path),
                              destination.parent(create=True) as parent,
                              Part.create(parent, f".media-archive-transfer-{run_id}", volume.mount_id) as part):
                            _copy_source(part, source, source_path, expected, progress, cancelled)
                            advance("transferred")
                            advance("verifying")
                            copy, status = part.verified_copy()
                            if cancelled is not None and cancelled():
                                raise ReadCancelled("Anulowano transfer.")
                            proof = verify(copy)
                            at_risk = True
                            part.publish(destination, status)
                    advance("published")
                    with transaction(connection):
                        journal.commit_result(connection, locks, attempt, record_result)
            except (StateError, OSError, KeyboardInterrupt) as error:
                interrupted = isinstance(error, (KeyboardInterrupt, ReadCancelled))
                conflict = isinstance(error, TransferConflict)
                try:
                    with transaction(connection):
                        journal.stop_attempt(
                            connection, locks, attempt,
                            "needs_review" if at_risk else "interrupted" if interrupted else "conflict" if conflict else "failed",
                            error_code="interrupted" if interrupted else "destination_conflict" if conflict else "transfer_failed",
                        )
                except StateError:
                    pass  # The last committed stage still determines recovery.
                if isinstance(error, OSError):
                    raise StateError("Nie można bezpiecznie ukończyć zapisu lub publikacji kopii.") from None
                raise
            return TransferResult(attempt, proof)
