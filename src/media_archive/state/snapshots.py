"""Create, validate and explicitly restore private state generations."""

from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
from tempfile import TemporaryDirectory
from time import monotonic
from uuid import uuid4

from sqlalchemy.engine import Connection

from media_archive._encoding import require_utf8
from media_archive.config import Configuration
from media_archive.config.validation import validate_relationships
from media_archive.config.loader import _parse_config

from ._installation import installation_lock
from .database import _check_sidecars, _database_path, _sync_directory, migrate_database, open_database, transaction
from .errors import DatabasePublishedError, StateError
from .locking import canonical_path
from .migrations import _head_revision, schema_version
from .reconciliation import restore_state
from .registry import _validate_registered_configuration

_DATABASE = "state.sqlite3"
_CONFIG = "settings.env"
_MANIFEST = "manifest.json"
_FORMAT = "media-archive/state-v1"


class SnapshotPublishedError(StateError):
    """The generation is visible, but directory sync failed."""


@dataclass(frozen=True)
class Generation:
    generation_id: str
    created_at: str
    revision: str
    database_sha256: str
    config_sha256: str
    config_path: str


@dataclass(frozen=True)
class RestoreResult:
    generation_id: str
    previous_database: Path | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def _write_private(path: Path, data: bytes) -> None:
    path.touch(mode=0o600, exist_ok=False)
    path.write_bytes(data)


def _copy_private(source: Path, destination: Path) -> None:
    destination.touch(mode=0o600, exist_ok=False)
    shutil.copyfile(source, destination)


def _sync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _sync_generation(directory: Path) -> None:
    for name in (_DATABASE, _CONFIG, _MANIFEST):
        _sync_file(directory / name)
    _sync_directory(directory)


def _validate_database(path: Path, config: Configuration | None = None) -> str:
    with open_database(path) as connection:
        revision = schema_version(connection)
        if connection.exec_driver_sql("PRAGMA main.integrity_check").all() != [("ok",)]:
            raise StateError("Kopia bazy nie przeszła kontroli integralności.")
        if connection.exec_driver_sql("PRAGMA main.foreign_key_check").first() is not None:
            raise StateError("Kopia bazy zawiera niespójne powiązania.")
        if config is not None and revision != "0001":
            _validate_registered_configuration(connection, config)
        return revision


def _captured_config(directory: Path, source: Path) -> Configuration:
    try:
        text = (directory / _CONFIG).read_text(encoding="utf-8-sig")
    except UnicodeError:
        raise StateError("Konfiguracja w generacji musi być zapisana w UTF-8.") from None
    return _parse_config(source, text)


def _read_manifest(directory: Path) -> Generation:
    try:
        payload = json.loads((directory / _MANIFEST).read_text(encoding="utf-8"))
        if payload.pop("format") != _FORMAT:
            raise ValueError
        generation = Generation(**payload)
        if not isinstance(generation.config_path, str) or not Path(generation.config_path).is_absolute():
            raise ValueError
        if not re.fullmatch(r"[0-9a-f]{32}", generation.generation_id):
            raise ValueError
        if datetime.fromisoformat(generation.created_at).utcoffset() != timezone.utc.utcoffset(None):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError, RecursionError):
        raise StateError("Niepoprawny manifest generacji stanu.") from None
    return generation


def _check_generation_files(directory: Path) -> None:
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise StateError("Generacja musi wskazywać na katalog.")
    for name in (_DATABASE, _CONFIG, _MANIFEST):
        if not stat.S_ISREG((directory / name).lstat().st_mode):
            raise StateError("Generacja wymaga zwykłych plików bazy, konfiguracji i manifestu.")
    _check_sidecars(directory / _DATABASE, allow_journal=False)


def validate_snapshot(directory: Path) -> Generation:
    require_utf8(str(directory), StateError)
    try:
        _check_generation_files(directory)
        generation = _read_manifest(directory)
        if (_digest(directory / _DATABASE), _digest(directory / _CONFIG)) != (
            generation.database_sha256, generation.config_sha256,
        ):
            raise StateError("Sumy kontrolne generacji nie zgadzają się z manifestem.")
        if _validate_database(
            directory / _DATABASE, _captured_config(directory, Path(generation.config_path)),
        ) != generation.revision:
            raise StateError("Rewizja bazy nie zgadza się z manifestem generacji.")
        return generation
    except OSError:
        raise StateError("Nie można odczytać generacji stanu.") from None


def _protected_paths(config: Configuration) -> tuple[Path, ...]:
    return tuple(map(canonical_path, (
        config.source_file,
        config.local.staging_root, config.local.undated_root,
        *(volume.root for volume in config.local.volumes),
        *(collection.local_root for collection in config.collections),
        *(target.mount for group in config.archive_groups for target in group.targets),
    )))


def _backup_database(source: Connection, destination: Path) -> None:
    deadline = monotonic() + 30

    def check_progress(status: int, remaining: int, total: int) -> None:
        if status != sqlite3.SQLITE_DONE and monotonic() >= deadline:
            raise StateError("Nie można ukończyć kopii SQLite w wyznaczonym czasie.")

    destination.touch(mode=0o600, exist_ok=False)
    with closing(sqlite3.connect(destination)) as target:
        source.connection.driver_connection.backup(target, pages=256, progress=check_progress, sleep=0.1)


