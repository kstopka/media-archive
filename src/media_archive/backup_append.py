"""Execute append-only backup previews against explicitly selected test targets."""

from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
import json

from media_archive.backup_capacity import BackupCopy
from media_archive.backup_planning import BackupFile, BackupPlan, plan_backup
from media_archive.config import Configuration
from media_archive.devices import registered_volume
from media_archive.sources import open_local_source
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

        intent = json.dumps({"kind": "backup_append", "local_id": local_id}, ensure_ascii=False)
        identity = json.dumps((local_id, target.volume_id, copy.archive_path.as_posix()))
        token = "backup:" + sha256(identity.encode("utf-8")).hexdigest()
        operation = journal.find_operation(connection, token)
        while operation is not None:
            attempts = journal.load_attempts(connection, operation.id)
            if not attempts or attempts[-1].state != "committed":
                break
            token = f"backup:{operation.id}"
            successor = journal.find_operation(connection, token)
            if successor is None and copy.action == "verified":
                return operation.id
            operation = successor
        if operation is None:
            occurrence = history.record_occurrence(connection, "archive", copy.archive_path.as_posix(),
                                                   content_id=content, volume_id=target.volume_id)
            operation_id = journal.plan_operation(connection, locks, config, operation_key=token, intent=intent,
                                                  target_occurrence_id=occurrence, required_resources=resources)
        else:
            operation_id = operation.id

    scope = config.collection(file.collection_id).local_root

    @contextmanager
    def open_source():
        with open_local_source(config, local_key, scope=scope) as reader:
            yield reader, file.path.relative_to(scope)

    execute_transfer(config, operation_id, locks=locks, open_source=open_source,
                     verify_existing_only=copy.action == "verified",
                     record_result=lambda connection, verification: history.record_archive_copy(
                         connection, local_id, verification,
                     ))
    return operation_id


def execute_append(config: Configuration, *, profile_id: str, targets: tuple[str, ...],
                   collection_id: str | None = None, month: str | None = None, pair: tuple[str, ...] = (),
                   copy_if_elsewhere: bool = False) -> BackupPlan:
    if not profile_id or not targets:
        raise StateError("Próba BACKUP wymaga jawnego profilu i celów.")
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
                           targets=targets, pair=pair)
        if plan.code:
            return plan
        files = {file.path: file for file in plan.files if file.changes == "keep_both"}
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
            result["wynik"] = "zgodna" if copy.action == "verified" else "skopiowano"
            results.append(result)
        return replace(plan, message="Zakończono próbę BACKUP dla zbiorów dopisywanych.",
                       details={**plan.details, "Wyniki BACKUP": results,
                                "Pliki z polityką zastępowania poza próbą": len(plan.files) - len(files)})


def is_append_operation(config: Configuration, operation_id: int) -> bool:
    with open_database(config.app.database_file) as connection:
        return journal.load_operation(connection, operation_id).operation_key.startswith("backup:")
