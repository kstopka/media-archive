"""Database lifecycle, transaction safety and migration failure scenarios."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError, ResourceClosedError

from media_archive.cli import main
from media_archive.state import (
    StateError, create_database, migrate_database, open_database, schema_version, transaction,
)
from media_archive.state import migrations
from media_archive.state.migrations import APPLICATION_ID
from tests.config_fixtures import registry_values, write_config


class StateDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "private database #1? Żółte.sqlite3"

    def connect_raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    def test_explicit_creation_identity_version_permissions_and_cleanup(self) -> None:
        create_database(self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0002")
            self.assertEqual(connection.exec_driver_sql("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.exec_driver_sql("PRAGMA synchronous").fetchone()[0], 2)
        with self.assertRaises(ResourceClosedError):
            connection.exec_driver_sql("SELECT 1")

    def test_missing_database_is_not_created_by_open_or_migration(self) -> None:
        for writable in (False, True):
            with self.subTest(writable=writable), self.assertRaises(StateError):
                with open_database(self.path, writable=writable):
                    self.fail("Missing database opened")
        with self.assertRaises(StateError):
            migrate_database(self.path)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_creation_does_not_create_parent_directories(self) -> None:
        with self.assertRaises(StateError):
            create_database(self.root / "missing" / "state.sqlite3")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_sqlite_sidecar_names_are_reserved(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(StateError, "zarezerwowana"):
                create_database(self.path.with_name(self.path.name + suffix))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_sidecar_files_survive_creation_and_writable_access(self) -> None:
        for suffix in ("-journal", "-wal", "-shm"):
            with self.subTest(suffix=suffix):
                path = self.root / f"state{suffix}.sqlite3"
                other = self.root / "other.sqlite3"
                sidecar = path.with_name(path.name + suffix)
                create_database(path)
                create_database(other)
                other.rename(sidecar)
                original = path.read_bytes()
                collision = sidecar.read_bytes()
                with self.assertRaisesRegex(StateError, "koliduje"):
                    with open_database(path, writable=True):
                        self.fail("Opened a database with a colliding sidecar")
                with self.assertRaisesRegex(StateError, "koliduje"):
                    migrate_database(path)
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(sidecar.read_bytes(), collision)
                path.unlink()
                with self.assertRaisesRegex(StateError, "koliduje"):
                    create_database(path)
                self.assertFalse(path.exists())
                self.assertEqual(sidecar.read_bytes(), collision)

    def test_writer_checks_sidecars_again_before_beginning_work(self) -> None:
        create_database(self.path)
        journal = self.path.with_name(self.path.name + "-journal")
        with open_database(self.path, writable=True) as connection:
            journal.write_bytes(b"unrelated file")
            with self.assertRaisesRegex(StateError, "koliduje"):
                with transaction(connection):
                    self.fail("Transaction started with a colliding journal")
        self.assertEqual(journal.read_bytes(), b"unrelated file")

    def test_foreign_database_with_hot_journal_is_rejected_without_recovery(self) -> None:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE foreign_data (value TEXT)")
            connection.execute("INSERT INTO foreign_data VALUES ('preserved')")
            connection.commit()
        original = self.path.read_bytes()
        script = """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA cache_size = 8')
