"""Database setup, upgrades and read-only journal inspection for the CLI."""

from pathlib import Path
import stat

from media_archive._encoding import require_utf8
from media_archive.config import Configuration
from media_archive.state import StateError, migrate_database, open_database, schema_version
from media_archive.state.journal import load_attempts, load_operation, load_unfinished, recommended_recovery
from media_archive.state.migrations import _head_revision
from media_archive.state.registry import initialize_database, validate_registered_configuration


def _resolved(path: Path) -> Path:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise StateError("Nie można rozpoznać ścieżki katalogu aplikacji.") from None
    require_utf8(str(resolved), StateError)
    return resolved


def _overlap(first: Path, second: Path) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def check_application_paths(config: Configuration, *, reports: bool = False) -> None:
    """Check physical separation before opening state or writing generated files."""
    state = _resolved(config.app.state_dir)
    settings = _resolved(config.source_file)
    media = [_resolved(path) for path in (
        config.local.staging_root, config.local.undated_root,
        *(volume.root for volume in config.local.volumes),
        *(collection.local_root for collection in config.collections),
        *(target.mount for group in config.archive_groups for target in group.targets),
    )]
    protected = [settings, *media]
    if any(_overlap(state, path) for path in protected):
        raise StateError("Katalog stanu koliduje z konfiguracją lub ścieżką danych.")
    if reports:
        try:
            mode = config.app.reports_dir.lstat().st_mode
        except FileNotFoundError:
            pass
        except OSError:
            raise StateError("Nie można sprawdzić ścieżki katalogu raportów.") from None
        else:
            if stat.S_ISLNK(mode):
                raise StateError("Katalog raportów nie może być dowiązaniem symbolicznym.")
            if not stat.S_ISDIR(mode):
                raise StateError("Ścieżka raportów musi wskazywać na katalog.")
        report_dir = _resolved(config.app.reports_dir)
        reserved = [state / f"state.sqlite3{suffix}" for suffix in ("", "-journal", "-wal", "-shm")]
        if state.is_relative_to(report_dir) or any(_overlap(report_dir, path) for path in [*reserved, *protected]):
            raise StateError("Katalog raportów koliduje z inną ścieżką aplikacji.")


def setup(config: Configuration, *, dry_run: bool) -> dict[str, object]:
    path = config.app.database_file
    try:
        path.lstat()
    except FileNotFoundError:
        exists = False
    except OSError:
        raise StateError("Nie można sprawdzić położenia bazy stanu.") from None
    else:
        exists = True
    if exists:
        if not dry_run:
            raise StateError("Baza już istnieje. Użyj istniejącej instalacji.")
        with open_database(path) as connection:
            current = schema_version(connection)
        details: dict[str, object] = {"Baza stanu": str(path), "Rewizja": current}
        if current == _head_revision():
            validate_registered_configuration(config)
            details["Rejestr"] = "konfiguracja zgodna z rejestrem"
        else:
            details["Rejestr"] = "porównanie wymaga jawnej migracji"
        return details
    if not dry_run:
        try:
            config.app.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError:
            raise StateError("Nie można utworzyć katalogu stanu.") from None
        initialize_database(config)
    return {
        "Baza stanu": str(path),
        "Profile źródeł": len(config.phones) + len(config.cameras),
        "Zbiory": len(config.collections),
        "Grupy archiwalne": len(config.archive_groups),
    }


def migrate(config: Configuration, *, dry_run: bool) -> dict[str, object]:
    if not dry_run:
        return {"Rewizja": migrate_database(config.app.database_file)}
    with open_database(config.app.database_file) as connection:
        before = schema_version(connection)
    return {"Rewizja bieżąca": before, "Rewizja docelowa": _head_revision()}


def pending(config: Configuration, operation_id: int | None = None) -> dict[str, object]:
    with open_database(config.app.database_file) as connection:
        if schema_version(connection) in ("0001", "0002", "0003"):
            raise StateError("Podgląd operacji wymaga jawnej migracji bazy.")
        operations = load_unfinished(connection) if operation_id is None else (load_operation(connection, operation_id),)
        result = []
        for operation in operations:
            attempts = load_attempts(connection, operation.id)
            last = attempts[-1] if attempts else None
            if last is not None and last.state == "committed":
                raise StateError("Operacja ma już zatwierdzony wynik.")
            item = {
                "id_operacji": operation.id,
                "stan": last.state if last else "planned",
                "id_uruchomienia": last.run_id if last else None,
            }
            if operation_id is not None:
                recovery = recommended_recovery(last)
                if recovery == "restart":
                    step = 'Po uzyskaniu blokad jawnie przejmij próbę w trybie recovery="restart" i wznów transfer.'
                elif recovery == "reconcile":
                    step = 'Po uzyskaniu blokad uruchom odzyskiwanie w trybie recovery="reconcile" i sprawdź plik docelowy.'
                else:
                    step = "Uzyskaj blokady i rozpocznij próbę transferu."
                item["następny_krok"] = step
            result.append(item)
        return {"Niedokończone operacje": result}