def prepare_snapshot(config: Configuration, directory: Path, *, settings: bytes | None = None) -> Generation:
    """Build one synchronized generation in an empty private directory."""
    with open_database(config.app.database_file) as source:
        if settings is None:
            _copy_private(config.source_file, directory / _CONFIG)
        else:
            _write_private(directory / _CONFIG, settings)
        _backup_database(source, directory / _DATABASE)
    revision = _validate_database(directory / _DATABASE, _captured_config(directory, config.source_file))
    if revision == "0001":
        raise StateError("Przed utworzeniem snapshotu zaktualizuj bazę do rewizji z rejestrem.")
    generation = Generation(
        uuid4().hex, _now(), revision, _digest(directory / _DATABASE), _digest(directory / _CONFIG),
        str(config.source_file),
    )
    manifest = json.dumps({"format": _FORMAT, **asdict(generation)}, ensure_ascii=False, indent=2)
    _write_private(directory / _MANIFEST, (manifest + "\n").encode("utf-8"))
    _sync_generation(directory)
    return generation


def create_snapshot(config: Configuration, directory: Path) -> Path:
    """Publish a verified generation under an existing, trusted output directory."""
    validate_relationships(config)
    directory = canonical_path(directory)
    protected = (*_protected_paths(config), canonical_path(config.app.state_dir), canonical_path(config.app.reports_dir))
    if any(directory.is_relative_to(path) or path.is_relative_to(directory) for path in protected):
        raise StateError("Katalog snapshotu koliduje z konfiguracją, stanem, raportami lub ścieżką danych.")
    database = _database_path(config.app.database_file)
    try:
        with installation_lock(database), TemporaryDirectory(prefix=".snapshot-", dir=directory) as temporary:
            staged = Path(temporary)
            generation = prepare_snapshot(config, staged)
            published = directory / f"snapshot-{generation.generation_id}"
            if published.exists() or published.is_symlink():
                raise StateError("Generacja o tym identyfikatorze już istnieje.")
            staged.rename(published)
            try:
                _sync_directory(directory)
            except OSError:
                raise SnapshotPublishedError(
                    f"Generacja {published.name} jest widoczna, ale trwałość zapisu nie została potwierdzona. "
                    "Sprawdź ją poleceniem verify-snapshot przed ponowieniem."
                ) from None
            return published
    except (OSError, sqlite3.Error):
        raise StateError("Nie można zapisać generacji stanu.") from None


def _preserve_previous(database: Path, staging_dir: Path) -> Path | None:
    try:
        info = database.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise StateError("Przywracanie wymaga zwykłego pliku bazy bez dodatkowych dowiązań.")
    # The exclusive installation lock and absence of sidecars make this a copy
    # of a stopped database. Preserve its bytes even if the database is damaged.
    previous = database.parent / f"state-before-restore-{uuid4().hex}.sqlite3"
    staged = staging_dir / "previous.sqlite3"
    _copy_private(database, staged)
    _sync_file(staged)
    os.link(staged, previous)
    _sync_directory(database.parent)
    return previous


def restore_snapshot(config: Configuration, directory: Path) -> RestoreResult:
    """Restore a staged copy under an exclusive installation lock."""
    validate_relationships(config)
    require_utf8(str(directory), StateError)
    database = _database_path(config.app.database_file)
    state_dir = canonical_path(database.parent)
    if any(state_dir.is_relative_to(path) or path.is_relative_to(state_dir)
           for path in _protected_paths(config)):
        raise StateError("Katalog stanu koliduje z konfiguracją lub ścieżką danych.")
    if canonical_path(directory / _DATABASE) == canonical_path(database):
        raise StateError("Generacja musi znajdować się poza plikiem roboczej bazy.")
    try:
        _check_generation_files(directory)
        if not state_dir.is_dir():
            raise StateError("Przed przywróceniem przygotuj istniejący katalog APP_STATE_DIR.")
        with installation_lock(database, exclusive=True), TemporaryDirectory(
            prefix=".restore-", dir=database.parent,
        ) as temporary:
            _check_sidecars(database, allow_journal=False)
            staged = Path(temporary)
            for name in (_DATABASE, _CONFIG, _MANIFEST):
                _copy_private(directory / name, staged / name)
            generation = validate_snapshot(staged)
            candidate = staged / _DATABASE
            if generation.revision != _head_revision():
                migrate_database(candidate)
            with open_database(candidate, writable=True) as connection, transaction(connection):
                _validate_registered_configuration(connection, config)
                connection.execute(restore_state.delete())
                connection.execute(restore_state.insert().values(
                    id=1, generation_id=generation.generation_id, restored_at=_now(),
                ))
            _sync_file(candidate)
            previous = _preserve_previous(database, staged)
            os.replace(candidate, database)
            try:
                _sync_directory(database.parent)
            except OSError:
                raise DatabasePublishedError(
                    "Baza została przywrócona, ale trwałość zapisu nie została potwierdzona. Sprawdź stan instalacji."
                ) from None
            return RestoreResult(generation.generation_id, previous)
    except OSError:
        raise StateError("Nie można przywrócić generacji stanu.") from None
