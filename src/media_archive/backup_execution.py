"""Execute controlled BACKUP trials against explicitly selected test targets."""

from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import errno
from hashlib import sha256
import json
from pathlib import Path
from threading import Event, RLock

from sqlalchemy.engine import Connection

from media_archive._transfer_files import Destination, InsufficientSpace
from media_archive.backup_capacity import PINNED_COPY_CONFLICT, BackupCopy
from media_archive.backup_cleanup import Approval, execute_cleanup
from media_archive.backup_planning import BackupFile, BackupPlan, _local_mount, plan_backup
from media_archive.config import ConfigError, Configuration
from media_archive.devices import MountedVolume, registered_volume, verify_volume
from media_archive.sort_conflicts import require_destination_name
from media_archive.sources import LocalSource, ReadCancelled, SourceDigest, open_local_source
from media_archive.state import DeviceKey, StateError, history, journal, open_database, transaction
from media_archive.state.locking import OperationLocks, Resource, collection_resource, lock_resources
from media_archive.state.reconciliation import require_reconciled
from media_archive.transfer import ArchiveRead, TransferResult, _open_under_gate, execute_transfer


@dataclass
class _CopyOutcome:
    copy: BackupCopy
    report: dict[str, object]
    read: ArchiveRead | None = None


def _run_copy(config: Configuration, group: str, file: BackupFile, copy: BackupCopy,
              locks: OperationLocks, resources: frozenset[Resource], *, fresh: bool,
              cancelled: Callable[[], bool] | None,
              state_gate: AbstractContextManager[object]) -> tuple[int, int | None, ArchiveRead | None]:
    local = next(volume for volume in config.local.volumes if file.path.is_relative_to(volume.root))
    local_key = DeviceKey("local", local.alias)
    with state_gate:
        source = registered_volume(config, local_key)
        target = registered_volume(config, DeviceKey("archive", copy.target, group))
    expected = copy.digest
    if expected is None or source.volume_id is None or target.volume_id is None or target.device_id is None:
        raise StateError("Próba BACKUP wymaga znanej treści i zarejestrowanych woluminów.")
    database = config.app.database_file
    replacement = file.changes == "replace_verified"
    scope = config.collection(file.collection_id).local_root
    checked_occurrence = None

    @contextmanager
    def open_source() -> Generator[tuple[LocalSource, Path], None, None]:
        with open_local_source(config, local_key, scope=scope) as reader:
            yield reader, file.path.relative_to(scope)

    def finish(operation_id: int, local_id: int, *, verify_existing_only: bool) -> TransferResult:
        def record_result(connection: Connection, verification: int) -> None:
            history.record_archive_copy(connection, local_id, verification, update_presence=replacement)

        return execute_transfer(config, operation_id, locks=locks, open_source=open_source, record_result=record_result,
                                verify_existing_only=verify_existing_only, cancelled=cancelled, state_gate=state_gate)

    if replacement:
        with state_gate, open_database(database) as connection:
            pending = journal.load_pending_replacements(connection, target.volume_id, copy.archive_path.as_posix())
        observed = expected if copy.action == "verified" else copy.previous
        for earlier in pending:
            earlier_digest = SourceDigest(earlier.sha256, earlier.size)
            if earlier_digest == expected:
                continue  # The same intent is resumed below.
            if earlier_digest != observed:
                raise StateError(f"Podmianę blokuje niedokończona operacja BACKUP {earlier.id}. Sprawdź cel i zamiar operacji.")
            finish(earlier.id, json.loads(earlier.intent)["local_id"], verify_existing_only=True)

    with state_gate, open_database(database, writable=True) as connection, transaction(connection):
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
                    if not fresh:
                        return operation.id, None, None
                    checked_occurrence = operation.target_occurrence_id
                    break
            operation = successor
        if operation is None:
            occurrence = history.record_occurrence(connection, "archive", copy.archive_path.as_posix(),
                                                   content_id=content, volume_id=target.volume_id)
            operation_id = journal.plan_operation(connection, locks, config, operation_key=token, intent=json.dumps(intent),
                                                  target_occurrence_id=occurrence, required_resources=resources)
        else:
            operation_id = operation.id

    if checked_occurrence is not None:
        observed = _read_archive_copy(config, group, copy, state_gate, occurrence=checked_occurrence,
                                      local_id=local_id)
        if observed is None:
            raise StateError("Zgodna kopia zmieniła się po podglądzie. Odśwież plan pojemności.")
        proof, read = observed
        return operation_id, proof, read
    result = finish(operation_id, local_id, verify_existing_only=copy.action == "verified")
    return operation_id, result.verification_id, result.archive_read


