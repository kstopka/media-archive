"""Snapshot, validation and recovery against synthetic installations."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import ConfigError, load_config
from media_archive.state import StateError, migrations, open_database, schema_version, transaction
from media_archive.state.history import create_run, record_content, record_occurrence
from media_archive.state.journal import plan_operation, start_attempt
from media_archive.state.locking import OperationBusy, collection_resource, lock_resources
from media_archive.state.reconciliation import load_reconciliation
from media_archive.state.registry import initialize_database
from media_archive.state.snapshots import create_snapshot, restore_snapshot, validate_snapshot
from tests.config_fixtures import registry_values, write_config


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / "settings.env"
        self.state = self.root / "state"
        self.state.mkdir()
        self.output = self.root / "snapshots"
        self.output.mkdir()
        self.values = registry_values(self.root) | {"APP_STATE_DIR": str(self.state)}
        write_config(self.settings, self.values)
        self.config = load_config(self.settings)
        self.database = self.config.app.database_file
        initialize_database(self.config)

    def test_snapshot_copies_committed_state_while_writer_is_active(self) -> None:
        with open_database(self.database, writable=True) as connection, transaction(connection):
            committed = create_run(connection)
        with open_database(self.database, writable=True) as connection, transaction(connection):
            uncommitted = create_run(connection)
            generation = create_snapshot(self.config, self.output)
        self.assertEqual(validate_snapshot(generation).revision, "0006")
        self.assertEqual((generation / "settings.env").read_bytes(), self.settings.read_bytes())
        with open_database(generation / "state.sqlite3") as connection:
            runs = connection.exec_driver_sql("SELECT id FROM history_runs").scalars().all()
        self.assertIn(committed, runs)
        self.assertNotIn(uncommitted, runs)

    def test_corrupt_or_incompatible_snapshot_leaves_working_database_intact(self) -> None:
        generation = create_snapshot(self.config, self.output)
        original = self.database.read_bytes()
        write_config(self.settings, self.values | {"PHONE_MAIN_ALIAS": "replacement"})
        with self.assertRaises(ConfigError):
            restore_snapshot(load_config(self.settings), generation)
        self.assertEqual(self.database.read_bytes(), original)
        with (generation / "state.sqlite3").open("ab") as stream:
            stream.write(b"damaged")
        with self.assertRaisesRegex(StateError, "Sumy kontrolne"):
            restore_snapshot(self.config, generation)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertFalse(list(self.state.glob("state-before-restore-*")))

    def test_snapshot_reports_partial_result_if_output_directory_sync_fails(self) -> None:
        from media_archive.state.database import _sync_directory

        def fail_output_sync(path: Path) -> None:
            if path == self.output:
                raise OSError("simulated directory sync failure")
            _sync_directory(path)

        diagnostics = StringIO()
        with patch("media_archive.state.snapshots._sync_directory", side_effect=fail_output_sync):
            with redirect_stderr(diagnostics):
                self.assertEqual(main(["--config", str(self.settings), "snapshot", str(self.output)]), 3)
        generation, = self.output.iterdir()
        self.assertIn(generation.name, diagnostics.getvalue())
        self.assertIn("verify-snapshot", diagnostics.getvalue())
        self.assertEqual(validate_snapshot(generation).generation_id, generation.name.removeprefix("snapshot-"))

    def test_failed_copy_of_previous_database_leaves_no_published_recovery_copy(self) -> None:
        generation = create_snapshot(self.config, self.output)
        original = self.database.read_bytes()
        copyfile = shutil.copyfile

        def fail_previous_copy(source: Path, destination: Path) -> Path:
            if source == self.database:
                destination.write_bytes(b"partial")
                raise OSError("simulated short copy")
            return copyfile(source, destination)

        with patch("media_archive.state.snapshots.shutil.copyfile", side_effect=fail_previous_copy):
            with self.assertRaises(StateError):
                restore_snapshot(self.config, generation)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(list(self.state.iterdir()), [self.database])

    def test_restore_preserves_previous_state_and_blocks_media_attempts(self) -> None:
        resource = collection_resource(self.config, "photos")
        with lock_resources(self.database, [resource]) as locks:
            with open_database(self.database, writable=True) as connection, transaction(connection):
                run = create_run(connection)
                content = record_content(connection, "a" * 64, 4)
                occurrence = record_occurrence(connection, "staging", "/stage/example", content_id=content)
                operation = plan_operation(
                    connection, locks, self.config, operation_key="example", intent="Example",
                    target_occurrence_id=occurrence, required_resources=[resource],
                )
                start_attempt(connection, locks, operation, run)
        generation = create_snapshot(self.config, self.output)
        with open_database(self.database, writable=True) as connection, transaction(connection):
            later_run = create_run(connection)
        original = self.database.read_bytes()
        write_config(self.settings, self.values | {"PHONE_MAIN_LABEL": "Recovered"})
        selected = self.settings.read_bytes()
        result = restore_snapshot(load_config(self.settings), generation)
        self.assertEqual(result.previous_database.read_bytes(), original)
        self.assertEqual(self.settings.read_bytes(), selected)
        with lock_resources(self.database, [resource]) as locks:
            with open_database(self.database, writable=True) as connection, transaction(connection):
                self.assertEqual(load_reconciliation(connection).generation_id, result.generation_id)
                runs = connection.exec_driver_sql("SELECT id FROM history_runs").scalars().all()
                self.assertIn(run, runs)
                self.assertNotIn(later_run, runs)
                self.assertEqual(connection.exec_driver_sql("SELECT state FROM journal_attempts").scalar_one(), "planned")
                with self.assertRaisesRegex(StateError, "wymaga uzgodnienia"):
                    start_attempt(connection, locks, operation, create_run(connection), recovery="restart")
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["--config", str(self.settings), "resume", str(operation), "--dry-run"]), 0)
        self.assertIn("Przed wznowieniem uzgodnij", output.getvalue())
        self.assertNotIn('recovery="restart"', output.getvalue())

    def test_restore_upgrades_a_staged_copy_of_an_earlier_revision(self) -> None:
        revisions = self.root / "older-revisions"
        shutil.copytree(migrations._SCRIPT_LOCATION, revisions, ignore=shutil.ignore_patterns(
            "r0005_restore.py", "r0006_volume_hardware.py", "__pycache__",
        ))
        self.database.unlink()
        with patch.object(migrations, "_SCRIPT_LOCATION", revisions):
            initialize_database(self.config)
            generation = create_snapshot(self.config, self.output)
        before = {path.name: path.read_bytes() for path in generation.iterdir()}
        self.assertEqual(validate_snapshot(generation).revision, "0004")
        restore_snapshot(self.config, generation)
        self.assertEqual({path.name: path.read_bytes() for path in generation.iterdir()}, before)
        with open_database(self.database) as connection:
            self.assertEqual(schema_version(connection), "0006")
            self.assertIsNotNone(load_reconciliation(connection))

    def test_restore_is_excluded_while_connection_or_operation_is_open(self) -> None:
        generation = create_snapshot(self.config, self.output)
        script = """
