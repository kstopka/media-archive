# SQLite state foundation

Implemented task **1.3.1** provides explicit SQLite database creation, opening, schema validation, transactions and migrations. SQLAlchemy Core manages connections and transactions. Alembic manages schema revisions. Registration, media history, operation journals and operation locks are planned in tasks 1.3.2–1.3.4. Operational CLI commands belong to task 1.4.

## Entry points and ownership

`media_archive.state` exports the following API. Callers select the database path explicitly, normally from `config.app.database_file`. Loading configuration and importing the package perform no database access.

| API | Contract |
| --- | --- |
| `create_database(path) -> None` | Explicitly build and publish a new database with the current schema. The parent directory must exist. Any existing destination, including an empty file or dangling symlink, prevents publication. |
| `open_database(path, *, writable=False)` | Context manager yielding a SQLAlchemy Core `Connection` to an existing regular file. Default access is read-only. Validate identity, revision and SQLite `quick_check`. For writable access, check that SQLite can create its journal by writing and rolling back a probe before yielding. Close the connection on exit. |
| `schema_version(connection) -> str` | Return the applied Alembic revision after checking application identity. Accept a known older revision for inspection. |
| `migrate_database(path) -> str` | Explicitly apply all pending revisions in one write transaction and return the applied revision. Recheck the revision after obtaining the write lock. Calling it again at the current revision applies no schema changes. |
| `transaction(connection)` | Context manager for a short write transaction on a writable connection supplied by the state API. Commit on normal exit, roll back on exceptions or interruption, including a failed commit. Reject nested transactions and driver-level autocommit. |
| `StateError` | Application-level failure with a Polish diagnostic that excludes database paths, SQL text and values. |

Paths accept `str` or `Path` and must be absolute, contain no `..` components or NUL bytes, and identify regular files when opening. Leaf symlinks are rejected. The suffixes `-journal`, `-wal` and `-shm` are reserved for SQLite and cannot be used as database names. Use an ordinary UTF-8 filename with room for SQLite's journal suffix. Explicit handling of extreme filename lengths and non-UTF-8 paths is [deferred](roadmap.md#deferred-features). Parent directories are trusted application state directories. Filesystem identity, mount and media-path checks belong to the device subsystem.

Example for an explicitly selected temporary state directory whose parent already exists

```python
from pathlib import Path
from media_archive.state import (
    create_database, migrate_database, open_database, schema_version, transaction,
)

path = Path("/tmp/example-media-archive/state.sqlite3")
create_database(path)

with open_database(path) as connection:
    version = schema_version(connection)

# After an application upgrade, explicitly update the schema before writing.
version = migrate_database(path)
with open_database(path, writable=True) as connection:
    with transaction(connection):
        version = schema_version(connection)
```

Creation is separate from migration. Opening a missing database fails. A known older revision can be opened read-only, while writable opening requires the current revision. Each subsequent transaction on that writable connection rechecks the revision after acquiring the write lock. A changed or unrecognized revision closes the connection before application SQL runs. Opening never runs migrations. An unknown application ID, unknown revision or missing or inconsistent revision record fails validation.

## Schema and migrations

[migrations.py](../src/media_archive/state/migrations.py) runs the packaged [Alembic revisions](../src/media_archive/state/revisions/versions/) on the application's existing connection. Add schema changes as a new Alembic revision whose `down_revision` points to the current head. Preserve applied revision files and keep one head.

The current schema contains `main.alembic_version`, with one row for the applied revision `0001`. Both Alembic and the state API read the revision from `main`, so a temporary table with the same name cannot change which revision gets recorded or validated. SQLite header `application_id` is `0x4D415243` (`MARC`).

The Alembic environment runs in a caller-owned transaction with transactional DDL. Use Alembic operations or SQL on `op.get_bind()`. The SQLite authorizer blocks transaction-control statements, database attachment and changes to connection settings during revisions. This keeps schema changes and Alembic's revision update in the same transaction. A failed revision rolls back the entire pending batch. [Alembic's shared connection recipe](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands)