def _fresh_copy_status(config: Configuration, group: str, files: dict[Path, BackupFile],
                       outcomes: list[_CopyOutcome]) -> tuple[list[dict[str, object]], bool]:
    """Check that the reads from this invocation still describe the required files."""
    reads: dict[Path, list[_CopyOutcome]] = {}
    for outcome in outcomes:
        if outcome.read is not None:
            reads.setdefault(outcome.copy.source, []).append(outcome)
    rows = []
    invalidated = False
    with ExitStack() as stack:
        mounts: dict[DeviceKey, MountedVolume] = {}
        archives: dict[str, MountedVolume | None] = {}
        for path, file in files.items():
            proofs = []
            disks = set()
            matches = reads.get(path, ())
            for match in matches:
                alias = match.copy.target
                proof = match.read
                assert proof is not None
                try:
                    if alias not in archives:
                        archives[alias] = None
                        archives[alias] = stack.enter_context(
                            verify_volume(config, DeviceKey("archive", alias, group)),
                        )
                    volume = archives[alias]
                    if (volume is None or proof.mount_id != volume.mount_id
                            or proof.physical_device != volume.physical_device
                            or volume.physical_device in disks
                            or LocalSource(volume, None).attributes(match.copy.archive_path) != proof.entry):
                        raise StateError("Kopia lub nośnik zmieniły się po weryfikacji.")
                except (ConfigError, StateError, OSError):
                    invalidated = True
                    match.report["błąd końcowej weryfikacji"] = "Nie potwierdzono bieżącego stanu kopii."
                else:
                    disks.add(volume.physical_device)
                    proofs.append({"cel": alias, "ścieżka": match.copy.archive_path.as_posix(),
                                   "dowód": match.report["dowód SHA-256"]})
            if matches:
                scope = config.collection(file.collection_id).local_root
                relative = path.relative_to(scope)
                try:
                    local = LocalSource(_local_mount(config, stack, mounts, path), scope)
                    if local.attributes(relative) != replace(file.entry, path=relative):
                        raise StateError("Źródło zmieniło się od planowania BACKUP.")
                except (ConfigError, StateError, OSError):
                    invalidated = True
                    proofs.clear()
                    for match in matches:
                        match.report["błąd końcowej weryfikacji"] = "Nie potwierdzono bieżącego stanu źródła."
            readiness = "brak dwóch świeżych kopii"
            if len(proofs) == 2:
                readiness = ("po usunięciu pliku lokalnego kolejny BACKUP może zaproponować usunięcie kopii z archiwów"
                             if file.deletions == "confirm_each_run" else "można ręcznie zwolnić miejsce")
            rows.append({"źródło": str(path), "status kopii": f"{len(proofs)}/2", "kopie": proofs,
                         "ręczne zwolnienie miejsca": readiness})
    return rows, invalidated


def _read_archive_copy(config: Configuration, group: str, copy: BackupCopy, state_gate: AbstractContextManager[object], *,
                       occurrence: int | None = None, local_id: int | None = None) -> tuple[int, ArchiveRead] | None:
    """Record a fresh read without a transfer or a new publication attempt."""
    key = DeviceKey("archive", copy.target, group)
    with state_gate:
        binding = registered_volume(config, key)
    if binding.device_id not in copy.required_pair.devices:
        raise StateError("Sprawdzany wolumin nie należy do zapisanej pary BACKUP.")
    with _open_under_gate(verify_volume(config, key), state_gate) as volume:
        destination = Destination(volume, copy.archive_path)
        require_destination_name(destination)
        observed = destination.recover_file(sync=False)
        if observed is None or observed[0] != copy.digest:
            return None
        digest, entry = observed
        with state_gate, open_database(config.app.database_file, writable=True) as connection, transaction(connection):
            run = history.create_run(connection)
            if occurrence is None:
                content = history.record_content(connection, digest.sha256, digest.size)
                occurrence = history.record_occurrence(connection, "archive", copy.archive_path.as_posix(),
                                                       content_id=content, volume_id=binding.volume_id)
            proof = history.record_verification(connection, occurrence, run, verified_at=datetime.now(timezone.utc),
                                                scope="full", read_sha256=digest.sha256, bytes_read=digest.size)
            if local_id is not None:
                history.record_archive_copy(connection, local_id, proof)
        return proof, ArchiveRead(entry, volume.mount_id, volume.physical_device)


