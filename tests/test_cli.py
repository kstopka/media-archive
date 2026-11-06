"""Exercise the actual module entry point in an isolated working directory."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.config_fixtures import registry_values, write_config


class CliTests(unittest.TestCase):
    def run_cli(
        self, *arguments: str, valid_config: bool = False, config_name: str = ".env",
        overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / config_name
            # Even with configuration present, help must not create state.
            config_text = (
                f"APP_DATABASE_FILE='{root / 'state' / 'state.sqlite3'}'\n"
                f"APP_REPORTS_DIR='{root / 'reports'}'\n"
                f"LOCAL_STAGING_ROOT='{root / 'staging'}'\n"
            )
            config.write_text(config_text, encoding="utf-8")
            if valid_config:
                values = registry_values(root)
                values.update(overrides or {})
                write_config(config, values)
                config_text = config.read_text(encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                PYTHONIOENCODING="utf-8",
                HOME=directory,
                XDG_CONFIG_HOME=str(root / "config"),
                XDG_STATE_HOME=str(root / "state"),
                XDG_CACHE_HOME=str(root / "cache"),
            )
            result = subprocess.run(
                # Resolve the installed package, not the checkout or PYTHONPATH.
                [sys.executable, "-I", "-B", "-m", "media_archive", *arguments],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=False,
            )
            self.assertEqual(list(root.iterdir()), [config])
            self.assertEqual(config.read_text(encoding="utf-8"), config_text)
            return result

    def test_help(self) -> None:
        for option in ("--help", "-h"):
            with self.subTest(option=option):
                result = self.run_cli(option)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("media-archive", result.stdout)
                self.assertIn("--help", result.stdout)
                self.assertIn(
                    "nie są jeszcze zaimplementowane", " ".join(result.stdout.split())
                )
                self.assertEqual(result.stderr, "")

    def test_no_arguments_shows_help(self) -> None:
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--help", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_unimplemented_command_is_rejected(self) -> None:
        result = self.run_cli("import")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("import", result.stderr)

    def test_check_default_config(self) -> None:
        result = self.run_cli("--check-config", valid_config=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Konfiguracja poprawna", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("test-phone", result.stdout)
        self.assertNotIn("/tmp/", result.stdout)

    def test_check_collection_registry(self) -> None:
        result = self.run_cli('--check-config', valid_config=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        result = self.run_cli('--check-config', valid_config=True,
                              overrides={'COLLECTION_PHOTOS_POLICY': 'PRIVATE_UNKNOWN_POLICY'})
        self.assertEqual(result.returncode, 2)
        self.assertNotIn('PRIVATE_UNKNOWN_POLICY', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_explicit_config_path_with_spaces(self) -> None:
        result = self.run_cli(
            "--config", "config with spaces.env", valid_config=True,
            config_name="config with spaces.env",
            overrides={"APP_CONFIG_FILE": "missing.env"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Konfiguracja poprawna", result.stdout)

    def test_invalid_config_does_not_leak_values(self) -> None:
        result = self.run_cli(
            "--check-config", valid_config=True,
            overrides={"PHONE_MAIN_SERIAL": "PRIVATE DEVICE ID"},
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("PHONE_MAIN_SERIAL", result.stderr)
        self.assertNotIn("PRIVATE DEVICE ID", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_missing_selected_file_is_reported(self) -> None:
        result = self.run_cli("--config", "PRIVATE_MISSING_FILE")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("PRIVATE_MISSING_FILE", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_help_does_not_read_selected_file(self) -> None:
        result = self.run_cli("--config", "missing.env", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--check-config", result.stdout)


if __name__ == "__main__":
    unittest.main()
