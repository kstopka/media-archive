"""Validate parsing, policy constraints, privacy and read-only configuration."""

from pydantic import ValidationError
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_archive.config import AppSettings, ConfigError, load_config, parse_env
from tests.config_fixtures import registry_values, write_config


class EnvParserTests(unittest.TestCase):
    def test_comments_quotes_empty_values_and_literal_content(self) -> None:
        self.assertEqual(
            parse_env(
                "# comment\n\nAPP_CONFIG_FILE = '.env' # comment\n"
                'PHONE_MAIN_MODEL="Camera #1 with spaces"\n'
                "PHONE_MAIN_BUILD=abc#123 # comment\n"
                "PHONE_SECONDARY_SERIAL= # disabled\n"
                "PHONE_SECONDARY_TRANSPORT=''\n"
                "APP_DATABASE_FILE='/literal/$HOME/$(touch marker)/file\\name'\n"
            ),
            {
                "APP_CONFIG_FILE": ".env",
                "PHONE_MAIN_MODEL": "Camera #1 with spaces",
                "PHONE_MAIN_BUILD": "abc#123",
                "PHONE_SECONDARY_SERIAL": "",
                "PHONE_SECONDARY_TRANSPORT": "",
                "APP_DATABASE_FILE": "/literal/$HOME/$(touch marker)/file\\name",
            },
        )

    def test_dotenv_syntax_and_no_variable_interpolation(self) -> None:
        self.assertEqual(
            parse_env(
                "export PHONE_MAIN_MODEL=Camera\n"
                "PHONE_MAIN_BUILD=build-1\n"
                "APP_CONFIG_FILE='${HOME}/.env'\n"
            ),
            {
                "PHONE_MAIN_MODEL": "Camera",
                "PHONE_MAIN_BUILD": "build-1",
                "APP_CONFIG_FILE": "${HOME}/.env",
            },
        )

    def test_invalid_syntax_has_no_private_values_in_errors(self) -> None:
        cases = (
            "PHONE_MAIN_SERIAL",
            "UNKNOWN_PRIVATE_VALUE=secret",
            "PHONE_MAIN_SERIAL='PRIVATE_VALUE",
            "PHONE_MAIN_SERIAL='PRIVATE_VALUE' trailing",
            "PHONE_MAIN_SERIAL=PRIVATE_VALUE\nPHONE_MAIN_SERIAL=again",
            "PHONE_MAIN_SERIAL='PRIVATE_VALUE\x00'",
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ConfigError) as caught:
                    parse_env(text)
                self.assertNotIn("PRIVATE_VALUE", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "config with spaces.env"
        self.values = registry_values(self.root)

    def load(self):
        write_config(self.path, self.values)
        return load_config(self.path)

    def test_obsolete_keys_are_rejected_without_exposing_values(self) -> None:
        for key in (
            "LOCAL_VOLUME_ROOT", "LOCAL_PHOTOS_ROOT", "PHONE_MAIN_DESTINATION_ROOT",
            "CAMERA_MODEL", "CAMERA_CARD_A_UUID_OLD", "DATA_ARCHIVE_A_UUID",
            "VIDEO_ARCHIVE_ROOT_RELATIVE", "PHOTO_REVIEW_RELATIVE",
        ):
            with self.subTest(key=key):
                self.values[key] = "PRIVATE_OLD_VALUE"
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertNotIn("PRIVATE_OLD_VALUE", str(caught.exception))
                del self.values[key]

    def test_incomplete_registry_does_not_fall_back_to_another_schema(self) -> None:
        self.values = {"APP_TIMEZONE": "Europe/Warsaw"}
        with self.assertRaises(ConfigError):
            self.load()

    def test_complete_configuration_and_disabled_secondary(self) -> None:
        config = self.load()
        self.assertEqual(config.app.timezone.key, "Europe/Warsaw")
        self.assertEqual(config.app.min_free_bytes, 1024**3)
        self.assertEqual(config.app.report_keep_runs, 20)
        self.assertEqual(config.app.database_keep_generations, 5)
        self.assertEqual(config.phone("main").initial_policy, "import_missing")
        self.assertTrue(config.phone("main").enabled)
        self.assertFalse(config.phone("secondary").enabled)
        self.assertEqual(config.camera("hero8").ignore_extensions, ("THM", "LRV"))
        self.assertEqual(config.archive_group("data").root_relative, "Data")
        self.assertEqual(config.collection("reviewed").local_root.name, "2.reviewed")
        self.assertEqual(config.source_file, self.path)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_immutable_config_and_private_repr(self) -> None:
        config = self.load()
        with self.assertRaises(ValidationError):
            config.phone("main").alias = "different"
        for item in (config, config.app, config.local, config.phone("main"), config.camera("hero8"),
                     config.archive_group("data"), config.archive_group("data").target("a"), config.collection("reviewed")):
            self.assertNotIn(str(self.root), repr(item))
            self.assertNotIn(config.phone("main").device.serial, repr(item))
            self.assertNotIn(str(self.root), str(item))
            self.assertNotIn(config.phone("main").device.serial, str(item))

    def test_pydantic_validates_model_input_and_hides_error_values(self) -> None:
        settings = self.load().app.model_dump(by_alias=True)
        # model_dump uses field names, so restore the input alias explicitly.
        settings["config_file"] = settings.pop("declared_config_file").as_posix()
        settings["report_keep_runs"] = "PRIVATE_INVALID_NUMBER"
        with self.assertRaises(ValidationError) as caught:
            AppSettings.model_validate(settings)
        self.assertNotIn("PRIVATE_INVALID_NUMBER", str(caught.exception))
        self.assertNotIn("input_value", str(caught.exception))

    def test_paths_with_spaces_and_non_ascii(self) -> None:
        self.values["LOCAL_STAGING_ROOT"] = str(self.root / "Żółte zdjęcia" / "staging")
        config = self.load()
        self.assertEqual(str(config.local.staging_root), self.values["LOCAL_STAGING_ROOT"])
        self.assertFalse(config.local.staging_root.exists())

    def test_initial_policy_is_independent_of_profile_name(self) -> None:
        self.values["PHONE_MAIN_INITIAL_POLICY"] = "baseline_skip_missing"
        self.assertEqual(self.load().phone("main").initial_policy, "baseline_skip_missing")
        self.values["PHONE_SECONDARY_INITIAL_POLICY"] = "baseline_skip_missing"
        self.assertEqual(self.load().phone("secondary").initial_policy, "baseline_skip_missing")

    def test_active_secondary(self) -> None:
        self.values.update(
            PHONE_SECONDARY_TRANSPORT="adb", PHONE_SECONDARY_SERIAL="second-device",
            PHONE_SECONDARY_CAMERA_DIR="/sdcard/DCIM/Camera",
        )
        self.assertTrue(self.load().phone("secondary").enabled)

    def test_partial_secondary_is_rejected(self) -> None:
        for key in (
            "PHONE_SECONDARY_TRANSPORT", "PHONE_SECONDARY_SERIAL", "PHONE_SECONDARY_CAMERA_DIR"
        ):
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = "adb" if key.endswith("TRANSPORT") else "incomplete"
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = original

    def test_optional_phone_metadata_can_be_omitted(self) -> None:
        for key in ("PHONE_MAIN_MODEL", "PHONE_MAIN_ANDROID_VERSION", "PHONE_MAIN_BUILD"):
            self.values.pop(key, None)
        self.assertEqual(self.load().phone("main").device.model, "")

    def test_unconfigured_phone_profiles_can_be_omitted(self) -> None:
        self.values = {
            key: value for key, value in self.values.items() if not key.startswith("PHONE_")
        }
        config = self.load()
        self.assertEqual(config.phones, ())

    def test_active_phones_cannot_use_the_same_device(self) -> None:
        self.values.update(
            PHONE_SECONDARY_TRANSPORT="adb",
            PHONE_SECONDARY_SERIAL=self.values["PHONE_MAIN_SERIAL"],
            PHONE_SECONDARY_CAMERA_DIR="/sdcard/DCIM/Camera",
        )
        with self.assertRaises(ConfigError):
            self.load()

    def test_required_values_and_types(self) -> None:
        cases = {
            "APP_CONFIG_FILE": ("", "   "),
            "APP_DATABASE_FILE": ("", "relative/file", "~/state", "/tmp/../state"),
            "APP_TIMEZONE": ("", "Invalid/PrivateValue", "../UTC"),
            "APP_MIN_FREE_BYTES": ("-1", "0", "1", "1.5", "not-a-number"),
            "APP_REPORT_KEEP_RUNS": ("0", "-2", "2.5"),
            "APP_DATABASE_KEEP_GENERATIONS": ("0",),
            "APP_ARCHIVE_STATE_DIR": (".", "..", "/state", "../state", "state/subdir"),
            "PHONE_MAIN_SERIAL": ("", "REPLACE_WITH_ADB_SERIAL"),
            "PHONE_MAIN_TRANSPORT": ("", "mtp"),
            "PHONE_MAIN_CAMERA_DIR": ("relative/DCIM", "../Camera"),
            "PHONE_MAIN_INITIAL_POLICY": ("", "skip_all"),
            "CAMERA_HERO8_IGNORE_EXTENSIONS": ("THM,LRV,", "THM,LRV,*.TMP"),
            "CAMERA_HERO8_CAMERA_FOLDER": ("../camera", "camera/subdir"),
            "ARCHIVE_DATA_ROOT_RELATIVE": ("/archive", "../archive", "."),
            "COLLECTION_REVIEWED_ARCHIVE_RELATIVE": ("", "../reviewed", "/reviewed"),
        }
        for key, invalid_values in cases.items():
            original = self.values[key]
            for value in invalid_values:
                with self.subTest(key=key, value=value):
                    self.values[key] = value
                    with self.assertRaises(ConfigError) as caught:
                        self.load()
                    self.assertNotIn("PrivateValue", str(caught.exception))
            self.values[key] = original
        del self.values["APP_DATABASE_FILE"]
        with self.assertRaisesRegex(ConfigError, "APP_DATABASE_FILE"):
            self.load()

    def test_document_archive_path_is_required(self) -> None:
        del self.values["COLLECTION_DOCUMENTS_ARCHIVE_RELATIVE"]
        with self.assertRaisesRegex(ConfigError, "COLLECTION_DOCUMENTS_ARCHIVE_RELATIVE"):
            self.load()

    def test_unsafe_path_relationships(self) -> None:
        cases = {
            "LOCAL_STAGING_ROOT": self.values["COLLECTION_PHOTOS_LOCAL_ROOT"] + "/staging",
            "LOCAL_UNDATED_ROOT": self.values["COLLECTION_GOPRO_LOCAL_ROOT"] + "/undated",
            "COLLECTION_GOPRO_LOCAL_ROOT": self.values["COLLECTION_PHOTOS_LOCAL_ROOT"],
            "COLLECTION_PHOTOS_MAIN_LOCAL_ROOT": str(self.root / "elsewhere"),
            "APP_DATABASE_FILE": self.values["LOCAL_VOLUME_WORKING_ROOT"] + "/state.sqlite3",
            "APP_REPORTS_DIR": self.values["ARCHIVE_DATA_TARGET_A_MOUNT"] + "/reports",
            "COLLECTION_REVIEW_ARCHIVE_RELATIVE": self.values["COLLECTION_REVIEWED_ARCHIVE_RELATIVE"],
            "COLLECTION_DOCUMENTS_ARCHIVE_RELATIVE": (
                self.values["COLLECTION_PHOTOS_ARCHIVE_RELATIVE"] + "/docs"
            ),
            "ARCHIVE_DATA_TARGET_B_MOUNT": self.values["ARCHIVE_DATA_TARGET_A_MOUNT"],
            "APP_ARCHIVE_STATE_DIR": self.values["ARCHIVE_DATA_ROOT_RELATIVE"],
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = original

    def test_staging_and_state_cannot_use_archive_or_each_other(self) -> None:
        self.values["LOCAL_STAGING_ROOT"] = self.values["ARCHIVE_DATA_TARGET_A_MOUNT"] + "/staging"
        with self.assertRaises(ConfigError):
            self.load()
        self.values["LOCAL_STAGING_ROOT"] = str(self.root / "external-staging")
        self.values["APP_DATABASE_FILE"] = self.values["LOCAL_STAGING_ROOT"] + "/state.sqlite3"
        with self.assertRaises(ConfigError):
            self.load()
        self.values["APP_DATABASE_FILE"] = str(self.path)
        with self.assertRaises(ConfigError):
            self.load()

    def test_duplicate_identities(self) -> None:
        for key, value in {
            "PHONE_SECONDARY_ALIAS": self.values["PHONE_MAIN_ALIAS"],
            "ARCHIVE_DATA_TARGET_B_UUID": self.values["ARCHIVE_DATA_TARGET_A_UUID"].upper(),
            "ARCHIVE_VIDEO_TARGET_A_UUID": self.values["LOCAL_VOLUME_WORKING_UUID"],
            "CAMERA_CARD_B_UUID": self.values["CAMERA_CARD_A_UUID"],
        }.items():
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = original

    def test_no_environment_override_or_recursive_loading(self) -> None:
        self.values["APP_CONFIG_FILE"] = str(self.root / "does-not-exist.env")
        before = os.environ.copy()
        with patch.dict(os.environ, {"APP_TIMEZONE": "Invalid/Timezone"}):
            config = self.load()
        self.assertEqual(os.environ, before)
        self.assertEqual(config.app.timezone.key, "Europe/Warsaw")
        self.assertEqual(config.source_file, self.path)
        self.assertFalse(config.app.declared_config_file.exists())

    def test_shell_syntax_is_literal_and_never_executed(self) -> None:
        marker = self.root / "executed"
        self.values["PHONE_MAIN_MODEL"] = f"$(touch {marker})"
        config = self.load()
        self.assertEqual(config.phone("main").device.model, self.values["PHONE_MAIN_MODEL"])
        self.assertFalse(marker.exists())

    def test_utf8_bom_and_crlf(self) -> None:
        write_config(self.path, self.values)
        text = self.path.read_text(encoding="utf-8")
        self.path.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8"))
        self.assertTrue(load_config(self.path).phone("main").enabled)

    def test_unreadable_config_does_not_expose_path(self) -> None:
        for kind in ("missing", "directory", "encoding"):
            with self.subTest(kind=kind):
                path = self.root / kind
                if kind == "directory":
                    path.mkdir()
                if kind == "encoding":
                    path.write_bytes(b"\xff")
                with self.assertRaises(ConfigError) as caught:
                    load_config(path)
                self.assertNotIn(str(self.root), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
