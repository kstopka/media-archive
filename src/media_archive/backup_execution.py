"""Execute controlled BACKUP trials against explicitly selected test targets."""

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

from sqlalchemy.engine import Connection

from media_archive.backup_capacity import BackupCopy
from media_archive.backup_cleanup import Approval, execute_cleanup
from media_archive.backup_planning import BackupFile, BackupPlan, plan_backup
from media_archive.config import Configuration
from media_archive.devices import registered_volume
from media_archive.sources import LocalSource, SourceDigest, open_local_source
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.locking import OperationLocks, Resource, collection_resource, lock_resources
from media_archive.state.reconciliation import require_reconciled
from media_archive.transfer import execute_transfer


def _run_copy(config: Configuration, group: str, file: BackupFile, copy: BackupCopy,
              locks: OperationLocks, resources: frozenset[Resource]) -> int:
    local = next(volume for volume in config.local.volumes if file.path.is_relative_to(volume.root))
    local_key = DeviceKey("local", local.alias)
    source = registered_volume(config, local_key)
    target = registered_volume(config, DeviceKey("archive", copy.target, group))
    expected = copy.digest
    if expected is None or source.volume_id is None or target.volume_id is None or target.device_id is None:
        raise StateError("Próba BACKUP wymaga znanej treści i zarejestrowanych woluminów.")
    database = config.app.database_file
    replacement = file.changes == "replace_verified"
    scope = config.collection(file.collection_id).local_root

    @contextmanager
    def open_source() -> Generator[tuple[LocalSource, Path], None, None]:
        with open_local_source(config, local_key, scope=scope) as reader:
            yield reader, file.path.relative_to(scope)

    def finish(operation_id: int, local_id: int, *, verify_existing_only: bool) -> None:
        def record_result(connection: Connection, verification: int) -> None:
            history.record_archive_copy(connection, local_id, verification, update_presence=replacement)

        execute_transfer(config, operation_id, locks=locks, open_source=open_source, record_result=record_result,
                         verify_existing_only=verify_existing_only)

    if replacement:
        with open_database(database) as connection:
            pending = journal.load_pending_replacements(connection, target.volume_id, copy.archive_path.as_posix())
        observed = expected if copy.action == "verified" else copy.previous
        for earlier in pending:
            earlier_digest = SourceDigest(earlier.sha256, earlier.size)
            if earlier_digest == expected:
                continue  # The same intent is resumed below.
            if earlier_digest != observed:
                raise StateError(f"Podmianę blokuje niedokończona operacja BACKUP {earlier.id}. Sprawdź cel i zamiar operacji.")
            finish(earlier.id, json.loads(earlier.intent)["local_id"], verify_existing_only=True)

    with open_database(database, writable=True) as connection, transaction(connection):
        require_reconciled(connection)
        content = history.record_content(connection, expected.sha256, expected.size)
        locations = [row for row in history.load_occurrences(connection, "local", str(file.path))
                     if (row.content_id, row.volume_id, row.collection_id) == (content, source.volume_id, file.collection_id)]
        local_id = next((row.id for row in locations if history.load_archive_pair(connection, row.id)), None)
        if local_id is None:
            local_id = locations[-1].id if locations else history.record_occurrence(
                connection, "local", str(file.path), content_id=content,
                collection_id=file.collection_id, volume_id=source.volume_id,
            )
        first, second = sorted(copy.required_pair.devices)
        history.assign_archive_pair(connection, local_id, first, second, copy.required_pair.path.as_posix())
        history.pin_archive_path(connection, local_id, target.device_id, copy.archive_path.as_posix())

        prefix = journal.BACKUP_REPLACE_PREFIX if replacement else "backup:"
        intent: dict[str, object] = {"kind": "backup_replace" if replacement else "backup_append", "local_id": local_id}
        if replacement:
            intent["previous"] = [copy.previous.sha256, copy.previous.size] if copy.previous is not None else None
        identity = json.dumps((local_id, target.volume_id, copy.archive_path.as_posix()))
        token = prefix + sha256(identity.encode("utf-8")).hexdigest()
        operation = journal.find_operation(connection, token)
        while operation is not None:
            attempts = journal.load_attempts(connection, operation.id)
            if not attempts or attempts[-1].state != "committed":
                break
            token = f"{prefix}{operation.id}"
            successor = journal.find_operation(connection, token)
            if successor is None and copy.action == "verified":
                if not replacement or history.archive_copy_last_seen_present(connection, operation.target_occurrence_id):
                    return operation.id
            operation = successor
        if operation is None:
            occurrence = history.record_occurrence(connection, "archive", copy.archive_path.as_posix(),
                                                   content_id=content, volume_id=target.volume_id)
            operation_id = journal.plan_operation(connection, locks, config, operation_key=token, intent=json.dumps(intent),
                                                  target_occurrence_id=occurrence, required_resources=resources)
        else:
            operation_id = operation.id

    finish(operation_id, local_id, verify_existing_only=copy.action == "verified")
    return operation_id


