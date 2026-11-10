"""Explicit database lifecycle and short transactions, independent of the CLI."""

from collections.abc import Generator
from contextlib import closing, contextmanager
import os
from pathlib import Path
import sqlite3
import stat
import struct
import tempfile

from sqlalchemy import URL, create_engine, event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import ConnectionPoolEntry, NullPool

from .errors import StateError
from .migrations import APPLICATION_ID, _head_revision, _upgrade, schema_version


_JOURNAL_SUFFIX = "-journal"
_SIDECAR_SUFFIXES = (_JOURNAL_SUFFIX, "-wal", "-shm")
_JOURNAL_MAGIC = bytes.fromhex("d9d505f920a163d7")
_JOURNAL_HEADER = struct.Struct(">8s5I")


def _database_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute() or ".." in candidate.parts or "\x00" in str(candidate):
        raise StateError("Baza stanu wymaga bezwzględnej ścieżki bez przejść do rodzica.")
    if candidate.name.endswith(_SIDECAR_SUFFIXES):
        raise StateError("Nazwa bazy stanu jest zarezerwowana dla pliku pomocniczego SQLite.")
    return candidate


def _is_rollback_journal(path: Path, info: os.stat_result) -> bool:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return False
    with path.open("rb") as journal:
        header = journal.read(_JOURNAL_HEADER.size)
    if len(header) != _JOURNAL_HEADER.size:
        return False
    magic, page_count, _, database_pages, sector_size, page_size = _JOURNAL_HEADER.unpack(header)
    # A writer may have initialized the header without syncing the journal yet.
    if magic != _JOURNAL_MAGIC and (magic != bytes(8) or page_count != 0):
        return False
    valid_sizes = all(512 <= size <= 65536 and size & (size - 1) == 0
                      for size in (sector_size, page_size))
    return valid_sizes and database_pages > 0 and info.st_size >= sector_size


def _check_sidecars(path: Path, *, allow_journal: bool) -> None:
    for suffix in _SIDECAR_SUFFIXES:
        sidecar = path.with_name(path.name + suffix)
        try:
            info = sidecar.lstat()
            if allow_journal and suffix == _JOURNAL_SUFFIX and _is_rollback_journal(sidecar, info):
                continue
        except FileNotFoundError:
            # A concurrent writer may have committed and removed its journal.
            continue
        except OSError:
            raise StateError("Nie można sprawdzić plików pomocniczych bazy stanu.") from None
        raise StateError("Ścieżka bazy stanu koliduje z plikiem pomocniczym SQLite.")


def _check_application_id(path: Path) -> None:
    # Inspect only the stable marker, ignoring the journal without running recovery.
    uri = path.as_uri() + "?mode=ro&immutable=1"
    try:
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            with closing(connection.execute("PRAGMA main.application_id")) as result:
                if result.fetchone() != (APPLICATION_ID,):
                    raise StateError("Plik nie jest bazą stanu Media Archive.")
    except sqlite3.Error:
        raise StateError("Nie można odczytać identyfikatora bazy stanu.") from None


def _engine(path: Path, *, writable: bool, migrating: bool = False, initializing: bool = False) -> Engine:
    url = URL.create("sqlite+pysqlite", database=path.as_uri(), query={
        "uri": "true", "mode": "rw" if writable else "ro",
    })
    connect_args = {"isolation_level": None, "timeout": 5.0}
    if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"):
        # SQLAlchemy owns BEGIN. Keep DB-API commit/rollback effective for it.
        connect_args["autocommit"] = sqlite3.LEGACY_TRANSACTION_CONTROL
    engine = create_engine(url, connect_args=connect_args, poolclass=NullPool, hide_parameters=True)

    @event.listens_for(engine, "do_connect")
    def check_files(*args: object) -> None:
        _check_sidecars(path, allow_journal=not initializing)
        if writable and not initializing:
            _check_application_id(path)

    @event.listens_for(engine, "connect")
    def configure(driver: sqlite3.Connection, record: ConnectionPoolEntry) -> None:
        # Alembic batch rebuilds drop the original table. Disable cascades only
        # on the dedicated migration connection, before starting its transaction.
        foreign_keys = 0 if migrating else 1
        with closing(driver.cursor()) as cursor:
            cursor.execute(f"PRAGMA foreign_keys = {foreign_keys}")
            if cursor.execute("PRAGMA foreign_keys").fetchone() != (foreign_keys,):
                raise StateError("Nie można ustawić kontroli powiązań bazy stanu.")
            cursor.execute("PRAGMA synchronous = FULL")
            if not writable:
                cursor.execute("PRAGMA query_only = ON")
        record.info["state_writable"] = writable

    @event.listens_for(engine, "begin")
    def begin(connection: Connection) -> None:
        try:
            if writable:
                _check_sidecars(path, allow_journal=not initializing)
            connection.exec_driver_sql("BEGIN IMMEDIATE" if writable else "BEGIN")
            expected = connection.get_execution_options().get("state_revision")
            if expected is not None and schema_version(connection) != expected:
                raise StateError("Rewizja bazy stanu zmieniła się od otwarcia połączenia.")
        except BaseException:
            # A failed begin hook leaves no SQLAlchemy transaction to roll back.
            # Close the connection so it cannot run statements outside a transaction.
            connection.close()
            raise

    @event.listens_for(engine, "before_cursor_execute")
    def require_active_transaction(connection: Connection, *args: object) -> None:
        # SQLite can roll back on its own while SQLAlchemy still has a transaction object.
        if connection.in_transaction():
            _check_active_transaction(connection)

    return engine


