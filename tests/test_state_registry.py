"""Configuration registration against temporary SQLite databases and synthetic hardware."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from media_archive.config import ConfigError, Configuration, configuration_identities, load_config
from media_archive.state import (
    DeviceChange, DeviceKey, RegisteredDevice, StateError, create_database,
    load_registered_devices, load_registered_identities, migrate_database, open_database,
    register_configuration, validate_registered_configuration,
)
from media_archive.state import migrations
from tests.config_fixtures import registry_values, write_config


class StateRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = self.enterContext(TemporaryDirectory())
        self.root = Path(directory)
        self.path = self.root / "state.sqlite3"
        self.settings = self.root / "settings.env"
        self.values = registry_values(self.root)
        self.values["APP_DATABASE_FILE"] = str(self.path)

    def config(self, **changes: str | None) -> Configuration:
        values = self.values.copy()
        for key, value in changes.items():
            if value is None:
                del values[key]
            else:
                values[key] = value
        write_config(self.settings, values)
        return load_config(self.settings)

    def device(self, key: DeviceKey) -> RegisteredDevice:
        return next(device for device in load_registered_devices(self.path) if device.key == key)

    def test_registration_is_explicit_and_reopening_is_read_only(self) -> None:
        config = self.config()
        for action in (register_configuration, validate_registered_configuration):
            with self.assertRaises(StateError):
                action(config)
        self.assertFalse(self.path.exists())

        create_database(self.path)
        empty = self.path.read_bytes()
        with self.assertRaisesRegex(ConfigError, "Niezarejestrowane"):
            validate_registered_configuration(config)
        self.assertEqual(self.path.read_bytes(), empty)
        self.assertFalse(load_registered_identities(self.path).collections)

        register_configuration(config)
        first = self.path.read_bytes()
        self.assertEqual(load_registered_identities(self.path), configuration_identities(config))
        self.assertEqual(len(load_registered_devices(self.path)), 9)
        validate_registered_configuration(config)
        register_configuration(config)
        self.assertEqual(self.path.read_bytes(), first)

    def test_explicit_migration_adds_empty_registry_to_baseline(self) -> None:
        baseline = self.root / "baseline"
        versions = baseline / "versions"
        versions.mkdir(parents=True)
        current = migrations._SCRIPT_LOCATION
        shutil.copy(current / "env.py", baseline)
        shutil.copy(current / "versions" / "r0001_foundation.py", versions)
        with patch.object(migrations, "_SCRIPT_LOCATION", baseline):
            create_database(self.path)
        previous = self.path.read_bytes()
        config = self.config()
        with self.assertRaisesRegex(StateError, "jawnej migracji"):
            validate_registered_configuration(config)
        with self.assertRaisesRegex(StateError, "jawnej migracji"):
            register_configuration(config)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(migrate_database(self.path), migrations._head_revision())
        self.assertFalse(load_registered_identities(self.path).collections)
        register_configuration(config)
        validate_registered_configuration(config)

    def test_labels_and_mounts_can_change_without_changing_identity(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        original = load_registered_devices(self.path)
        candidate = self.config(
            PHONE_MAIN_LABEL="Travel phone",
            COLLECTION_DOCUMENTS_LABEL="Papers",
            BACKUP_DATA_LABEL="Home archive",
            ARCHIVE_DATA_TARGET_A_MOUNT=str(self.root / "alternate-mount"),
            ARCHIVE_DATA_TARGET_A_UUID=self.values["ARCHIVE_DATA_TARGET_A_UUID"].upper(),
        )
        validate_registered_configuration(candidate)
        register_configuration(candidate)
        self.assertEqual(load_registered_devices(self.path), original)
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT label FROM main.registry_sources WHERE kind='phone' AND alias='main'"
            ).scalar_one(), "Travel phone")
        self.assertNotEqual(
            self.device(DeviceKey("archive", "a", "data")).device_id,
            self.device(DeviceKey("archive", "a", "video")).device_id,
        )

    def test_same_source_alias_in_distinct_namespaces_preserves_binding(self) -> None:
        create_database(self.path)
        config = self.config(CAMERA_HERO8_ALIAS="main")
        register_configuration(config)
        registered = load_registered_identities(self.path)
        self.assertIn("main", registered.phones)
        self.assertIn("main", registered.cameras)
        reassigned = self.config(CAMERA_HERO8_ALIAS="main", PHONE_MAIN_COLLECTION="photos_secondary")
        for action in (validate_registered_configuration, register_configuration):
            with self.assertRaisesRegex(ConfigError, "powiązanie"):
                action(reassigned)
        validate_registered_configuration(config)

    def test_first_archive_assignment_is_explicit_and_removal_retains_it(self) -> None:
        create_database(self.path)
        without_assignment = self.config(BACKUP_DATA_COLLECTIONS="photos")
        register_configuration(without_assignment)
        self.assertIn("documents", load_registered_identities(self.path).collections)
        with self.assertRaisesRegex(ConfigError, "jawnej rejestracji"):
            validate_registered_configuration(self.config())
        register_configuration(self.config())
        validate_registered_configuration(without_assignment)
        register_configuration(without_assignment)
        modified = self.config(COLLECTION_DOCUMENTS_ARCHIVE_RELATIVE="OtherDocuments")
        with self.assertRaisesRegex(ConfigError, "przypisanie celu"):
            register_configuration(modified)
        validate_registered_configuration(self.config())

    def test_removed_collection_reserves_its_path_against_new_ids(self) -> None:
        create_database(self.path)
        original = self.config()
        register_configuration(original)
        other_values = {
            key.replace("COLLECTION_DOCUMENTS_", "COLLECTION_OTHER_"): value
            for key, value in self.values.items() if key.startswith("COLLECTION_DOCUMENTS_")
        }
        removed_values = dict.fromkeys(key for key in self.values if key.startswith("COLLECTION_DOCUMENTS_"))
        collections = self.values["COLLECTIONS"].replace(",documents", "")
        removed = self.config(COLLECTIONS=collections, BACKUP_DATA_COLLECTIONS="photos", **removed_values)
        register_configuration(removed)
        for relative in ("Documents", "Documents/Projects"):
            with self.subTest(relative=relative):
                other_values["COLLECTION_OTHER_ARCHIVE_RELATIVE"] = relative
                replacement = self.config(
                    COLLECTIONS=f"{collections},other", BACKUP_DATA_COLLECTIONS="photos,other",
                    **removed_values, **other_values,
                )
                with self.assertRaisesRegex(ConfigError, "zarezerwowaną"):
                    register_configuration(replacement)
                self.assertNotIn("other", load_registered_identities(self.path).collections)
        validate_registered_configuration(original)

    def test_child_mapping_allows_registered_nesting_and_checks_group_root(self) -> None:
        create_database(self.path)
        original = self.config(COLLECTION_GOPRO_ARCHIVE_RELATIVE="GoPro/Clips")
        register_configuration(original)
        child = self.config(
            COLLECTION_GOPRO_ARCHIVE_RELATIVE="GoPro/Clips",
            COLLECTIONS=self.values["COLLECTIONS"] + ",new_child",
            COLLECTION_NEW_CHILD_LOCAL_ROOT=self.values["COLLECTION_PHOTOS_LOCAL_ROOT"] + "/New",
            COLLECTION_NEW_CHILD_ARCHIVE_RELATIVE="Photos/New",
            COLLECTION_NEW_CHILD_POLICY=self.values["COLLECTION_PHOTOS_POLICY"],
            COLLECTION_NEW_CHILD_PARENT="photos",
        )
        register_configuration(child)
        validate_registered_configuration(child)
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT parent_id, archive_relative FROM main.registry_archive_mappings WHERE collection_id='new_child'"
            ).one(), ("photos", "Photos/New"))

        changed_root = self.config(
            ARCHIVE_VIDEO_ROOT_RELATIVE="Video/GoPro",
            COLLECTION_GOPRO_ARCHIVE_RELATIVE="Clips",
        )
        self.assertEqual(original.archive_mapping("gopro", "video"), changed_root.archive_mapping("gopro", "video"))
        with self.assertRaisesRegex(ConfigError, "korzeń grupy"):
            register_configuration(changed_root)

    def test_cannot_move_existing_collection_between_backup_profiles(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        moved = self.config(
            BACKUP_PROFILES="data,video,papers", BACKUP_DATA_COLLECTIONS="photos",
            BACKUP_PAPERS_ARCHIVE_GROUP="data", BACKUP_PAPERS_COLLECTIONS="documents",
        )
        with self.assertRaisesRegex(ConfigError, "przypisanie celu"):
            register_configuration(moved)
        self.assertNotIn("papers", load_registered_identities(self.path).backup_profiles)

    def test_pending_card_can_gain_uuid_and_first_camera_assignment(self) -> None:
        create_database(self.path)
        register_configuration(self.config(CAMERA_CARD_A_UUID="", CAMERA_HERO8_CARDS="b"))
        card = DeviceKey("card", "a")
        pending = self.device(card)
        self.assertIsNotNone(pending.device_id)
        self.assertIsNone(pending.volume_id)
        with self.assertRaises(ConfigError):
            validate_registered_configuration(self.config())
        register_configuration(self.config())
        self.assertEqual(self.device(card).device_id, pending.device_id)
        self.assertIsNotNone(self.device(card).volume_id)
        validate_registered_configuration(self.config())

        second_camera = {
            key.replace("CAMERA_HERO8_", "CAMERA_SECOND_"): value
            for key, value in self.values.items() if key.startswith("CAMERA_HERO8_")
        }
        second_camera["CAMERA_SECOND_CARDS"] = "a"
        if "CAMERA_SECOND_ALIAS" in second_camera:
            second_camera["CAMERA_SECOND_ALIAS"] = "second"
        moved = self.config(CAMERA_PROFILES="hero8,second", CAMERA_HERO8_CARDS="b", **second_camera)
        with self.assertRaisesRegex(ConfigError, "innego profilu"):
            register_configuration(moved)

    def test_reformat_keeps_device_and_replacement_keeps_previous_episodes(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        card = DeviceKey("card", "a")
        previous = self.device(card)
        formatted_config = self.config(CAMERA_CARD_A_UUID="formatted-volume")
        with self.assertRaisesRegex(ConfigError, "potwierdzenia"):
            register_configuration(formatted_config)
        register_configuration(formatted_config, device_changes=[DeviceChange(previous, "reformat")])
        formatted = self.device(card)
        self.assertEqual(formatted.device_id, previous.device_id)
        self.assertNotEqual(formatted.volume_id, previous.volume_id)
        validate_registered_configuration(formatted_config)

        replacement = self.config(CAMERA_CARD_A_UUID="replacement-card")
        with self.assertRaisesRegex(ConfigError, "potwierdzenia"):
            register_configuration(replacement, device_changes=[DeviceChange(previous, "replace")])
        register_configuration(replacement, device_changes=[DeviceChange(formatted, "replace")])
        new = self.device(card)
        self.assertNotEqual(new.device_id, previous.device_id)
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT id, device_id FROM main.registry_volume_episodes WHERE id IN (?, ?, ?) ORDER BY id",
                (previous.volume_id, formatted.volume_id, new.volume_id),
            ).all(), [(previous.volume_id, previous.device_id), (formatted.volume_id, previous.device_id),
                      (new.volume_id, new.device_id)])

    def test_previous_uuid_cannot_be_registered_under_a_new_alias(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        previous = self.device(DeviceKey("card", "a"))
        register_configuration(self.config(CAMERA_CARD_A_UUID="formatted-volume"), device_changes=[
            DeviceChange(previous, "reformat"),
        ])
        duplicate = self.config(
            CAMERA_CARD_A_UUID="formatted-volume", CAMERA_CARDS="a,b,c",
            CAMERA_CARD_C_UUID=self.values["CAMERA_CARD_A_UUID"].upper(),
        )
        with self.assertRaisesRegex(ConfigError, "zapisanego epizodu") as failure:
            register_configuration(duplicate)
        self.assertNotIn(self.values["CAMERA_CARD_A_UUID"].upper(), str(failure.exception))
        self.assertNotIn(DeviceKey("card", "c"), {device.key for device in load_registered_devices(self.path)})

    def test_phone_replacement_and_new_registration_roll_back_together(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        phone = DeviceKey("phone", "main")
        previous = self.device(phone)
        candidate = self.config(
            PHONE_MAIN_SERIAL="next-serial", COLLECTIONS=self.values["COLLECTIONS"] + ",papers",
            COLLECTION_PAPERS_LOCAL_ROOT=self.values["COLLECTION_DOCUMENTS_LOCAL_ROOT"] + "-incoming",
            COLLECTION_PAPERS_ARCHIVE_RELATIVE="Papers", COLLECTION_PAPERS_POLICY="documents",
        )
        old_card = self.device(DeviceKey("card", "a"))
        # The unused card approval is detected after the phone replacement was inserted.
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ConfigError, "nie odpowiada"):
            register_configuration(candidate, device_changes=[
                DeviceChange(previous, "replace"), DeviceChange(old_card, "replace"),
            ])
        self.assertEqual(self.device(phone), previous)
        self.assertNotIn("papers", load_registered_identities(self.path).collections)
        self.assertEqual(self.path.read_bytes(), before)
        register_configuration(candidate, device_changes=[DeviceChange(previous, "replace")])
        self.assertNotEqual(self.device(phone).device_id, previous.device_id)
        self.assertIn("papers", load_registered_identities(self.path).collections)
        with open_database(self.path) as connection:
            self.assertEqual(connection.exec_driver_sql(
                "SELECT serial FROM main.registry_devices WHERE id=?", (previous.device_id,),
            ).scalar_one(), previous.serial)

    def test_simultaneous_replacements_cannot_use_the_same_old_approval(self) -> None:
        create_database(self.path)
        register_configuration(self.config())
        phone = DeviceKey("phone", "main")
        previous = self.device(phone)
        candidates = [self.config(PHONE_MAIN_SERIAL=serial) for serial in ("new-one", "new-two")]

        def replace(candidate: Configuration) -> bool:
            try:
                register_configuration(candidate, device_changes=[DeviceChange(previous, "replace")])
            except ConfigError as error:
                self.assertIn("potwierdzenia", str(error))
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(replace, candidates)), [False, True])
        self.assertIn(self.device(phone).serial, ("new-one", "new-two"))


if __name__ == "__main__":
    unittest.main()