connection.execute('PRAGMA cache_spill = ON')
connection.execute('PRAGMA synchronous = FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE foreign_data SET value = 'uncommitted'")
connection.execute('CREATE TABLE unfinished (data BLOB)')
connection.execute('WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<128) '
                   'INSERT INTO unfinished SELECT zeroblob(4096) FROM n')
os._exit(23)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(self.path)],
            cwd=self.root, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        before = self.path.read_bytes()
        journal = self.path.with_name(self.path.name + "-journal")
        journal_before = journal.read_bytes()
        self.assertNotEqual(before, original)
        self.assertEqual(journal_before[:8], bytes.fromhex("d9d505f920a163d7"))
        with self.assertRaisesRegex(StateError, "nie jest bazą stanu"):
            with open_database(self.path, writable=True):
                self.fail("Foreign database opened for writing")
        with self.assertRaisesRegex(StateError, "nie jest bazą stanu"):
            migrate_database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(journal.read_bytes(), journal_before)

    def test_creation_never_overwrites_existing_files_or_symlinks(self) -> None:
        self.path.write_bytes(b"private existing data")
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            create_database(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        link = self.root / "existing-link"
        link.symlink_to(self.path)
        with self.assertRaises(StateError):
            create_database(link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_bytes(), before)
        dangling = self.root / "dangling-link"
        dangling.symlink_to(self.root / "absent")
        with self.assertRaises(StateError):
            create_database(dangling)
        self.assertTrue(dangling.is_symlink())
        self.assertFalse((self.root / "absent").exists())

    def test_concurrent_creators_publish_one_valid_database(self) -> None:
        def create() -> bool:
            try:
                create_database(self.path)
            except StateError:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: create(), range(2)))
        self.assertEqual(sorted(results), [False, True])
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0002")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_invalid_paths_and_nonregular_files_are_rejected(self) -> None:
        create_database(self.path)
        link = self.root / "link"
        link.symlink_to(self.path)
        for path in ("relative.sqlite3", self.root / ".." / "state.sqlite3", str(self.root) + "/nul\x00", self.root, link):
            with self.subTest(path=path), self.assertRaises(StateError):
                with open_database(path):
                    self.fail("Invalid path opened")

    def test_unrelated_empty_and_corrupt_databases_are_rejected_without_changes(self) -> None:
        for kind in ("unrelated", "empty", "corrupt", "truncated"):
            with self.subTest(kind=kind):
                path = self.root / f"{kind}.sqlite3"
                if kind == "unrelated":
                    with closing(sqlite3.connect(path)) as connection:
                        connection.execute("CREATE TABLE unrelated (id)")
                elif kind == "truncated":
                    create_database(path)
                    path.write_bytes(path.read_bytes()[:100])
                else:
                    path.write_bytes(b"" if kind == "empty" else b"not a database")
                before = path.read_bytes()
                with self.assertRaises(StateError):
                    with open_database(path):
                        self.fail("Invalid database opened")
                with self.assertRaises(StateError):
                    migrate_database(path)
                self.assertEqual(path.read_bytes(), before)

    def test_future_and_inconsistent_revisions_are_rejected(self) -> None:
        create_database(self.path)
        connection = self.connect_raw()
        for sql in (
            "UPDATE alembic_version SET version_num = 'future'",
            "UPDATE alembic_version SET version_num = X'30303031'",
            "INSERT INTO alembic_version VALUES ('other')",
            "DELETE FROM alembic_version",
        ):
            with self.subTest(sql=sql):
                connection.execute("DELETE FROM alembic_version")
                connection.execute("INSERT INTO alembic_version VALUES ('0001')")
                connection.execute(sql)
                before = self.path.read_bytes()
                with self.assertRaises(StateError):
                    with open_database(self.path):
                        self.fail("Invalid revision opened")
                with self.assertRaises(StateError):
                    migrate_database(self.path)
                self.assertEqual(self.path.read_bytes(), before)

    def test_readonly_open_changes_neither_bytes_nor_directory_and_rejects_writes(self) -> None:
        create_database(self.path)
        before = self.path.read_bytes()
        with self.assertRaises(StateError):
            with open_database(self.path) as connection:
                connection.exec_driver_sql("DELETE FROM alembic_version")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_transactions_commit_rollback_and_preserve_application_exceptions(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection:
            with transaction(connection):
                connection.exec_driver_sql("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
                connection.exec_driver_sql("INSERT INTO sample VALUES (1, 'kept')")
            for error in (RuntimeError("application failure"), ValueError("application failure"),
                          OSError("application failure"), KeyboardInterrupt()):
                with self.subTest(error=type(error)), self.assertRaises(type(error)):
                    with transaction(connection):
                        connection.exec_driver_sql("INSERT INTO sample VALUES (2, 'rolled back')")
                        raise error
                self.assertFalse(connection.in_transaction())
                self.assertEqual(connection.exec_driver_sql("SELECT * FROM sample").fetchall(), [(1, "kept")])
                connection.rollback()
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM sample").fetchall(), [(1, "kept")])

    def test_nested_transaction_does_not_commit_or_rollback_outer_work(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection:
            with transaction(connection):
                connection.exec_driver_sql("CREATE TABLE sample (id)")
                with self.assertRaises(StateError):
                    with transaction(connection):
                        self.fail("Nested transaction opened")
                self.assertTrue(connection.in_transaction())
                connection.exec_driver_sql("INSERT INTO sample VALUES (1)")
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM sample").fetchall(), [(1,)])

    def test_foreign_keys_uniqueness_and_deferred_commit_failure_rollback(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection:
            with transaction(connection):
                connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
                connection.exec_driver_sql("""CREATE TABLE child (
                    id INTEGER PRIMARY KEY,
                    parent_id INTEGER REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED
                )""")
            with self.assertRaises(StateError):
                with transaction(connection):
                    connection.exec_driver_sql("INSERT INTO child VALUES (1, 999)")
            self.assertFalse(connection.in_transaction())
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM child").fetchall(), [])
            connection.rollback()
            with transaction(connection):
                connection.exec_driver_sql("INSERT INTO parent VALUES (1)")
            with self.assertRaises(StateError):
                with transaction(connection):
                    connection.exec_driver_sql("INSERT INTO parent VALUES (2)")
                    connection.exec_driver_sql("INSERT INTO parent VALUES (1)")
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM parent").fetchall(), [(1,)])

    def test_uncommitted_work_is_rolled_back_on_connection_close(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection:
            connection.exec_driver_sql("CREATE TABLE abandoned (id)")
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE name='abandoned'").fetchall(), [])

    def test_driver_errors_hide_private_sql_and_paths(self) -> None:
        create_database(self.path)
        with self.assertRaises(StateError) as caught:
            with open_database(self.path, writable=True) as connection:
                with transaction(connection):
                    connection.exec_driver_sql('INSERT INTO "PRIVATE_TABLE_NAME" VALUES (1)')
        self.assertNotIn("PRIVATE_TABLE_NAME", str(caught.exception))
        self.assertNotIn(str(self.path), str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_failed_filesystem_publication_cleans_up(self) -> None:
        with patch("media_archive.state.database.os.link", side_effect=OSError("PRIVATE_PATH")):
            with self.assertRaises(StateError) as caught:
                create_database(self.path)
        self.assertNotIn("PRIVATE_PATH", str(caught.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_directory_sync_failure_keeps_published_valid_database(self) -> None:
        fsync = os.fsync

        def fail_directory_sync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("PRIVATE_DIRECTORY")
            fsync(descriptor)

        with patch("media_archive.state.database.os.fsync", side_effect=fail_directory_sync):
            with self.assertRaises(StateError) as caught:
                create_database(self.path)
        self.assertNotIn("PRIVATE_DIRECTORY", str(caught.exception))
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0002")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_help_and_configuration_check_never_connect_to_database(self) -> None:
        path = self.root / "config.env"
        write_config(path, registry_values(self.root))
        with (patch("sqlite3.connect", side_effect=AssertionError("database access")),
              patch("sqlite3.dbapi2.connect", side_effect=AssertionError("database access")),
              redirect_stdout(StringIO())):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--config", str(path)]), 0)


    def test_automatic_sqlite_rollback_blocks_further_statements_and_commit(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection:
            with transaction(connection):
                connection.exec_driver_sql("CREATE TABLE sample (id INTEGER UNIQUE ON CONFLICT ROLLBACK)")
                connection.exec_driver_sql("INSERT INTO sample VALUES (1)")
            with self.assertRaisesRegex(StateError, "przedwcześnie"):
                with transaction(connection):
                    connection.exec_driver_sql("INSERT INTO sample VALUES (2)")
                    with self.assertRaises(IntegrityError):
                        connection.exec_driver_sql("INSERT INTO sample VALUES (1)")
                    with self.assertRaisesRegex(StateError, "przedwcześnie"):
                        connection.exec_driver_sql("INSERT INTO sample VALUES (3)")
            self.assertFalse(connection.in_transaction())
            with transaction(connection):
                connection.exec_driver_sql("INSERT INTO sample VALUES (4)")
            self.assertEqual(connection.exec_driver_sql("SELECT id FROM sample ORDER BY id").all(), [(1,), (4,)])

    @unittest.skipUnless(hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"), "Python 3.12+ required")
    def test_autocommit_connections_cannot_enter_managed_transactions(self) -> None:
        create_database(self.path)
        with closing(sqlite3.connect(self.path, autocommit=True)) as raw:
            with self.assertRaises(StateError):
                with transaction(raw):
                    self.fail("Raw autocommit connection accepted")
            self.assertFalse(raw.in_transaction)
        with open_database(self.path, writable=True) as connection:
            driver = connection.connection.driver_connection
            driver.autocommit = True
            with self.assertRaisesRegex(StateError, "autocommit"):
                with transaction(connection):
                    self.fail("Driver autocommit accepted")
            self.assertFalse(driver.in_transaction)

    def test_writable_open_rejects_sqlite_readonly_fallback(self) -> None:
        create_database(self.path)
        connect = sqlite3.dbapi2.connect

        def readonly_connection(database: str, **kwargs: object) -> sqlite3.Connection:
            return connect(database.replace("mode=rw", "mode=ro"), **kwargs)

        before = self.path.read_bytes()
        with patch.object(sqlite3.dbapi2, "connect", side_effect=readonly_connection):
            with self.assertRaises(StateError):
                with open_database(self.path, writable=True):
                    self.fail("Read-only connection exposed as writable")
            with self.assertRaises(StateError):
                migrate_database(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_writable_open_rejects_file_without_effective_write_access(self) -> None:
        create_database(self.path)
        self.path.chmod(0o400)
        if os.access(self.path, os.W_OK):
            self.skipTest("Current user can write this file despite mode 0400")
        with open_database(self.path) as reader:
            self.assertEqual(schema_version(reader), "0002")
        with self.assertRaises(StateError):
            with open_database(self.path, writable=True):
                self.fail("Read-only file exposed as writable")

    def test_writable_open_probes_journal_creation_without_persisting_a_write(self) -> None:
        create_database(self.path)
        before = self.path.read_bytes()
        with open_database(self.path, writable=True) as connection:
            self.assertEqual(schema_version(connection), "0002")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])

        self.root.chmod(0o500)
        self.addCleanup(self.root.chmod, 0o700)
        if os.access(self.root, os.W_OK):
            self.skipTest("Current user can write this directory despite mode 0500")
        self.assertTrue(os.access(self.path, os.W_OK))
        with open_database(self.path) as reader:
            self.assertEqual(schema_version(reader), "0002")
        with self.assertRaises(StateError):
            with open_database(self.path, writable=True):
                self.fail("Connection exposed without permission to create a journal")
        self.assertEqual(self.path.read_bytes(), before)


class StateMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "state.sqlite3"
        self.revisions = self.root / "revisions"
        versions = self.revisions / "versions"
        versions.mkdir(parents=True)
        shutil.copy(migrations._SCRIPT_LOCATION / "env.py", self.revisions)
        shutil.copy(migrations._SCRIPT_LOCATION / "versions" / "r0001_foundation.py", versions)
        self.enterContext(patch.object(migrations, "_SCRIPT_LOCATION", self.revisions))

    def add_revision(self, version: str, parent: str, *statements: str) -> None:
        body = "\n".join(f"    op.get_bind().exec_driver_sql({statement!r})" for statement in statements)
        (self.revisions / "versions" / f"r{version}.py").write_text(
            f"from alembic import op\nrevision = {version!r}\ndown_revision = {parent!r}\n"
            f"def upgrade():\n{body}\n",
            encoding="utf-8",
        )

    def test_failed_creation_leaves_destination_absent(self) -> None:
        self.add_revision("0002", "0001", "CREATE TABLE unfinished (id)", "INVALID SQL")
        with self.assertRaises(StateError):
            create_database(self.path)
        self.assertEqual(list(self.root.iterdir()), [self.revisions])

    def test_old_schema_is_readable_and_only_explicit_migration_updates_it(self) -> None:
        create_database(self.path)
        before = self.path.read_bytes()
        self.add_revision("0002", "0001", "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)",
                          "INSERT INTO sample VALUES (1, 'preserved')")
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0001")
        with self.assertRaises(StateError):
            with open_database(self.path, writable=True):
                self.fail("Outdated schema opened for writing")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(migrate_database(self.path), "0002")
        self.assertEqual(migrate_database(self.path), "0002")
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0002")
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM sample").all(), [(1, "preserved")])
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM main.alembic_version").all(),
                             [("0002",)])

    def test_failed_batch_restores_schema_revision_and_existing_data(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection, transaction(connection):
            connection.exec_driver_sql("CREATE TABLE original (id PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO original VALUES (1)")
        self.add_revision("0002", "0001", "CREATE TABLE sample (id INTEGER PRIMARY KEY)")
        self.add_revision("0003", "0002", "DELETE FROM original", "CREATE TABLE unfinished (id)", "INVALID SQL")
        with self.assertRaises(StateError):
            migrate_database(self.path)
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0001")
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM original").all(), [(1,)])
            self.assertEqual(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name IN ('sample', 'unfinished')"
            ).all(), [])
        self.add_revision("0003", "0002", "CREATE TABLE finished (id)")
        self.assertEqual(migrate_database(self.path), "0003")

    def test_temporary_table_cannot_shadow_persistent_revision(self) -> None:
        create_database(self.path)
        self.add_revision("0002", "0001",
                          "CREATE TEMP TABLE alembic_version (version_num TEXT PRIMARY KEY)",
                          "INSERT INTO temp.alembic_version VALUES ('0001')")
        self.assertEqual(migrate_database(self.path), "0002")
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0002")
            self.assertEqual(connection.exec_driver_sql("SELECT version_num FROM main.alembic_version").all(),
                             [("0002",)])

    def test_batch_rebuild_preserves_rows_referencing_parent_with_cascade(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection, transaction(connection):
            connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY, label TEXT)")
            connection.exec_driver_sql("""CREATE TABLE child (
                id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE
            )""")
            connection.exec_driver_sql("INSERT INTO parent VALUES (1, 'preserved')")
            connection.exec_driver_sql("INSERT INTO child VALUES (2, 1)")
        (self.revisions / "versions" / "r0002.py").write_text('''
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"

def upgrade():
    with op.batch_alter_table("parent", recreate="always") as batch:
        batch.alter_column("label", existing_type=sa.Text(), nullable=False)
''', encoding="utf-8")
        self.assertEqual(migrate_database(self.path), "0002")
        with open_database(self.path, writable=True) as connection:
            self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(), 1)
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM parent").all(), [(1, "preserved")])
            self.assertEqual(connection.exec_driver_sql("SELECT * FROM child").all(), [(2, 1)])
            self.assertEqual(connection.exec_driver_sql(
                "SELECT [notnull] FROM pragma_table_info('parent') WHERE name = 'label'"
            ).scalar_one(), 1)

    def test_open_writer_rejects_a_revision_applied_by_another_connection(self) -> None:
        create_database(self.path)
        with (open_database(self.path, writable=True) as stale,
              open_database(self.path, writable=True) as reconnecting):
            with transaction(stale):
                stale.exec_driver_sql("CREATE TABLE sample (age_days INTEGER)")
                stale.exec_driver_sql("INSERT INTO sample VALUES (2)")
            self.add_revision(
                "0002", "0001", "ALTER TABLE sample RENAME COLUMN age_days TO age_seconds",
                "UPDATE sample SET age_seconds = age_seconds * 86400",
            )
            self.assertEqual(migrate_database(self.path), "0002")
            with self.assertRaisesRegex(StateError, "Rewizja bazy stanu zmieniła się"):
                with transaction(stale):
                    self.fail("Stale writer entered the transaction")
            self.assertTrue(stale.closed)
            with self.assertRaises(ResourceClosedError):
                stale.exec_driver_sql("INSERT INTO sample VALUES (3)")
            reconnecting.invalidate()
            with self.assertRaisesRegex(StateError, "Rewizja bazy stanu zmieniła się"):
                reconnecting.exec_driver_sql("INSERT INTO sample VALUES (3)")
            self.assertTrue(reconnecting.closed)
            with open_database(self.path, writable=True) as current, transaction(current):
                current.exec_driver_sql("INSERT INTO sample VALUES (4)")
        with open_database(self.path) as reader:
            self.assertEqual(reader.exec_driver_sql("SELECT age_seconds FROM sample ORDER BY rowid").all(),
                             [(172800,), (4,)])

    def test_migration_with_invalid_foreign_keys_is_rolled_back(self) -> None:
        create_database(self.path)
        self.add_revision(
            "0002", "0001", "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            "CREATE TABLE child (parent_id REFERENCES parent(id) DEFERRABLE INITIALLY DEFERRED)",
            "INSERT INTO child VALUES (123)",
        )
        with self.assertRaises(StateError):
            migrate_database(self.path)
        with open_database(self.path) as connection:
            self.assertEqual(schema_version(connection), "0001")
            self.assertEqual(connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE name='child'"
            ).all(), [])

    def test_migration_cannot_end_transaction_or_change_connection_settings(self) -> None:
        create_database(self.path)
        for statement in ("COMMIT", "ROLLBACK", "SAVEPOINT bypass", "PRAGMA foreign_keys = OFF",
                          "ATTACH DATABASE ':memory:' AS other", "VACUUM"):
            with self.subTest(statement=statement):
                self.add_revision("0002", "0001", "CREATE TABLE unfinished (id)", statement)
                with self.assertRaises(StateError):
                    migrate_database(self.path)
                with open_database(self.path) as connection:
                    self.assertEqual(schema_version(connection), "0001")
                    self.assertEqual(connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE name='unfinished'"
                    ).all(), [])

    def test_concurrent_migrations_on_same_and_different_databases(self) -> None:
        other = self.root / "other.sqlite3"
        for path in (self.path, other):
            create_database(path)
        self.add_revision("0002", "0001", "CREATE TABLE sample (id)")
        with ThreadPoolExecutor(max_workers=3) as executor:
            self.assertEqual(list(executor.map(migrate_database, (self.path, other, self.path))), ["0002"] * 3)
        for path in (self.path, other):
            with open_database(path) as connection:
                self.assertEqual(schema_version(connection), "0002")

    def test_process_exit_after_spill_requires_rollback_journal_recovery(self) -> None:
        create_database(self.path)
        with open_database(self.path, writable=True) as connection, transaction(connection):
            connection.exec_driver_sql("CREATE TABLE original (value TEXT)")
            connection.exec_driver_sql("INSERT INTO original VALUES ('preserved')")
        before = self.path.read_bytes()
        self.add_revision(
            "0002", "0001",
            "UPDATE original SET value = 'uncommitted'",
            "CREATE TABLE unfinished (data BLOB)",
            "WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<128) "
            "INSERT INTO unfinished SELECT zeroblob(4096) FROM n",
        )
        script = """
import os
from pathlib import Path
import sys
from sqlalchemy import event
from media_archive.state import database, migrations
migrations._SCRIPT_LOCATION = Path(sys.argv[2])
engine_factory = database._engine
def engine_with_small_cache(path, *, writable, migrating=False, initializing=False):
    engine = engine_factory(path, writable=writable, migrating=migrating, initializing=initializing)
    @event.listens_for(engine, 'connect')
    def configure(driver, record):
        driver.execute('PRAGMA cache_size = 8').close()
        driver.execute('PRAGMA cache_spill = ON').close()
    return engine
database._engine = engine_with_small_cache
upgrade = database._upgrade
def interrupted(connection):
    upgrade(connection)
    os._exit(23)
database._upgrade = interrupted
database.migrate_database(sys.argv[1])
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(self.path), str(self.revisions)],
            cwd=self.root, capture_output=True, timeout=15,
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotEqual(self.path.read_bytes(), before)
        journal = self.path.with_name(self.path.name + "-journal")
        self.assertGreater(journal.stat().st_size, 0)
        with self.assertRaises(StateError):
            with open_database(self.path):
                self.fail("Read-only access unexpectedly recovered the journal")
        self.assertTrue(journal.exists())
        (self.revisions / "versions" / "r0002.py").unlink()
        with open_database(self.path, writable=True) as connection:
            self.assertEqual(schema_version(connection), "0001")
            self.assertEqual(connection.exec_driver_sql("SELECT value FROM original").all(), [("preserved",)])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