import sys
from pathlib import Path
from media_archive.config import load_config
from media_archive.state.locking import OperationBusy
from media_archive.state.snapshots import restore_snapshot
try:
    restore_snapshot(load_config(sys.argv[1]), Path(sys.argv[2]))
except OperationBusy:
    sys.exit(23)
"""
        for context in (open_database(self.database), lock_resources(
            self.database, [collection_resource(self.config, "photos")],
        )):
            with context:
                result = subprocess.run(
                    [sys.executable, "-I", "-c", script, str(self.settings), str(generation)],
                    capture_output=True, text=True, encoding="utf-8", timeout=10,
                )
                self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIsNotNone(restore_snapshot(self.config, generation).previous_database)

    def test_cli_can_restore_missing_database_using_selected_config(self) -> None:
        args = ["--config", str(self.settings)]
        with redirect_stdout(StringIO()):
            self.assertEqual(main([*args, "snapshot", str(self.output)]), 0)
            generation, = self.output.iterdir()
            self.assertEqual(main(["verify-snapshot", str(generation)]), 0)
            self.database.unlink()
            self.state.rmdir()
            diagnostics = StringIO()
            with redirect_stderr(diagnostics):
                self.assertEqual(main([*args, "restore", str(generation), "--yes"]), 1)
            self.assertIn("APP_STATE_DIR", diagnostics.getvalue())
            self.assertFalse(self.state.exists())
            self.state.mkdir()
            self.assertEqual(main([*args, "restore", str(generation), "--yes"]), 0)
        with open_database(self.database) as connection:
            self.assertIsNotNone(load_reconciliation(connection))
