"""Camera entry point and shared execution for registered media sources."""

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, replace
import json
from pathlib import Path
from time import monotonic
from typing import Literal

from media_archive.config import ConfigError, Configuration
from media_archive.import_journal import commit_import, import_operation_key
from media_archive.import_planning import ImportItem, ImportPlan, plan_import
from media_archive.sources import AdbSource, LocalSource, ReadCancelled, open_camera_source
from media_archive.state import StateError, open_database, transaction
from media_archive.state import history, journal
from media_archive.state.locking import OperationLocks, Resource, canonical_path, installation_lock, lock_resources
from media_archive.transfer import execute_transfer


@dataclass(frozen=True)
class CameraImportItem:
    path: Path
    size: int
    status: Literal["imported", "failed", "interrupted"]
    operation_id: int | None
    error: str | None = None


@dataclass(frozen=True)
class CameraImportResult:
    code: int
    message: str
    plan: ImportPlan
    results: tuple[CameraImportItem, ...] = ()


def _prepare(config: Configuration, plan: ImportPlan, item: ImportItem, locks: OperationLocks,
             resource: Resource) -> int:
    if item.source_occurrence_id is None or item.destination is None or plan.staging_volume_id is None:
        raise StateError("Transfer wymaga trwałej obserwacji i zaplanowanego stagingu.")
    with open_database(config.app.database_file, writable=True) as connection, transaction(connection):
        source = history.load_occurrence(connection, item.source_occurrence_id)
        # The staging destination owns the operation. A retry can supply the
        # same content from a different folder or volume episode of the card.
        operation_key = import_operation_key(plan.kind, plan.staging_volume_id, item.destination)
        existing = journal.find_operation(connection, operation_key)
        if existing is None:
            occurrence_id = history.record_occurrence(connection, "staging", item.destination.as_posix(),
                                                      content_id=source.content_id, volume_id=plan.staging_volume_id)
            intent = json.dumps({"kind": f"{plan.kind}_import", "source_occurrence_id": source.id})
        else:
            occurrence_id, intent = existing.target_occurrence_id, existing.intent
            target = history.load_occurrence(connection, occurrence_id)
            if target.content_id != source.content_id:
                raise StateError("Zapisana operacja stagingu dotyczy innej treści.")
        return journal.plan_operation(connection, locks, config, operation_key=operation_key, intent=intent,
                                      target_occurrence_id=occurrence_id, required_resources=[resource])


def import_camera(config: Configuration, profile: str, *, card: str, mount: Path,
                  reimport: Iterable[str | Path] = (), announce: Callable[[str], None] = lambda _: None,
                  cancelled: Callable[[], bool] | None = None) -> CameraImportResult:
    """Copy and verify the eligible files, stopping the batch at its first failure."""
    try:
        config.camera(profile)
    except KeyError:
        raise ConfigError("Nie znaleziono skonfigurowanego profilu kamery.") from None
    return _import(config, profile, "camera", lambda: open_camera_source(config, profile, card, mount),
                   reimport, announce, cancelled, card=card, mount=mount)


def _import(config: Configuration, profile: str, kind: Literal["camera", "phone"],
            open_source: Callable[[], AbstractContextManager[LocalSource | AdbSource]],
            reimport: Iterable[str | Path], announce: Callable[[str], None],
            cancelled: Callable[[], bool] | None, *, card: str | None = None, mount: Path | None = None,
            full_read: bool = False,
            finish: Callable[[Configuration, ImportPlan, CameraImportResult], CameraImportResult] | None = None,
            ) -> CameraImportResult:
    database = config.app.database_file
    with installation_lock(database):
        with open_database(database) as connection:
            source_id = history.load_source_id(connection, kind, profile)
        resource = Resource("source", str(source_id))
        staging = Resource("collection", str(canonical_path(config.local.staging_root)))
        with lock_resources(database, [resource, staging]) as locks:
            plan = plan_import(config, profile, card=card, mount=mount, reimport=reimport, record=True,
                               full_read=full_read, locks=locks, announce=announce, cancelled=cancelled)
            if not plan.ready:
                return CameraImportResult(3, "Import jest zablokowany przez plan.", plan)
            if not plan.transfers:
                result = CameraImportResult(0, "Brak nowych plików do importu.", plan)
            else:
                result = _execute(config, open_source, plan, locks, resource, announce, cancelled)
            if result.code == 0 and any(not item.reimport_planned for item in plan.missing_staging):
                return replace(result, code=3, message="Część wcześniejszych importów nadal wymaga jawnego reimportu.")
            return finish(config, plan, result) if finish is not None and result.code == 0 else result


def _execute(config: Configuration, open_source: Callable[[], AbstractContextManager[LocalSource | AdbSource]],
             plan: ImportPlan, locks: OperationLocks,
             resource: Resource, announce: Callable[[str], None],
             cancelled: Callable[[], bool] | None) -> CameraImportResult:
    results = []
    transfers = plan.transfers
    label = "z karty" if plan.kind == "camera" else "z telefonu"
    try:
        with open_source() as source:
            for index, item in enumerate(transfers, start=1):
                if cancelled is not None and cancelled():
                    raise ReadCancelled("Anulowano import.")
                operation = None
                last_message = monotonic()

                def progress(done: int, total: int) -> None:
                    nonlocal last_message
                    now = monotonic()
                    if now - last_message >= 1:
                        announce(f"Plik {index}/{len(transfers)} · weryfikacja źródła · {done}/{total} bajtów.")
                        last_message = now

                try:
                    announce(f"Import {label} · plik {index}/{len(transfers)} · {item.size} bajtów.")
                    operation = _prepare(config, plan, item, locks, resource)
                    execute_transfer(config, operation, locks=locks, progress=progress, cancelled=cancelled,
                                     source_occurrence_id=item.source_occurrence_id,
                                     open_source=lambda: nullcontext((source, item.relative_path)),
                                     record_result=lambda connection, proof: commit_import(connection, operation, proof))
                except (KeyboardInterrupt, ReadCancelled):
                    results.append(CameraImportItem(item.relative_path, item.size, "interrupted", operation))
                    raise
                except StateError as error:
                    results.append(CameraImportItem(item.relative_path, item.size, "failed", operation, str(error)))
                    return CameraImportResult(3, "Import zatrzymany po błędzie pliku.", plan, tuple(results))
                results.append(CameraImportItem(item.relative_path, item.size, "imported", operation))
    except (KeyboardInterrupt, ReadCancelled):
        return CameraImportResult(130, f"Przerwano import {label}.", plan, tuple(results))
    except StateError as error:
        return CameraImportResult(3, str(error), plan, tuple(results))
    return CameraImportResult(0, f"Import {label} ukończony.", plan, tuple(results))
