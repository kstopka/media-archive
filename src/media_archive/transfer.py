"""Execute one planned transfer and reconcile a possibly published copy."""

from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import stat
from typing import TypeVar

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import (
    Destination, InsufficientSpace, LinkUnavailable, Part, TransferConflict, _file, is_transfer_part,
)
from media_archive.config import Configuration
from media_archive.devices import registered_volume, verify_volume
from media_archive.operations import check_application_paths
from media_archive.sort_conflicts import require_destination_name
from media_archive.sources import AdbSource, LocalSource, ReadCancelled, SourceDigest, SourceEntry, hash_source_file
from media_archive.state import DeviceKey, StateError, open_database, transaction, validate_registered_configuration
from media_archive.state import history, journal
from media_archive.state.locking import (
    OperationLocks, Resource, canonical_path, installation_lock, lock_resources, require_locks,
)

SourceOpener = Callable[[], AbstractContextManager[tuple[LocalSource | AdbSource, Path]]]
ResultWriter = Callable[[Connection, int], None]
_Opened = TypeVar("_Opened")


@contextmanager
def _open_under_gate(manager: AbstractContextManager[_Opened],
                     gate: AbstractContextManager[object]) -> Generator[_Opened, None, None]:
    """Serialize setup that reads shared state, releasing the gate during file I/O."""
    with ExitStack() as stack:
        with gate:
            opened = stack.enter_context(manager)
        yield opened


@dataclass(frozen=True)
class ArchiveRead:
    entry: SourceEntry
    mount_id: int
    physical_device: str