@contextmanager
def _connect(
    path: Path, *, writable: bool, migrating: bool = False, initializing: bool = False,
) -> Generator[Connection, None, None]:
    try:
        regular_file = stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        raise StateError("Nie można otworzyć istniejącej bazy stanu.") from None
    if not regular_file:
        raise StateError("Baza stanu musi być zwykłym plikiem.")
    engine = _engine(path, writable=writable, migrating=migrating, initializing=initializing)
    try:
        with engine.connect() as connection:
            yield connection
    except (SQLAlchemyError, sqlite3.Error):
        raise StateError("Nie można wykonać operacji na bazie stanu.") from None
    finally:
        engine.dispose()


def _check_active_transaction(connection: Connection) -> None:
    if not connection.connection.driver_connection.in_transaction:
        raise StateError("SQLite zakończył transakcję przedwcześnie.")


@contextmanager
def transaction(connection: Connection) -> Generator[Connection, None, None]:
    """Commit one short write transaction, rolling back on any interruption.

    Transaction control belongs to this context. Nested transactions are rejected.
    """
    try:
        if not isinstance(connection, Connection) or not connection.info.get("state_writable"):
            raise StateError("Transakcja wymaga połączenia Media Archive otwartego do zapisu.")
        if connection.in_transaction():
            raise StateError("Zagnieżdżone transakcje bazy stanu są niedozwolone.")
        driver = connection.connection.driver_connection
        if getattr(driver, "autocommit", False) is True:
            raise StateError("Połączenie bazy stanu ma nieobsługiwany tryb autocommit.")
        try:
            with connection.begin():
                yield connection
                _check_active_transaction(connection)
        except SQLAlchemyError:
            # A failed SQLite COMMIT can leave the underlying transaction open
            # after SQLAlchemy has deactivated its transaction object.
            if not connection.closed and not connection.invalidated and driver.in_transaction:
                try:
                    driver.rollback()
                except sqlite3.Error:
                    connection.invalidate()
            raise
    except SQLAlchemyError:
        raise StateError("Nie można zatwierdzić transakcji bazy stanu.") from None


def _validate(connection: Connection) -> str:
    version = schema_version(connection)
    if connection.exec_driver_sql("PRAGMA main.quick_check").all() != [("ok",)]:
        raise StateError("Baza stanu jest uszkodzona.")
    return version


def _check_write_access(connection: Connection) -> None:
    # Change the validated revision briefly so SQLite must write its journal.
    # Roll back only the probe, including when called in a migration transaction.
    probe = connection.begin_nested()
    try:
        connection.exec_driver_sql("UPDATE main.alembic_version SET version_num = ''")
    finally:
        probe.rollback()


@contextmanager
def open_database(
    path: str | Path, *, writable: bool = False,
) -> Generator[Connection, None, None]:
    """Open existing state without creating it or migrating its schema."""
    with _connect(_database_path(path), writable=writable) as connection:
        check = connection.begin()
        try:
            version = _validate(connection)
            if writable:
                if version != _head_revision():
                    raise StateError("Baza stanu wymaga jawnej migracji przed zapisem.")
                _check_write_access(connection)
        finally:
            check.rollback()
        if writable:
            connection.execution_options(state_revision=version)
        yield connection


def create_database(path: str | Path) -> None:
    """Build a private database and publish it atomically without replacement.

    The parent directory must already exist and support same-filesystem hard links.
    """
    destination = _database_path(path)
    try:
        with tempfile.TemporaryDirectory(prefix=".media-archive-init-", dir=destination.parent) as directory:
            temporary = Path(directory) / "state.sqlite3"
            temporary.touch(mode=0o600, exist_ok=False)
            with _connect(temporary, writable=True, migrating=True, initializing=True) as connection:
                with transaction(connection):
                    connection.exec_driver_sql(f"PRAGMA main.application_id = {APPLICATION_ID}")
                    _upgrade(connection)
                    _validate(connection)
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            _check_sidecars(destination, allow_journal=False)
            os.link(temporary, destination)
            directory_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except OSError:
        raise StateError("Nie można ukończyć tworzenia nowej bazy stanu.") from None


def migrate_database(path: str | Path) -> str:
    """Explicitly apply all pending migrations atomically and return the revision."""
    with _connect(_database_path(path), writable=True, migrating=True) as connection:
        with transaction(connection):
            # Recheck after acquiring the write lock, including on concurrent calls.
            _validate(connection)
            _upgrade(connection)
            _check_write_access(connection)
            return _validate(connection)