The dedicated migration connection disables foreign-key enforcement before beginning the transaction. Alembic batch rebuilds can then drop and replace a referenced table without deleting dependent records through `ON DELETE CASCADE`. Express intended changes to related rows explicitly in a revision. Before committing, `PRAGMA main.foreign_key_check` validates the result and any violation rolls back the entire batch. Ordinary connections continue to enforce foreign keys. [Alembic's guidance on referencing foreign keys](https://alembic.sqlalchemy.org/en/latest/batch.html#dealing-with-referencing-foreign-keys)

## Connections and transactions

[database.py](../src/media_archive/state/database.py) uses SQLAlchemy Core with the standard-library SQLite driver, URI `mode=ro` or `mode=rw`, and a five-second SQLite lock timeout. SQLAlchemy explicitly begins transactions with `BEGIN` for readers and `BEGIN IMMEDIATE` for writers. The driver is configured so commit and rollback take effect with explicit `BEGIN`, including on Python 3.12+. Each connection sets `synchronous=FULL`. Ordinary connections enable and check `foreign_keys`, and read-only connections also enable `query_only`. [SQLAlchemy's SQLite transaction configuration](https://docs.sqlalchemy.org/en/21/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver)

Connections are for trusted application code in one thread. Use Core statements or parameterized `exec_driver_sql()` and use `transaction()` for writes. SQLAlchemy can begin a transaction on the first statement, so finish an existing read before opening a managed write transaction on the same connection. Leave transaction control and connection settings to the state layer while a managed transaction is active. If SQLite rolls back automatically, further statements in the aborted transaction are rejected. A failed commit also rolls back any transaction still open in the driver. Connection closing rolls back unfinished work.

The foundation uses SQLite's default rollback journal. Before opening a writable SQLite connection, the state API reads only the application ID through a read-only SQLite connection with recovery disabled. A foreign database is rejected without recovering or deleting its journal. Normal opening then validates the revision and integrity. After a writer terminates, an explicit writable opening or migration can recover a recognized rollback journal of a Media Archive database. A read-only opening may fail while recovery is required. [SQLite connection options](https://www.sqlite.org/uri.html#recognized_query_parameters) · [SQLite journal format](https://www.sqlite.org/fileformat.html#the_rollback_journal)

`quick_check` reads database pages and can cost more as state grows. It checks SQLite structural consistency. Historical content verification and device identity require the later state and device subsystems.

## Creation, side effects and errors

Creation builds the schema inside a private temporary directory beside the destination, with database file permissions `0600`. After validation and closing SQLite, it syncs the file, publishes it using a same-filesystem hard link without replacement, and syncs the parent directory. The filesystem must support hard links and directory syncing. Concurrent creators can publish only one database.

Creation refuses to publish beside an existing SQLite sidecar. Opening checks sidecar paths before connecting and checks them again before beginning a writable transaction. Existing `-wal` and `-shm` paths, links, and files without a recognizable rollback-journal header cause a safe error and remain in place. An ambiguous or incomplete journal requires operator inspection. Keep the database and its sidecar names under application control in the trusted state directory.

Ordinary creation failures remove temporary state. If directory syncing fails after publication, the API raises `StateError` and preserves the published, valid database. The caller should inspect that database before retrying. Process termination during preparation may leave a private temporary directory for later operator inspection.

Writable operations may create SQLite rollback journals. Writable opening briefly changes the revision row inside a rolled-back probe. This checks that SQLite can write the journal, including when the database file is writable but its parent directory is read-only. The probe leaves the stored revision unchanged. Default read-only opening preserves database contents and performs no schema updates. Externally changing connection or journal settings is outside the supported lifecycle.

Filesystem and SQLite failures become `StateError` without forwarding their underlying diagnostic text. Application exceptions from a transaction body propagate after rollback. Explicitly supplied SQL and raw connection use remain the responsibility of trusted callers. Database creation and migration have no CLI entry points yet and never run as a side effect of help, `load_config()` or `--check-config`.

## Verification and limits

[test_state_database.py](../tests/test_state_database.py) uses temporary directories and synthetic data. It covers creation and concurrent creators, sidecar collisions, read-only fallback, journal permissions, invalid databases and revisions, writers opened before a migration, preservation of dependent rows during a batch rebuild, temporary-table shadowing, constraint failures, automatic rollback, unsupported autocommit, failed commits, migration rollback/retry and concurrent migration calls. The interruption tests verify that Media Archive recovers its own database and preserves the exact bytes of a foreign database and its hot journal. CLI checks verify that help and configuration validation never connect to SQLite.

This foundation provides schema migration transactions. Long-running operation locks, file transfers, physical-device validation, snapshots and restore are delivered in their respective later tasks. Tests on temporary state establish the exercised software behavior. They do not establish power-loss guarantees for a particular production filesystem or hardware.

## Planned first setup and upgrades

Task 1.4 will give each installation one working database named `state.sqlite3`. The operator will configure the state directory, and the application will choose the filename. The current configuration still takes the full path in `APP_DATABASE_FILE`.

1. During first setup, create the database and record the initial state after the operator confirms the configuration. Protect any existing file at the destination. Configuration checks remain read-only.
2. During normal operation, open the same database and append or update records as needed. When an application update requires a schema change, run Alembic explicitly against the same file and preserve its data.
3. If an existing installation's database is missing, report the error and stop the operation. New database creation remains an explicit setup action so that a missing file cannot silently start a new history.