def execute_backup_trial(config: Configuration, *, profile_id: str, targets: tuple[str, ...],
                         collection_id: str | None = None, month: str | None = None, pair: tuple[str, ...] = (),
                         copy_if_elsewhere: bool = False, replace_test: bool = False,
                         approve_cleanup: Approval | None = None) -> BackupPlan:
    if not profile_id or not targets:
        raise StateError("Próba BACKUP wymaga jawnego profilu i celów.")
    if approve_cleanup is not None and (month is not None or replace_test):
        raise StateError("Próba sprzątania wymaga zakresu Wszystko.")
    try:
        profile = config.backup_profile(profile_id)
    except KeyError:
        raise StateError("Nie znaleziono wybranego profilu BACKUP.") from None
    resources = frozenset({
        Resource("archive", profile.archive_group_id),
        *(collection_resource(config, root.id) for root in config.profile_collections(profile.id)),
    })
    with lock_resources(config.app.database_file, resources) as locks:
        with open_database(config.app.database_file) as connection:
            require_reconciled(connection)
        plan = plan_backup(config, profile_id=profile_id, collection_id=collection_id, month=month,
                           targets=targets, pair=pair, prepare_replacements=replace_test or approve_cleanup is not None)
        if plan.code:
            return plan
        policy = "replace_verified" if replace_test else "keep_both"
        files = {file.path: file for file in plan.files if approve_cleanup is not None or file.changes == policy}
        candidates = [copy for copy in plan.copies if copy.source in files]
        if not copy_if_elsewhere and any(copy.elsewhere for copy in candidates):
            return replace(plan, code=3, message=(
                "Znaleziono historyczne kopie pod inną ścieżką. Sprawdź listę i ponów próbę z --copy-if-elsewhere."
            ))
        results = []
        for copy in candidates:
            result = {"źródło": str(copy.source), "cel": copy.target, "ścieżka": copy.archive_path.as_posix()}
            try:
                result["operacja"] = _run_copy(config, profile.archive_group_id, files[copy.source], copy, locks, resources)
            except (StateError, OSError) as error:
                result["błąd"] = str(error) if isinstance(error, StateError) else "Nie można bezpiecznie wykonać kopii."
                results.append(result)
                return replace(plan, code=3, message="Próba BACKUP zakończona częściowo.",
                               details={**plan.details, "Wyniki BACKUP": results})
            except KeyboardInterrupt:
                return replace(plan, code=130, message="Przerwano próbę BACKUP.",
                               details={**plan.details, "Wyniki BACKUP": results})
            result["wynik"] = {"verified": "zgodna", "replace": "zastąpiono"}.get(copy.action, "skopiowano")
            results.append(result)
        if approve_cleanup is not None:
            code, message, details = execute_cleanup(config, profile, collection_id, targets, plan.files, approve_cleanup,
                                                    locks)
            return replace(plan, code=code, message=message,
                           details={**plan.details, "Wyniki BACKUP": results, **details})
        message = ("Zakończono próbę kontrolowanego zastępowania BACKUP." if replace_test
                   else "Zakończono próbę BACKUP dla zbiorów dopisywanych.")
        skipped = ("Pliki z polityką dopisywania poza próbą" if replace_test
                   else "Pliki z polityką zastępowania poza próbą")
        return replace(plan, message=message,
                       details={**plan.details, "Wyniki BACKUP": results, skipped: len(plan.files) - len(files)})


def backup_retry_command(config: Configuration, operation_id: int) -> str | None:
    with open_database(config.app.database_file) as connection:
        return journal.backup_retry_command(journal.load_operation(connection, operation_id))
