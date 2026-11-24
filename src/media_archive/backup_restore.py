"""Reconcile known archive copies after restoring the state database."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import Destination
from media_archive.config import Configuration
from media_archive.devices import VolumeUnavailable, registered_volume, verify_volume
from media_archive.sort_conflicts import require_destination_name
from media_archive.sources import LocalSource, SourceDigest, hash_source_file
from media_archive.state import DeviceKey, StateError, history, journal
from media_archive.state.locking import OperationLocks
from media_archive.state.registry import load_registered_devices


@dataclass(frozen=True)
class _Attempt:
    operation_id: int
    target_id: int
    local: RowMapping
    expected: SourceDigest
    previous: SourceDigest | None
    replacement: bool
    recovery: Literal["restart", "reconcile"] | None


@dataclass(frozen=True)
class ArchiveRecovery:
    reads: dict[tuple[int, str], SourceDigest | None]
    attempts: dict[tuple[int, str], list[_Attempt]]
    removals: set[tuple[int, str]]
    unchecked: list[int]
    superseded: set[int]


def _matches_working_copy(config: Configuration, local: RowMapping, digest: SourceDigest) -> bool:
    """Prove the current source still belongs to the restored replacement's collection and volume."""
    path = Path(local.path)
    try:
        scope = config.collection(local.collection_id).local_root
        if config.policy_for_path(local.collection_id, path).changes != "replace_verified":
            return False
    except (KeyError, ValueError):
        return False
    configured = next((volume for volume in config.local.volumes if path.is_relative_to(volume.root)), None)
    if configured is None:
        return False
    key = DeviceKey("local", configured.alias)
    if registered_volume(config, key).volume_id != local.volume_id:
        return False
    try:
        with verify_volume(config, key) as volume:
            return hash_source_file(LocalSource(volume, scope), path.relative_to(scope)) == digest
    except (StateError, OSError):
        return False


def inspect_restored_archives(config: Configuration, connection: Connection,
                              unfinished: tuple[RowMapping, ...]) -> ArchiveRecovery:
    """Read known paths on their verified volumes without mutating archive files."""
    with connection.begin():
        locations = set(journal.load_backup_locations(connection))
        removals = {(row.volume_id, row.path) for row in history.load_pending_archive_removals(connection)}
        attempts = defaultdict(list)
        for operation in unfinished:
            target = history.load_occurrence(connection, operation.target_occurrence_id)
            content = history.load_content(connection, target.content_id)
            intent = json.loads(operation.intent)
            last = journal.load_attempts(connection, operation.id)
            previous = intent.get("previous")
            attempts[target.volume_id, target.path].append(_Attempt(
                operation.id, target.id, history.load_occurrence(connection, intent["local_id"]),
                SourceDigest(content.sha256, content.size),
                SourceDigest(*previous) if previous is not None else None,
                intent["kind"] == "backup_replace", journal.recommended_recovery(last[-1] if last else None),
            ))
    by_volume = defaultdict(list)
    for volume_id, path in sorted(locations | removals):
        by_volume[volume_id].append(path)
    required_volumes = {volume_id for volume_id, _ in attempts.keys() | removals}
    configured = {DeviceKey("archive", target.alias, group.id): group
                  for group in config.archive_groups for target in group.targets}
    bindings = {item.volume_id: item.key for item in load_registered_devices(config.app.database_file)
                if item.key in configured}
    reads = {}
    unchecked = []
    superseded = set()
    for volume_id, paths in by_volume.items():
        key = bindings.get(volume_id)
        try:
            if key is None:
                raise VolumeUnavailable("Brak skonfigurowanego woluminu.")
            root = Path(configured[key].root_relative)
            with verify_volume(config, key) as volume:
                for text in paths:
                    path = Path(text)
                    if path == root or not path.is_relative_to(root) or ".." in path.parts:
                        raise StateError("Zapisana ścieżka BACKUP wykracza poza korzeń archiwum.")
                    destination = Destination(volume, path)
                    require_destination_name(destination)
                    try:
                        observed = destination.recover_file(sync=(volume_id, text) in attempts)
                        digest = observed[0] if observed is not None else None
                        if digest is None and (volume_id, text) in removals:
                            destination.sync_absence()
                    except OSError:
                        raise StateError("Nie można bezpiecznie odczytać celu BACKUP podczas uzgodnienia.") from None
                    for attempt in attempts.get((volume_id, text), ()):
                        if digest != attempt.expected and digest != attempt.previous:
                            if (not attempt.replacement or observed is None
                                    or not _matches_working_copy(config, attempt.local, observed[0])
                                    or LocalSource(volume, None).attributes(path) != observed[1]):
                                raise StateError(f"Operacja BACKUP {attempt.operation_id} wymaga sprawdzenia treści celu.")
                            superseded.add(attempt.operation_id)
                    reads[volume_id, text] = digest
        except VolumeUnavailable:
            if volume_id in required_volumes:
                raise StateError(f"Podłącz wolumin archiwalny {volume_id} wymagany przez niedokończony BACKUP.") from None
            unchecked.append(volume_id)
    return ArchiveRecovery(reads, dict(attempts), removals, unchecked, superseded)


