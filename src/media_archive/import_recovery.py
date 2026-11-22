"""Resume one import using its durable intent and the shared transfer."""

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from media_archive.config import Configuration
from media_archive.devices import registered_volume
from media_archive.import_journal import ImportOperation, commit_import, load_import_operation
from media_archive.operations import check_application_paths
from media_archive.phone_import import finish_phone_baseline
from media_archive.sources import AdbSource, LocalSource, ReadCancelled, open_adb_source, open_camera_source
from media_archive.state import DeviceKey, StateError, journal, open_database
from media_archive.state.locking import Resource, canonical_path, installation_lock, lock_resources
from media_archive.state.reconciliation import require_reconciled
from media_archive.transfer import execute_transfer


@contextmanager
def _open_source(config: Configuration, operation: ImportOperation, card: str | None,
                 mount: Path | None) -> Generator[tuple[LocalSource | AdbSource, Path], None, None]:
    profile = operation.profile
    origin = operation.source
    path = Path(origin.path)
    if profile.kind == "camera":
        if card is None or mount is None:
            raise StateError("Ponowienie odczytu wymaga oryginalnej karty oraz --card i --mount.")
        try:
            camera = config.camera(profile.alias)
        except KeyError:
            raise StateError("Profil kamery nie występuje w bieżącej konfiguracji.") from None
        device = registered_volume(config, DeviceKey("card", card))
        if (device.device_id != origin.device_id or device.volume_id != origin.volume_id
                or not path.is_relative_to(camera.source_relative)):
            raise StateError("Karta, jej wolumin lub katalog różni się od zapisanego źródła próby.")
        with open_camera_source(config, profile.alias, card, mount) as source:
            yield source, path.relative_to(camera.source_relative)
    else:
        try:
            phone = config.phone(profile.alias)
        except KeyError:
            raise StateError("Profil telefonu nie występuje w bieżącej konfiguracji.") from None
        device = registered_volume(config, DeviceKey("phone", profile.alias))
        if phone.device is None or device.device_id != origin.device_id or not path.is_relative_to(phone.device.camera_dir):
            raise StateError("Telefon lub katalog różni się od zapisanego źródła próby.")
        with open_adb_source(config, profile.alias) as source:
            yield source, path.relative_to(phone.device.camera_dir)


def resume_import(config: Configuration, operation_id: int, *, card: str | None = None, mount: Path | None = None,
                  progress: Callable[[int, int], None] | None = None) -> tuple[int, str, dict[str, object]]:
    check_application_paths(config, device_mount=mount)
    database = config.app.database_file
    with installation_lock(database):
        with open_database(database) as connection:
            require_reconciled(connection)
            resources = journal.load_resources(connection, operation_id)
        staging = Resource("collection", str(canonical_path(config.local.staging_root)))
        with lock_resources(database, (*resources, staging)) as locks:
            with open_database(database) as connection:
                operation = load_import_operation(connection, operation_id)
            if card is not None and operation.profile.kind != "camera":
                raise StateError("Opcje karty są dostępne wyłącznie przy wznawianiu importu z kamery.")
            details = {"ID operacji": operation_id, "Profil źródła": operation.profile.alias}
            try:
                result = execute_transfer(
                    config, operation_id, locks=locks, progress=progress,
                    open_source=lambda: _open_source(config, operation, card, mount),
                    record_result=lambda connection, proof: commit_import(connection, operation_id, proof),
                    source_occurrence_id=operation.source.id,
                )
            except (KeyboardInterrupt, ReadCancelled):
                return 130, "Przerwano wznowienie importu.", details
            except StateError as error:
                return 3, f"Import oczekuje na wznowienie. {error}", details
            details.update({"ID próby": result.attempt_id, "ID weryfikacji": result.verification_id})
            if operation.profile.kind == "phone":
                try:
                    details["Inicjalizacja telefonu"] = {
                        "completed": "ukończona", "pending": "pozostają braki", "unchanged": "już ukończona",
                    }[finish_phone_baseline(config, operation.profile.id)]
                except StateError as error:
                    return 3, f"Kopia jest zatwierdzona. Nie zapisano zakończenia inicjalizacji. {error}", details
                except KeyboardInterrupt:
                    return 130, "Kopia jest zatwierdzona. Przerwano zamykanie inicjalizacji.", details
            return 0, "Wynik importu jest zatwierdzony i zweryfikowany.", details