@dataclass(frozen=True)
class TransferResult:
    attempt_id: int
    verification_id: int
    archive_read: ArchiveRead | None = None


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

    if target.kind == "staging":
        scope = config.local.staging_root
    elif target.kind == "undated":
        scope = config.local.undated_root
    else:
        scope = config.collection(target.collection_id).local_root
    if not path.is_relative_to(scope) or path == scope:
        raise StateError("Plik docelowy wykracza poza skonfigurowany katalog.")
    if target.kind == "local":
        if any(collection.local_root.is_relative_to(path) for collection in config.collections):
            raise StateError("Plik docelowy blokowałby zarezerwowany katalog zbioru.")
        if config.collection_for_path(target.collection_id, path).id != target.collection_id:
            raise StateError("Cel lokalny należy do innego skonfigurowanego podzbioru.")
    if target.kind == "undated":
        protected = {canonical_path(item.local_root) for item in config.collections}
        protected.add(canonical_path(config.local.staging_root))
        protected.update(canonical_path(disk.mount) for group in config.archive_groups for disk in group.targets)
        actual = canonical_path(scope)
        if any(actual.is_relative_to(root) or root.is_relative_to(actual) for root in protected):
            raise StateError("Katalog Do wyjaśnienia koliduje ze zbiorem, stagingiem lub archiwum.")
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
            if cancelled is None:
                part.rsync_from(stream.fileno())
            else:
                part.rsync_from(stream.fileno(), cancelled=cancelled)
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
                     locks: OperationLocks | None = None, source_occurrence_id: int | None = None,
                     sort_copy: bool = False, verify_existing_only: bool = False,
                     state_gate: AbstractContextManager[object] | None = None) -> TransferResult:
    """Acquire the planned locks, publish a verified copy and commit its result."""
    database = config.app.database_file
    gate = state_gate if state_gate is not None else nullcontext()
    check_application_paths(config)
    with installation_lock(database):
        with gate, open_database(database) as connection:
            resources = journal.load_resources(connection, operation_id)
        if locks is not None:
            require_locks(locks, canonical_path(database), resources)
        ownership = lock_resources(database, resources) if locks is None else nullcontext(locks)
        with ownership as locks, _open_under_gate(open_database(database, writable=True), gate) as connection:
            with gate:
                validate_registered_configuration(config)
            with gate, connection.begin():
                operation = journal.load_operation(connection, operation_id)
                target = history.load_occurrence(connection, operation.target_occurrence_id)
                content = history.load_content(connection, target.content_id)
                attempts = journal.load_attempts(connection, operation_id)
            if attempts and attempts[-1].state == "committed":
                return TransferResult(attempts[-1].id, attempts[-1].verification_id)
            with gate, connection.begin():
                key, path = _destination(connection, config, target, resources)
                previous = None
                if operation.operation_key.startswith(journal.BACKUP_REPLACE_PREFIX):
                    intent = json.loads(operation.intent)
                    local = history.load_occurrence(connection, intent["local_id"])
                    if (target.kind != "archive" or local.kind != "local" or local.content_id != target.content_id
                            or config.policy_for_path(local.collection_id, Path(local.path)).changes != "replace_verified"):
                        raise StateError("Bieżąca polityka zbioru nie dopuszcza zaplanowanej podmiany.")
                    if intent["previous"] is not None:
                        previous = SourceDigest(*intent["previous"])
                if source_occurrence_id is not None:
                    origin = history.load_occurrence(connection, source_occurrence_id)
                    if origin.kind != "source" or origin.content_id != target.content_id:
                        raise StateError("Źródło próby nie odpowiada treści zaplanowanego celu.")
            expected = SourceDigest(content.sha256, content.size)
            recovery = journal.recommended_recovery(attempts[-1] if attempts else None)
            archive_copy = target.kind == "archive"
            check_destination = sort_copy or archive_copy

            def start(mode):
                with gate, transaction(connection):
                    run = history.create_run(connection)
                    attempt_id = journal.start_attempt(connection, locks, operation_id, run, recovery=mode,
                                                       check_destination=check_destination)
                return run, attempt_id

            run_id, attempt = start(recovery)

            def advance(state):
                with gate, transaction(connection):
                    journal.advance_attempt(connection, locks, attempt, state)

            def verify(digest: SourceDigest) -> int:
                if digest != expected:
                    raise StateError("Niezależny odczyt kopii nie zgadza się z zaplanowaną treścią.")
                with gate, transaction(connection):
                    proof = history.record_verification(
                        connection, target.id, run_id, verified_at=datetime.now(timezone.utc),
                        scope="full", read_sha256=digest.sha256, bytes_read=digest.size,
                    )
                    journal.advance_attempt(connection, locks, attempt, "verified", verification_id=proof)
                return proof

            def confirm_absence(destination: Destination) -> None:
                destination.ensure_absent()
                with gate, transaction(connection):
                    absent = history.record_observation(connection, target.id, run_id,
                                                        observed_at=datetime.now(timezone.utc), present=False)
                    journal.restart_after_missing_destination(connection, locks, attempt, absent)

            def confirm_previous(digest: SourceDigest) -> None:
                with gate, transaction(connection):
                    proof = history.record_verification(
                        connection, target.id, run_id, verified_at=datetime.now(timezone.utc),
                        scope="full", read_sha256=digest.sha256, bytes_read=digest.size,
                    )
                    journal.restart_before_replacement(connection, locks, attempt, proof)

            at_risk = recovery == "reconcile" or check_destination
            archive_read = None

            def verify_link(destination: Destination, digest: SourceDigest) -> int:
                nonlocal at_risk
                if cancelled is not None and cancelled():
                    raise ReadCancelled("Anulowano transfer.")
                require_destination_name(destination)
                advance("transferred")
                advance("verifying")
                proof = verify(digest)
                at_risk = True
                return proof

            try:
                with _open_under_gate(verify_volume(config, key, writable=True), gate) as volume:
                    destination = Destination(volume, path)
                    proof = None
                    if check_destination:
                        require_destination_name(destination)
                    if at_risk:
                        observed = destination.recover_file()
                        found = observed[0] if observed is not None else None
                        if found == expected:
                            proof = verify(found)
                            if archive_copy:
                                archive_read = ArchiveRead(observed[1], volume.mount_id, volume.physical_device)
                        else:
                            if verify_existing_only:
                                raise StateError("Zgodna kopia zniknęła lub zmieniła się po podglądzie. Odśwież plan pojemności.")
                            if found != previous:
                                raise TransferConflict("Zastany cel różni się od zapisanego zamiaru. Pozostawiono go bez zmian.")
                            if found is None:
                                confirm_absence(destination)
                            else:
                                confirm_previous(found)
                            at_risk = False
                    if proof is None:
                        if target.kind == "staging" or archive_copy:
                            available, unit = destination.space()
                            parents = destination.preflight(allow_existing=previous is not None)
                            blocks = (expected.size + unit - 1) // unit + len(parents) + 1
                            if available < blocks * unit + config.app.min_free_bytes:
                                if archive_copy:
                                    raise InsufficientSpace("Za mało wolnego miejsca na kopię i wymaganą rezerwę BACKUP.")
                                raise StateError("Za mało wolnego miejsca na kopię i wymaganą rezerwę stagingu.")
                        else:
                            destination.ensure_absent()
                        if cancelled is not None and cancelled():
                            raise ReadCancelled("Anulowano transfer.")
                        advance("transferring")
                        if sort_copy:
                            try:
                                with _open_under_gate(open_source(), gate) as (source, source_path):
                                    if isinstance(source, LocalSource):
                                        with source.open_file(source_path) as stream:
                                            proof = destination.publish_link(
                                                stream.fileno(), lambda digest: verify_link(destination, digest),
                                            )
                            except LinkUnavailable:
                                with gate, transaction(connection):
                                    journal.stop_attempt(connection, locks, attempt, "needs_review",
                                                         error_code="link_unavailable")
                                run_id, attempt = start("reconcile")
                                require_destination_name(destination)
                                confirm_absence(destination)
                                at_risk = False
                                advance("transferring")
                    if proof is None:
                        with (_open_under_gate(open_source(), gate) as (source, source_path),
                              destination.parent(create=True) as parent,
                              Part.create(parent, f".media-archive-transfer-{run_id}", volume.mount_id) as part):
                            if sort_copy:
                                available, unit = destination.space()
                                blocks = (expected.size + unit - 1) // unit
                                if available < blocks * unit + config.app.min_free_bytes:
                                    raise StateError("Za mało miejsca na kopię SORT i wymaganą rezerwę.")
                            _copy_source(part, source, source_path, expected, progress, cancelled)
                            with gate, transaction(connection):
                                if source_occurrence_id is not None:
                                    history.record_observation(
                                        connection, source_occurrence_id, run_id, observed_at=datetime.now(timezone.utc),
                                        present=True, size=expected.size,
                                    )
                                journal.advance_attempt(connection, locks, attempt, "transferred")
                            advance("verifying")
                            copy, status = part.verified_copy()
                            if cancelled is not None and cancelled():
                                raise ReadCancelled("Anulowano transfer.")
                            proof = verify(copy)
                            at_risk = True
                            if check_destination:
                                require_destination_name(destination)
                                entry = part.publish(destination, status, allow_link_fallback=False, previous=previous,
                                                     cancelled=cancelled)
                                if archive_copy:
                                    archive_read = ArchiveRead(entry, volume.mount_id, volume.physical_device)
                            else:
                                part.publish(destination, status)
                    advance("published")
                    with gate, transaction(connection):
                        journal.commit_result(connection, locks, attempt, record_result)
            except (StateError, OSError, KeyboardInterrupt) as error:
                interrupted = isinstance(error, (KeyboardInterrupt, ReadCancelled))
                conflict = isinstance(error, TransferConflict)
                try:
                    with gate, transaction(connection):
                        journal.stop_attempt(
                            connection, locks, attempt,
                            "needs_review" if at_risk else "interrupted" if interrupted else "conflict" if conflict else "failed",
                            error_code="interrupted" if interrupted else "destination_conflict" if conflict else "transfer_failed",
                        )
                except StateError:
                    pass  # The last committed stage still determines recovery.
                if isinstance(error, OSError):
                    if archive_copy and error.errno == errno.ENOSPC:
                        raise InsufficientSpace("Brak miejsca podczas zapisu na celu BACKUP.") from None
                    raise StateError("Nie można bezpiecznie ukończyć zapisu lub publikacji kopii.") from None
                raise
            return TransferResult(attempt, proof, archive_read)