def record_restored_archives(connection: Connection, locks: OperationLocks,
                            evidence: ArchiveRecovery) -> dict[str, object]:
    """Write observations and settle proven publications in the reconciliation transaction."""
    if not evidence.reads:
        return {"Sprawdzone ścieżki BACKUP": 0, "Niesprawdzone woluminy archiwalne": evidence.unchecked}
    run = history.create_run(connection)
    now = datetime.now(timezone.utc)
    matched, changed, missing = 0, 0, 0
    for (volume_id, path), digest in evidence.reads.items():
        content = history.record_content(connection, digest.sha256, digest.size) if digest is not None else None
        known = [row for row in history.load_occurrences(connection, "archive", path) if row.volume_id == volume_id]
        if digest is None:
            missing += 1
        current = next((row.id for row in known if row.content_id == content), None)
        if digest is not None and current is None:
            changed += 1
            current = history.record_occurrence(connection, "archive", path, content_id=content, volume_id=volume_id)
            history.record_observation(connection, current, run, observed_at=now, present=True, size=digest.size)
        for row in known:
            present = digest is not None and row.content_id == content
            history.record_observation(connection, row.id, run, observed_at=now, present=present,
                                       size=digest.size if present else None)
        if (volume_id, path) in evidence.removals:
            history.finish_archive_removal(connection, volume_id, path, absent=digest is None)
        for attempt in evidence.attempts.get((volume_id, path), ()):
            if attempt.operation_id in evidence.superseded:
                assert digest is not None and current is not None
                proof = history.record_verification(connection, current, run, verified_at=now, scope="full",
                                                    read_sha256=digest.sha256, bytes_read=digest.size)
                journal.supersede_backup(connection, locks, attempt.operation_id, proof)
                continue
            if digest != attempt.expected:
                continue  # The existing retry workflow handles the recorded preimage or absence.
            attempt_run = history.create_run(connection)
            attempt_id = journal.start_attempt(connection, locks, attempt.operation_id, attempt_run,
                                               recovery=attempt.recovery, check_destination=True)
            proof = history.record_verification(connection, attempt.target_id, attempt_run, verified_at=now, scope="full",
                                                read_sha256=digest.sha256, bytes_read=digest.size)
            journal.advance_attempt(connection, locks, attempt_id, "verified", verification_id=proof)
            journal.advance_attempt(connection, locks, attempt_id, "published")
            journal.commit_result(connection, locks, attempt_id,
                                  lambda con, verification: history.record_archive_copy(
                                      con, attempt.local.id, verification, update_presence=attempt.replacement,
                                  ))
            matched += 1
    return {
        "Sprawdzone ścieżki BACKUP": len(evidence.reads), "Zatwierdzone próby BACKUP": matched,
        "Zastąpione zamiary BACKUP": sorted(evidence.superseded),
        "Nowe treści na znanych ścieżkach BACKUP": changed, "Brakujące ścieżki BACKUP": missing,
        "Niesprawdzone woluminy archiwalne": evidence.unchecked,
    }
