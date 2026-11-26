"""Exercise command setup, planning and report retention on temporary state."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.cli import main
from media_archive.config import load_config
from media_archive.state import StateError, open_database, schema_version, transaction
from media_archive.state import migrations
from media_archive.state.history import create_run, record_content, record_occurrence
from media_archive.state.journal import advance_attempt, plan_operation, start_attempt
from media_archive.state.locking import collection_resource, lock_resources
from media_archive.state.registry import validate_registered_configuration
from tests.config_fixtures import registry_values, write_config


class OperationalCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.settings = self.root / ".env"
        self.state = self.root / "state"
        self.database = self.state / "state.sqlite3"
        self.reports = self.state / "reports"
        values = registry_values(self.root)
        values.update(APP_STATE_DIR=str(self.state), APP_REPORTS_DIR=str(self.reports), APP_REPORT_KEEP_RUNS="2")
        self.values = values
        write_config(self.settings, values)

    def run_command(self, *arguments: str, expected: int = 0) -> str:
        output, diagnostics = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(diagnostics):
            code = main(["--config", str(self.settings), *arguments])
        self.assertEqual(code, expected, diagnostics.getvalue())
        return output.getvalue() + diagnostics.getvalue()

    def test_setup_plan_is_read_only_and_setup_publishes_registered_database(self) -> None:
        with patch("sqlite3.connect", side_effect=AssertionError("unexpected SQLite access")):
            output = self.run_command("setup", "--dry-run")
        self.assertIn(str(self.database), output)
        self.assertFalse(self.state.exists())
        self.run_command("setup", "--yes")
        validate_registered_configuration(load_config(self.settings))
        before = self.database.read_bytes()
        self.run_command("setup", "--dry-run")
        self.run_command("setup", "--yes", expected=1)
        self.assertEqual(self.database.read_bytes(), before)

    def test_setup_registration_failure_leaves_no_database_and_can_be_retried(self) -> None:
        with patch("media_archive.state.registry._check_configuration", side_effect=StateError("Test")):
            self.run_command("setup", "--yes", expected=1)
        self.assertFalse(self.database.exists())
        self.run_command("setup", "--yes")
        validate_registered_configuration(load_config(self.settings))

    def test_setup_rejects_state_or_reports_inside_resolved_collection(self) -> None:
        collection = Path(self.values["COLLECTION_PHOTOS_LOCAL_ROOT"])
        collection.parent.mkdir(parents=True)
        actual = self.root / "actual-photos"
        actual.mkdir()
        collection.symlink_to(actual, target_is_directory=True)
        for key in ("APP_STATE_DIR", "APP_REPORTS_DIR"):
            with self.subTest(key=key):
                write_config(self.settings, self.values | {key: str(actual / "output")})
                self.run_command("setup", "--yes", expected=1)
                self.assertEqual(list(actual.iterdir()), [])
                self.assertFalse(self.state.exists())

    def test_setup_rejects_linked_report_directory_before_creating_database(self) -> None:
        actual = self.root / "actual-reports"
        actual.mkdir()
        self.state.mkdir()
        self.reports.symlink_to(actual, target_is_directory=True)
        self.assertIn("dowiązaniem symbolicznym", self.run_command("setup", "--yes", expected=1))
        self.assertFalse(self.database.exists())
        self.assertEqual(list(actual.iterdir()), [])

    def test_setup_rejects_regular_file_as_reports_directory_before_creating_database(self) -> None:
        self.state.mkdir()
        self.reports.write_text("existing file", encoding="utf-8")
        self.assertIn("musi wskazywać na katalog", self.run_command("setup", "--yes", expected=1))
        self.assertFalse(self.database.exists())
        self.assertEqual(self.reports.read_text(encoding="utf-8"), "existing file")

    def test_reports_rotate_completed_commands_and_keep_plans_and_unrelated_files(self) -> None:
        self.run_command("setup", "--dry-run", "--report")
        plans = list(self.reports.glob("plan-*"))
        self.assertEqual(len(plans), 1)
        unrelated = self.reports / "notes.txt"
        unrelated.write_text("notes", encoding="utf-8")
        lookalike = self.reports / ("run-20000101T000000000000Z-" + "a" * 32)
        lookalike.mkdir()
        (lookalike / "report.txt").write_text("foreign data", encoding="utf-8")
        (lookalike / "report.json").write_text("{}", encoding="utf-8")
        self.run_command("setup", "--yes")
        mismatched = self.reports / ("run-20000101T000000000000Z-" + "b" * 32)
        mismatched.mkdir()
        setup_report = next(bundle for bundle in self.reports.glob("run-*") if bundle not in (lookalike, mismatched))
        shutil.copyfile(setup_report / "report.txt", mismatched / "report.txt")
        shutil.copyfile(setup_report / "report.json", mismatched / "report.json")
        before = self.database.read_bytes()
        self.run_command("migrate", "--dry-run")
        self.assertEqual(self.database.read_bytes(), before)
        self.run_command("migrate")
        self.run_command("migrate")
        runs = set(self.reports.glob("run-*")) - {lookalike, mismatched}
        self.assertEqual(len(runs), 2)
        self.assertTrue(unrelated.is_file())
        self.assertTrue(plans[0].is_dir())
        self.assertEqual((lookalike / "report.json").read_text(encoding="utf-8"), "{}")
        self.assertTrue((mismatched / "report.json").is_file())
        for bundle in runs:
            payload = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["polecenie"], "migrate")
            self.assertEqual(payload["kod"], 0)
            self.assertTrue((bundle / "report.txt").is_file())

    def test_missing_database_is_not_recreated_by_migrate_or_pending(self) -> None:
        self.run_command("migrate", "--dry-run", expected=1)
        self.run_command("pending", expected=1)
        self.assertFalse(self.state.exists())

    def test_migration_plan_preserves_old_database_until_explicit_upgrade(self) -> None:
        revisions = self.root / "old-revisions"
        shutil.copytree(migrations._SCRIPT_LOCATION, revisions, ignore=shutil.ignore_patterns("r0004*", "r0005*", "r0006*", "r0007*", "r0008*", "r0009*", "r0010*", "r0011*", "r0012*", "r0013*", "r0014*", "r0015*", "__pycache__"))
        with patch.object(migrations, "_SCRIPT_LOCATION", revisions):
            self.run_command("setup", "--yes")
        before = self.database.read_bytes()
        plan = self.run_command("migrate", "--dry-run")
        self.assertIn("Rewizja bieżąca · 0003", plan)
        self.assertIn("Rewizja docelowa · 0015", plan)
        self.assertEqual(self.database.read_bytes(), before)
        invalid_reports = self.root / "report-file"
        invalid_reports.touch()
        write_config(self.settings, self.values | {"APP_REPORTS_DIR": str(invalid_reports)})
        self.assertIn("musi wskazywać na katalog", self.run_command("migrate", expected=1))
        self.assertEqual(self.database.read_bytes(), before)
        write_config(self.settings, self.values)
        linked = self.root / "linked.sqlite3"
        linked.hardlink_to(self.database)
        self.assertIn("dowiązań twardych", self.run_command("migrate", expected=1))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertEqual(linked.read_bytes(), before)
        linked.unlink()
        self.run_command("migrate")
        with open_database(self.database) as connection:
            self.assertEqual(schema_version(connection), "0015")
        validate_registered_configuration(load_config(self.settings))

    def test_report_failure_returns_partial_and_preserves_setup(self) -> None:
        with patch("media_archive.cli.write_report", side_effect=StateError("Test")):
            self.run_command("setup", "--yes", expected=3)
        validate_registered_configuration(load_config(self.settings))

    def test_setup_reports_partial_result_after_directory_sync_failure(self) -> None:
        fsync = os.fsync

        def fail_directory_sync(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("PRIVATE_DIRECTORY")
            fsync(descriptor)

        with patch("media_archive.state.database.os.fsync", side_effect=fail_directory_sync):
            output = self.run_command("setup", "--yes", expected=3)
        self.assertIn("trwałość zapisu nie została potwierdzona", output)
        self.assertIn("setup --dry-run", output)
        self.assertNotIn("PRIVATE_DIRECTORY", output)
        bundle, = self.reports.glob("run-*")
        report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["kod"], 3)
        validate_registered_configuration(load_config(self.settings))
        before = self.database.read_bytes()
        self.run_command("setup", "--dry-run")
        self.run_command("setup", "--yes", expected=1)
        self.assertEqual(self.database.read_bytes(), before)

    def test_pending_and_resume_plan_read_journal_without_writes(self) -> None:
        self.run_command("setup", "--yes")
        config = load_config(self.settings)
        resource = collection_resource(config, "photos")
        with lock_resources(self.database, [resource]) as locks:
            with open_database(self.database, writable=True) as connection, transaction(connection):
                content = record_content(connection, "a" * 64, 4)
                target = record_occurrence(connection, "staging", "/staging/example", content_id=content)
                operation = plan_operation(
                    connection, locks, config, operation_key="example", intent="Example",
                    target_occurrence_id=target, required_resources=[resource],
                )
        before = self.database.read_bytes()
        self.assertIn(f"Operacja {operation}", self.run_command("pending"))
        self.assertIn("rozpocznij próbę", self.run_command("resume", str(operation), "--dry-run"))
        self.run_command("resume", str(operation), expected=1)
        self.assertEqual(self.database.read_bytes(), before)

        with lock_resources(self.database, [resource]) as locks:
            with open_database(self.database, writable=True) as connection, transaction(connection):
                attempt = start_attempt(connection, locks, operation, create_run(connection))
                advance_attempt(connection, locks, attempt, "transferring")
            before = self.database.read_bytes()
            preview = self.run_command("resume", str(operation), "--dry-run")
            self.assertIn('Po uzyskaniu blokad jawnie przejmij próbę w trybie recovery="restart"', preview)
            self.assertEqual(self.database.read_bytes(), before)

    def test_interrupt_has_distinct_status(self) -> None:
        with patch("media_archive.cli.operations.setup", side_effect=KeyboardInterrupt):
            self.run_command("setup", "--dry-run", expected=130)
        self.assertFalse(self.state.exists())
