"""Reconcile restored state against staging and baseline evidence needed by IMPORT."""

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
import json
from pathlib import Path
import stat

from sqlalchemy.engine import Connection, RowMapping

from media_archive._transfer_files import Destination
from media_archive.audit import _Scan, _scan
from media_archive.backup_restore import inspect_restored_archives, record_restored_archives
from media_archive.config import Configuration
from media_archive.devices import MountedVolume, registered_volume, staging_volume, verify_volume
from media_archive.import_journal import ImportOperation, commit_import, load_import_operation
from media_archive.operations import check_application_paths
from media_archive.phone_initialization import _archive_group, _archive_matches
from media_archive.sources import LocalSource, SourceDigest, hash_source_file, open_local_source
from media_archive.sort_execution import inspect_sort_recovery, load_sort_operation, record_restored_sort
from media_archive.state import DeviceKey, StateError, history, journal, open_database, sort_moves, transaction
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


def _scan_working(mounted: MountedVolume, root: Path, expected: dict[Path, SourceDigest],
                  *, label: str) -> dict[Path, SourceDigest]:
    try:
        with Destination(mounted, (root / "probe").relative_to(mounted.root)).parent():
            scan = _Scan()
            _scan(LocalSource(mounted, root), scan)
            if scan.errors:
                raise StateError(f"Nie ukończono odczytu katalogu {label} po przywróceniu bazy.")
    except FileNotFoundError:
        return {}
    found = {}
    for relative, entry in scan.entries.items():
        path = root / relative
        if stat.S_ISDIR(entry.mode):
            if path in expected:
                raise StateError(f"Katalog zajmuje ścieżkę pliku w {label}: {relative.as_posix()}.")
            continue
        if path not in expected or not stat.S_ISREG(entry.mode):
            raise StateError(f"Katalog {label} zawiera nierozpoznany wpis: {relative.as_posix()}.")
        digest = Destination(mounted, path.relative_to(mounted.root)).recover_digest()
        if digest != expected[path]:
            raise StateError(f"Treść pliku w {label} różni się od historii: {relative.as_posix()}.")
        found[path] = digest
    return found


def _scan_undated(config: Configuration, targets: tuple[RowMapping, ...]) -> dict[Path, SourceDigest]:
    root = config.local.undated_root
    local = next((volume for volume in config.local.volumes if root.is_relative_to(volume.root)), None)
    if local is None:
        if targets or root.exists():
            raise StateError("Katalog Do wyjaśnienia nie leży na zarejestrowanym woluminie.")
        return {}
    key = DeviceKey("local", local.alias)
    volume_id = registered_volume(config, key).volume_id
    expected = {Path(entry.path): SourceDigest(entry.sha256, entry.size) for entry in targets
                if entry.volume_id == volume_id and Path(entry.path).is_relative_to(root)}
    try:
        with verify_volume(config, key) as mounted:
            return _scan_working(mounted, root, expected, label="Do wyjaśnienia")
    except OSError:
        raise StateError("Nie można bezpiecznie sprawdzić katalogu Do wyjaśnienia.") from None


def reconcile_imports(config: Configuration, audit_ids: Iterable[str] = ()) -> dict[str, object]:
    """Reconcile local work and known archive locations before clearing the restore requirement."""
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
            sort_ids = {move.operation_id for move in sort_moves.load_moves(connection) if not move.cleanup_done
                        } if sort_moves.supported(connection) else set()
            operation_ids = {operation.id for operation in operations} | sort_ids
            resources = {resource for operation_id in operation_ids
                         for resource in journal.load_resources(connection, operation_id)}
        resources.add(Resource("collection", str(canonical_path(staging))))
        resources.add(Resource("collection", str(canonical_path(config.local.undated_root))))
        resources.update(collection_resource(config, phone.collection_id) for phone in config.phones)
        resources.update(Resource("source", str(source_id)) for source_id in phones.values())
        resources.update(Resource("archive", group.id) for group in config.archive_groups)
        with lock_resources(database, resources) as locks, open_database(database, writable=True) as connection:
            validate_registered_configuration(config)
            with connection.begin():
                if load_reconciliation(connection) != restored:
                    raise StateError("Stan przywrócenia zmienił się przed rozpoczęciem uzgodnienia.")
                unfinished = journal.load_unfinished(connection)
                planned_sort = [load_sort_operation(connection, operation_id) for operation_id in sorted(sort_ids)]
                backup = tuple(row for row in unfinished if journal.backup_retry_command(row))
                operations = [load_import_operation(connection, row.id) for row in unfinished
                              if row.id not in sort_ids and not journal.backup_retry_command(row)]
                imports = history.load_staged_imports(connection)
                undated = sort_moves.undated_locations(connection)
                known_undated = sort_moves.undated_targets(connection)
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
            for row in [*(entry for entry in pending if entry.id not in undated),
                        *(entry.target for entry in operations)]:
                path = Path(row.path)
                if row.volume_id != volume_id or not path.is_relative_to(staging) or path == staging:
                    raise StateError("Oczekujący materiał należy do innego woluminu lub katalogu stagingu.")
            for entry in imports:
                if entry.volume_id == volume_id and Path(entry.path).is_relative_to(staging):
                    expected[Path(entry.path)] = SourceDigest(entry.sha256, entry.size)
            try:
                with verify_volume(config, key) as mounted:
                    found = _scan_working(mounted, staging, expected, label="staging")
            except OSError:
                raise StateError("Nie można bezpiecznie sprawdzić stagingu po przywróceniu bazy.") from None
            found_undated = _scan_undated(config, known_undated)
            archive_evidence = inspect_restored_archives(config, connection, backup)
            sort_evidence = []
            for operation in planned_sort:
                with open_database(database) as read:
                    try:
                        digest = inspect_sort_recovery(config, read, operation)
                    except StateError as error:
                        raise StateError(f"Operacja SORT {operation.operation.id} wymaga sprawdzenia. {error}") from None
                sort_evidence.append((operation, digest))
            recovered_sorts = {operation.move.import_id for operation, digest in sort_evidence if digest is not None}
            missing_material = []
            for entry in pending:
                if entry.id in recovered_sorts:
                    continue
                current = undated.get(entry.id)
                present = Path(current.path) in found_undated if current is not None else Path(entry.path) in found
                if not present:
                    missing_material.append({"id_importu": entry.id, "staging": current.path if current else entry.path})
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
                for operation, digest in sort_evidence:
                    record_restored_sort(connection, locks, operation, digest)
                archives = record_restored_archives(connection, locks, archive_evidence)
            return {
                "Generacja": restored.generation_id, "Pełne audyty użyte do uzgodnienia": audits,
                "Zweryfikowane pliki stagingu": len(found), "Zatwierdzone próby": matched,
                "Próby z potwierdzonym brakiem celu": absent,
                "Sprawdzone operacje SORT": len(sort_evidence),
                "Brakujące materiały wymagające jawnego reimportu": missing_material,
                **archives,
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
