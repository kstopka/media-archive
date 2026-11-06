"""User scenarios for replacing phones and expanding storage configuration."""

from pathlib import Path
import tempfile
import unittest

from media_archive.config import ConfigError, load_config
from tests.config_fixtures import registry_values, write_config


class FlexibleConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / '.env'
        self.values = registry_values(self.root)

    def load(self):
        write_config(self.path, self.values)
        config = load_config(self.path)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        return config

    def test_add_third_phone_with_independent_first_run_policy(self) -> None:
        self.values.update(
            PHONE_PROFILES='main,secondary,family',
            PHONE_FAMILY_ALIAS='family',
            PHONE_FAMILY_COLLECTION='photos_main',
            PHONE_FAMILY_TRANSPORT='adb',
            PHONE_FAMILY_SERIAL='third-phone',
            PHONE_FAMILY_CAMERA_DIR='/sdcard/Pictures/Trips',
            PHONE_FAMILY_INITIAL_POLICY='baseline_skip_missing',
        )
        config = self.load()
        self.assertEqual(len(config.phones), 3)
        self.assertEqual(config.phone('family').initial_policy, 'baseline_skip_missing')
        self.assertEqual(config.phone('family').device.camera_dir, Path('/sdcard/Pictures/Trips'))

    def test_phone_replacement_keeps_profile_identity_and_collection(self) -> None:
        before = self.load().phone('main')
        self.values['PHONE_MAIN_SERIAL'] = 'replacement-phone'
        self.values['PHONE_MAIN_MODEL'] = 'New model'
        after = self.load().phone('main')
        self.assertEqual(before.alias, after.alias)
        self.assertEqual(before.collection_id, after.collection_id)
        self.assertEqual(before.initial_policy, after.initial_policy)
        self.assertNotEqual(before.device.serial, after.device.serial)
        # Persisting initialization and history is deliberately a future SQLite task.

    def test_sources_and_policies_do_not_depend_on_main_secondary_names(self) -> None:
        self.values['PHONE_MAIN_CAMERA_DIR'] = '/storage/emulated/0/Pictures'
        self.values['PHONE_SECONDARY_INITIAL_POLICY'] = 'baseline_skip_missing'
        config = self.load()
        self.assertEqual(config.phone('main').device.camera_dir, Path('/storage/emulated/0/Pictures'))
        self.assertEqual(config.phone('secondary').initial_policy, 'baseline_skip_missing')

    def test_add_local_volume_and_move_camera_collection(self) -> None:
        self.values.update(
            LOCAL_VOLUMES='working,larger',
            LOCAL_VOLUME_LARGER_ROOT=str(self.root / 'larger-disk'),
            LOCAL_VOLUME_LARGER_UUID='new-working-uuid',
            COLLECTION_GOPRO_LOCAL_ROOT=str(self.root / 'larger-disk' / 'GoPro'),
        )
        config = self.load()
        self.assertEqual(len(config.local.volumes), 2)
        self.assertEqual(config.collection("gopro").local_root, self.root / 'larger-disk' / 'GoPro')

    def test_archive_continuation_preserves_old_disk(self) -> None:
        old_uuid = self.values['ARCHIVE_DATA_TARGET_A_UUID']
        self.values.update(
            ARCHIVE_DATA_TARGETS='a,b,a2',
            ARCHIVE_DATA_TARGET_A_STATE='read_only',
            ARCHIVE_DATA_TARGET_A2_MOUNT=str(self.root / 'new-archive'),
            ARCHIVE_DATA_TARGET_A2_UUID='new-archive-uuid',
            ARCHIVE_DATA_TARGET_A2_STATE='active',
        )
        config = self.load()
        self.assertEqual(len(config.archive_group("data").targets), 3)
        self.assertEqual(config.archive_group("data").target('a').uuid, old_uuid)
        self.assertEqual(config.archive_group("data").target('a').state, 'read_only')
        self.assertEqual(config.archive_group("data").target('a2').state, 'active')

    def test_mount_change_keeps_disk_identity(self) -> None:
        before = self.load().archive_group("data").target('a')
        self.values['ARCHIVE_DATA_TARGET_A_MOUNT'] = str(self.root / 'changed-mount')
        after = self.load().archive_group("data").target('a')
        self.assertEqual(before.uuid, after.uuid)
        self.assertEqual(before.alias, after.alias)
        self.assertNotEqual(before.mount, after.mount)

    def test_same_uuid_on_new_target_is_rejected(self) -> None:
        self.values.update(
            ARCHIVE_DATA_TARGETS='a,b,a2',
            ARCHIVE_DATA_TARGET_A2_MOUNT=str(self.root / 'new-archive'),
            ARCHIVE_DATA_TARGET_A2_UUID=self.values['ARCHIVE_DATA_TARGET_A_UUID'],
        )
        with self.assertRaises(ConfigError):
            self.load()

    def test_invalid_registry_and_target_state_are_rejected(self) -> None:
        for key, value in (
            ('PHONE_PROFILES', 'main,MAIN,secondary'),
            ('PHONE_PROFILES', 'main'),
            ('LOCAL_VOLUMES', 'working,'),
            ('ARCHIVE_DATA_TARGETS', 'a,b,a'),
            ('ARCHIVE_DATA_TARGET_A_STATE', 'typo'),
        ):
            with self.subTest(key=key):
                previous = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = previous

    def test_names_with_shared_prefix_do_not_mix_settings(self) -> None:
        self.values.update(
            PHONE_PROFILES='main,secondary,main_work',
            PHONE_MAIN_WORK_COLLECTION='photos_main',
            PHONE_MAIN_WORK_TRANSPORT='adb',
            PHONE_MAIN_WORK_SERIAL='work-phone',
            PHONE_MAIN_WORK_CAMERA_DIR='/sdcard/Work',
            ARCHIVE_DATA_TARGETS='a,b,a_new',
            ARCHIVE_DATA_TARGET_A_NEW_MOUNT=str(self.root / 'new-archive'),
            ARCHIVE_DATA_TARGET_A_NEW_UUID='another-uuid',
        )
        config = self.load()
        self.assertEqual(config.phone('main_work').device.serial, 'work-phone')
        self.assertNotEqual(config.phone('main').device.serial, 'work-phone')
        self.assertEqual(config.archive_group("data").target('a_new').uuid, 'another-uuid')


    def test_add_card_and_confirm_new_uuid_after_format(self) -> None:
        before = self.load().camera('hero8')
        self.values.update(
            CAMERA_CARDS='a,b,c', CAMERA_HERO8_CARDS='a,b,c',
            CAMERA_CARD_C_UUID='third-card-uuid', CAMERA_CARD_A_UUID='formatted-card-uuid',
        )
        after = self.load().camera('hero8')
        self.assertEqual(after.alias, before.alias)
        self.assertEqual(after.collection_id, before.collection_id)
        self.assertEqual(len(after.cards), 3)
        self.assertEqual(after.cards[0].alias, before.cards[0].alias)
        self.assertNotEqual(after.cards[0].uuid, before.cards[0].uuid)

    def test_add_camera_profile_with_its_own_card(self) -> None:
        self.values.update(
            CAMERA_PROFILES='hero8,travel', CAMERA_CARDS='a,b,c',
            CAMERA_CARD_C_UUID='third-card-uuid', CAMERA_TRAVEL_ALIAS='travel',
            CAMERA_TRAVEL_MODEL='Another camera',
            CAMERA_TRAVEL_COLLECTION='gopro',
            CAMERA_TRAVEL_CAMERA_FOLDER='Travel camera', CAMERA_TRAVEL_SOURCE_RELATIVE='DCIM',
            CAMERA_TRAVEL_IGNORE_EXTENSIONS='THM', CAMERA_TRAVEL_CARDS='c',
        )
        config = self.load()
        self.assertEqual(len(config.cameras), 2)
        self.assertEqual(config.camera('travel').cards[0].alias, 'c')
        self.assertEqual(config.camera('travel').ignore_extensions, ('THM',))
        self.values['CAMERA_TRAVEL_CARDS'] = 'a'
        with self.assertRaises(ConfigError):
            self.load()

    def test_camera_can_have_no_exclusions_and_uuid_can_be_unknown(self) -> None:
        self.values['CAMERA_HERO8_IGNORE_EXTENSIONS'] = ''
        self.values['CAMERA_CARD_A_UUID'] = ''
        config = self.load()
        self.assertEqual(config.camera('hero8').ignore_extensions, ())
        self.assertIsNone(config.camera_cards[0].uuid)

    def test_camera_card_registration_and_scope_are_validated(self) -> None:
        cases = (
            ('CAMERA_HERO8_CARDS', 'a,missing'),
            ('CAMERA_HERO8_CARDS', 'a,a'),
            ('CAMERA_CARDS', 'a,b,A'),
            ('COLLECTION_GOPRO_LOCAL_ROOT', str(self.root / 'outside')),
            ('CAMERA_CARD_A_UUID', self.values['ARCHIVE_VIDEO_TARGET_A_UUID']),
        )
        for key, value in cases:
            with self.subTest(key=key):
                previous = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = previous

    def test_video_archive_continuation_preserves_old_disks_and_source(self) -> None:
        before = self.load()
        self.values.update(
            ARCHIVE_VIDEO_TARGETS='a,b,a2', ARCHIVE_VIDEO_TARGET_A_STATE='read_only',
            ARCHIVE_VIDEO_TARGET_A2_MOUNT=str(self.root / 'new-video-archive'),
            ARCHIVE_VIDEO_TARGET_A2_UUID='new-video-archive-uuid', ARCHIVE_VIDEO_TARGET_A2_STATE='active',
        )
        after = self.load()
        self.assertEqual(len(after.archive_group("video").targets), 3)
        self.assertEqual(after.archive_group("video").target('a').uuid, before.archive_group("video").target('a').uuid)
        self.assertEqual(after.archive_group("video").target('a').state, 'read_only')
        self.assertEqual(after.archive_group("video").target('a2').state, 'active')
        self.assertEqual(after.camera('hero8'), before.camera('hero8'))

    def test_camera_entries_can_be_omitted_for_phone_only_configuration(self) -> None:
        self.values = {key: value for key, value in self.values.items() if not key.startswith('CAMERA_')}
        config = self.load()
        self.assertEqual(config.cameras, ())
        self.assertEqual(config.camera_cards, ())
