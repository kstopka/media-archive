"""Import a registered Android phone and finish its baseline processing."""

from collections.abc import Callable, Iterable
from dataclasses import replace
import json
from pathlib import Path

from media_archive.camera_import import CameraImportResult, _import
from media_archive.config import ConfigError, Configuration
from media_archive.import_planning import ImportPlan, baseline_files, unfilled_baseline
from media_archive.sources import open_adb_source
from media_archive.state import StateError, open_database, transaction
from media_archive.state.history import create_run
from media_archive.state.initialization import (
    finish_initialization, load_initialization, record_initialization_progress,
)


def _finish(config: Configuration, plan: ImportPlan, result: CameraImportResult) -> CameraImportResult:
    try:
        with open_database(config.app.database_file, writable=True) as connection, transaction(connection):
            state = load_initialization(connection, plan.source_id)
            if state.status == "completed":
                return result
            comparison = next((scope for scope in state.scopes if scope.phase == "comparison"
                               and scope.scope == "copies" and scope.completed), None)
            if state.policy != "import_missing" or comparison is None or state.boundary is None:
                raise StateError("Brak zakończonego porównania bazowego telefonu.")
            files = baseline_files(connection, plan.source_id, state, comparison.cursor)
            device_id = json.loads(state.boundary)["device_id"]
            remaining = unfilled_baseline(connection, plan.source_id, device_id, files)
            if remaining:
                return replace(result, code=3, message="Część plików bazowych nadal oczekuje na import.")
            run_id = create_run(connection)
            record_initialization_progress(connection, plan.source_id, run_id, phase="processing",
                                           scope="baseline", completed=True)
            finish_initialization(connection, plan.source_id, run_id)
    except StateError as error:
        return replace(result, code=3, message=f"Nie zapisano zakończenia inicjalizacji telefonu. {error}")
    except KeyboardInterrupt:
        return replace(result, code=130, message="Przerwano zatwierdzanie inicjalizacji telefonu.")
    return replace(result, message="Inicjalizacja telefonu ukończona. Kolejny import obejmie nowe pliki.")


def import_phone(config: Configuration, profile: str, *, reimport: Iterable[str | Path] = (),
                 announce: Callable[[str], None] = lambda _: None,
                 cancelled: Callable[[], bool] | None = None) -> CameraImportResult:
    """Execute a phone import over ADB into verified staging."""
    try:
        config.phone(profile)
    except KeyError:
        raise ConfigError("Nie znaleziono skonfigurowanego profilu telefonu.") from None
    return _import(config, profile, "phone", lambda: open_adb_source(config, profile),
                   reimport, announce, cancelled, finish=_finish)
