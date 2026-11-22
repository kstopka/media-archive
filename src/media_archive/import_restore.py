"""Reconcile restored state against staging and baseline evidence needed by IMPORT."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
import json
from pathlib import Path
import stat

from sqlalchemy.engine import Connection

from media_archive._transfer_files import Destination
from media_archive.audit import _Scan, _scan
from media_archive.config import Configuration
from media_archive.devices import MountedVolume, registered_volume, staging_volume, verify_volume
from media_archive.import_journal import ImportOperation, commit_import, load_import_operation
from media_archive.operations import check_application_paths
from media_archive.phone_initialization import _archive_group, _archive_matches
from media_archive.sources import LocalSource, SourceDigest, hash_source_file, open_local_source
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.audit import load_full_audit
from media_archive.state.initialization import load_initialization
from media_archive.state.locking import (
    OperationLocks, Resource, canonical_path, collection_resource, installation_lock, lock_resources,
)
from media_archive.state.reconciliation import load_reconciliation, restore_state
from media_archive.state.registry import validate_registered_configuration


def _check_local_baseline(config: Configuration, collection_id: str,
                          copies: dict[int, tuple[SourceDigest, set[Path]]]) -> None:
    collection = config.collection(collection_id)
    root = collection.local_root
    volume = next(volume for volume in config.local.volumes if root.is_relative_to(volume.root))
    with open_local_source(config, DeviceKey("local", volume.alias), scope=root) as source:
        for content_id, (expected, paths) in copies.items():
            candidates = [path.relative_to(root) for path in sorted(paths) if path.is_relative_to(root)
                          and config.collection_for_path(collection_id, path).id == collection_id]
            for path in candidates:
                try:
                    if hash_source_file(source, path) == expected:
                        break
                except StateError:
                    continue  # Another recorded copy can still prove the same content.
            else:
                locations = ", ".join(path.as_posix() for path in candidates)
                detail = f" Sprawdzone ścieżki w zbiorze: {locations}." if locations else ""
                raise StateError(
                    f"W zbiorze {collection_id} nie potwierdzono lokalnej kopii bazowej (ID treści {content_id}). "
                    "Przywróć zgodną kopię pod jedną z zapisanych ścieżek przed ponowieniem." + detail
                )


def _baseline_evidence(config: Configuration, connection: Connection, audit_ids: Iterable[str],
                       restored_at: datetime, phones: dict[str, int]) -> int:
    audits = {}
    for audit_id in audit_ids:
        run, _ = load_full_audit(connection, audit_id)
        if run.group_id in audits:
            raise StateError("Podaj jeden pełny audyt na grupę archiwalną.")
        audits[run.group_id] = run.id
    used = set()
    for phone in config.phones:
        local_paths: dict[int, set[Path]] = defaultdict(set)
        archive_only = set()
        with connection.begin():
            state = load_initialization(connection, phones[phone.alias])
            comparison = next((scope for scope in state.scopes if scope.phase == "comparison"
                               and scope.scope == "copies" and scope.completed), None)
            if comparison is None:
                continue
            for row in json.loads(comparison.cursor)["files"]:
                if row["decision"] != "existing_copy":
                    continue
                content_id = history.load_occurrence(connection, row["occurrence_id"]).content_id
                if row["local_occurrence_ids"]:
                    for occurrence_id in row["local_occurrence_ids"]:
                        copy = history.load_occurrence(connection, occurrence_id)
                        if (copy.kind, copy.collection_id, copy.content_id) != ("local", phone.collection_id, content_id):
                            raise StateError("Zapisana kopia bazowa nie odpowiada zbiorowi lub treści.")
                        local_paths[content_id].add(Path(copy.path))
                else:
                    archive_only.add(content_id)
            local_copies = {}
            for content_id, paths in local_paths.items():
                content = history.load_content(connection, content_id)
                local_copies[content_id] = SourceDigest(content.sha256, content.size), paths
        if local_copies:
            _check_local_baseline(config, phone.collection_id, local_copies)
        wanted = archive_only - local_paths.keys()
        if not wanted:
            continue
        group = _archive_group(config, phone.collection_id)
        audit_id = audits.get(group.id)
        if audit_id is None:
            raise StateError(f"Wykonaj pełny audyt grupy {group.id} i podaj jego ID przez --audit.")
        matches = _archive_matches(config, phone, group, audit_id, connection, wanted)
        if any(not any(datetime.fromisoformat(read["read_at"]) > restored_at
                       for read in matches.get(content, ())) for content in wanted):
            raise StateError(f"Grupa {group.id} nie ma świeżego dowodu odczytu wszystkich wymaganych kopii.")
        used.add(group.id)
    return len(used)


def _scan_staging(mounted: MountedVolume, staging: Path,
                  expected: dict[Path, SourceDigest]) -> dict[Path, SourceDigest]:
    try:
        with Destination(mounted, (staging / "probe").relative_to(mounted.root)).parent():
            scan = _Scan()
            _scan(LocalSource(mounted, staging), scan)
            if scan.errors:
                raise StateError("Nie ukończono odczytu stagingu po przywróceniu bazy.")
    except FileNotFoundError:
        return {}
    found = {}
    for relative, entry in scan.entries.items():
        path = staging / relative
        if stat.S_ISDIR(entry.mode):
            if path in expected:
                raise StateError(f"Katalog zajmuje ścieżkę pliku stagingu: {relative.as_posix()}.")
            continue
        if path not in expected or not stat.S_ISREG(entry.mode):
            raise StateError(f"Staging zawiera nierozpoznany wpis: {relative.as_posix()}.")
        digest = Destination(mounted, path.relative_to(mounted.root)).recover_digest()
        if digest != expected[path]:
            raise StateError(f"Treść stagingu różni się od historii: {relative.as_posix()}.")
        found[path] = digest
    return found


def reconcile_imports(config: Configuration, audit_ids: Iterable[str] = ()) -> dict[str, object]:
    """Clear the restore requirement only after import state has been accounted for."""
    check_application_paths(config)
    database = config.app.database_file
    staging = config.local.staging_root
    with installation_lock(database):
        with open_database(database) as connection:
            restored = load_reconciliation(connection)
            if restored is None:
                raise StateError("Baza nie wymaga uzgodnienia po przywróceniu.")
            phones = {phone.alias: history.load_source_id(connection, "phone", phone.alias) for phone in config.phones}
            operations = journal.load_unfinished(connection)
            resources = {resource for operation in operations for resource in journal.load_resources(connection, operation.id)}
        resources.add(Resource("collection", str(canonical_path(staging))))
        resources.update(collection_resource(config, phone.collection_id) for phone in config.phones)
        resources.update(Resource("source", str(source_id)) for source_id in phones.values())
        resources.update(Resource("archive", group.id) for group in config.archive_groups)
        with lock_resources(database, resources) as locks, open_database(database, writable=True) as connection:
            validate_registered_configuration(config)
            with connection.begin():
                if load_reconciliation(connection) != restored:
                    raise StateError("Stan przywrócenia zmienił się przed rozpoczęciem uzgodnienia.")
                operations = [load_import_operation(connection, row.id) for row in journal.load_unfinished(connection)]
                imports = history.load_staged_imports(connection)
                expected = {}
                for operation in operations:
                    content = history.load_content(connection, operation.target.content_id)
                    expected[Path(operation.target.path)] = SourceDigest(content.sha256, content.size)
            audits = _baseline_evidence(config, connection, audit_ids, datetime.fromisoformat(restored.restored_at), phones)
            volume = staging_volume(config)
            key = DeviceKey("local", volume.alias)
            volume_id = registered_volume(config, key).volume_id
            latest = {(entry.source_id, entry.content_id): entry for entry in imports}
            pending = [entry for entry in latest.values() if entry.placement_id is None]
            for row in [*pending, *(entry.target for entry in operations)]:
                path = Path(row.path)
                if row.volume_id != volume_id or not path.is_relative_to(staging) or path == staging:
                    raise StateError("Oczekujący materiał należy do innego woluminu lub katalogu stagingu.")
            for entry in imports:
                if entry.volume_id == volume_id and Path(entry.path).is_relative_to(staging):
                    expected[Path(entry.path)] = SourceDigest(entry.sha256, entry.size)
            try:
                with verify_volume(config, key) as mounted:
                    found = _scan_staging(mounted, staging, expected)
            except OSError:
                raise StateError("Nie można bezpiecznie sprawdzić stagingu po przywróceniu bazy.") from None
            matched = 0
            absent = 0
            with transaction(connection):
                # Other connections continue seeing the restore marker until all journal changes commit.
                connection.execute(restore_state.delete())
                for operation in operations:
                    digest = found.get(Path(operation.target.path))
                    if _resolve_attempt(connection, locks, operation, digest):
                        matched += 1
                    else:
                        absent += 1
            return {
                "Generacja": restored.generation_id, "Pełne audyty użyte do uzgodnienia": audits,
                "Zweryfikowane pliki stagingu": len(found), "Zatwierdzone próby": matched,
                "Próby z potwierdzonym brakiem celu": absent,
                "Brakujące materiały wymagające jawnego reimportu": [
                    {"id_importu": entry.id, "staging": entry.path} for entry in pending if Path(entry.path) not in found
                ],
            }


def _resolve_attempt(connection: Connection, locks: OperationLocks, operation: ImportOperation,
                     digest: SourceDigest | None) -> bool:
    target = operation.target
    recovery = journal.recommended_recovery(operation.last)
    run = history.create_run(connection)
    now = datetime.now(timezone.utc)
    if recovery != "reconcile":
        if digest is not None:
            raise StateError(f"Operacja {operation.operation.id} ma zastany cel bez zapisu możliwej publikacji.")
        history.record_observation(connection, target.id, run, observed_at=now, present=False)
        return False

    attempt = journal.start_attempt(connection, locks, operation.operation.id, run, recovery="reconcile")
    if digest is None:
        observation = history.record_observation(connection, target.id, run, observed_at=now, present=False)
        journal.restart_after_missing_destination(connection, locks, attempt, observation)
        journal.stop_attempt(connection, locks, attempt, "interrupted", error_code="restored_destination_absent")
        return False
    proof = history.record_verification(connection, target.id, run, verified_at=now, scope="full",
                                        read_sha256=digest.sha256, bytes_read=digest.size)
    journal.advance_attempt(connection, locks, attempt, "verified", verification_id=proof)
    journal.advance_attempt(connection, locks, attempt, "published")
    journal.commit_result(connection, locks, attempt,
                          lambda con, verification: commit_import(con, operation.operation.id, verification))
    return True