def execute_backup_trial(config: Configuration, *, profile_id: str, targets: tuple[str, ...],
                         collection_id: str | None = None, month: str | None = None, pair: tuple[str, ...] = (),
                         copy_if_elsewhere: bool = False, replace_test: bool = False,
                         approve_cleanup: Approval | None = None, parallel_test: bool = False) -> BackupPlan:
    if not profile_id or not targets:
        raise StateError("Próba BACKUP wymaga jawnego profilu i celów.")
    if approve_cleanup is not None and (month is not None or replace_test or parallel_test):
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
        all_policies = parallel_test or approve_cleanup is not None
        plan = plan_backup(config, profile_id=profile_id, collection_id=collection_id, month=month,
                           targets=targets, pair=pair, prepare_replacements=replace_test or all_policies,
                           coordination_trial=parallel_test)
        if plan.code:
            return plan
        policy = "replace_verified" if replace_test else "keep_both"
        files = {file.path: file for file in plan.files if all_policies or file.changes == policy}
        candidates = [copy for copy in plan.copies if copy.source in files]
        if not copy_if_elsewhere and any(copy.elsewhere for copy in candidates):
            return replace(plan, code=3, message=(
                "Znaleziono historyczne kopie pod inną ścieżką. Sprawdź listę i ponów próbę z --copy-if-elsewhere."
            ))
        queues: dict[str, list[BackupCopy]] = {alias: [] for alias in plan.details["Wybrane cele"]}
        for copy in candidates:
            queues[copy.target].append(copy)
        stop = Event()
        interrupted = Event()
        space_failed = Event()
        # Keep the two workers from inspecting a SQLite journal while the other creates it.
        state_gate = RLock()
        use_workers = len(queues) > 1

        def run_target(queue: list[BackupCopy]) -> list[_CopyOutcome]:
            outcomes = []
            failed = any(copy.action == "blocked" for copy in queue)
            for copy in queue:
                row: dict[str, object] = {"źródło": str(copy.source), "cel": copy.target,
                                          "ścieżka": copy.archive_path.as_posix()}
                outcome = _CopyOutcome(copy, row)
                outcomes.append(outcome)
                if copy.action == "blocked":
                    row["błąd"] = PINNED_COPY_CONFLICT
                    continue
                if stop.is_set() or failed:
                    row["wynik"] = "pominięto"
                    continue
                try:
                    if copy.action == "check":
                        observed = _read_archive_copy(config, profile.archive_group_id, copy, state_gate)
                        if observed is None:
                            row["błąd"] = "Na celu tylko do odczytu brakuje zgodnej kopii pod wymaganą ścieżką."
                        else:
                            row["dowód SHA-256"], outcome.read = observed
                            row["wynik"] = "zgodna · tylko do odczytu"
                        continue
                    operation_id, proof_id, read = _run_copy(
                        config, profile.archive_group_id, files[copy.source], copy, locks, resources,
                        fresh=parallel_test, cancelled=stop.is_set if use_workers else None, state_gate=state_gate,
                    )
                except KeyboardInterrupt:
                    interrupted.set()
                    stop.set()
                    row["wynik"] = "przerwano"
                except ReadCancelled:
                    row["wynik"] = "wstrzymano"
                except (ConfigError, StateError, OSError) as error:
                    if isinstance(error, InsufficientSpace) or isinstance(error, OSError) and error.errno == errno.ENOSPC:
                        space_failed.set()
                        stop.set()
                    failed = True
                    row["błąd"] = (str(error) if isinstance(error, (ConfigError, StateError))
                                   else "Nie można bezpiecznie wykonać kopii.")
                else:
                    row["operacja"] = operation_id
                    row["wynik"] = {"verified": "zgodna", "replace": "zastąpiono"}.get(copy.action, "skopiowano")
                    if read is not None and parallel_test:
                        outcome.read = read
                        row["dowód SHA-256"] = proof_id
            return outcomes

        if use_workers:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_target, queue) for queue in queues.values()]
                try:
                    wait(futures)
                except KeyboardInterrupt:
                    interrupted.set()
                    stop.set()
            outcomes = [outcome for future in futures for outcome in future.result()]
        else:
            outcomes = [outcome for queue in queues.values() for outcome in run_target(queue)]
        results = [outcome.report for outcome in outcomes]
        details = {**plan.details, "Wyniki BACKUP": results}
        errors = any("błąd" in row or row.get("wynik") == "wstrzymano" for row in results)
        if parallel_test:
            rows, invalidated = _fresh_copy_status(config, profile.archive_group_id, files, outcomes)
            details["Stan kopii BACKUP"] = rows
            errors = errors or invalidated or set(targets) != queues.keys()
        if interrupted.is_set():
            return replace(plan, code=130, message="Przerwano próbę BACKUP.", details=details)
        if space_failed.is_set():
            return replace(plan, code=3, message="Zatrzymano BACKUP na wszystkich celach. Brak miejsca.", details=details)
        if errors:
            return replace(plan, code=3, message="Próba BACKUP zakończona częściowo.", details=details)
        if parallel_test:
            return replace(plan, message="Zakończono próbę koordynacji BACKUP.", details=details)
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
