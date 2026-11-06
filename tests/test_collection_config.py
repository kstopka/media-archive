"""Generic collections: scopes, policy boundaries and compatibility safety."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_archive.config import CameraCard, CameraProfile, ConfigError, configuration_identities, load_config
from tests.config_fixtures import registry_values, write_config


class CollectionConfigurationTests(unittest.TestCase):
    def test_camera_api_preserves_collection_identity_and_archive_mapping(self):
        config = self.load()
        camera = config.camera('hero8')
        self.assertIsInstance(camera, CameraProfile)
        self.assertTrue(all(isinstance(card, CameraCard) for card in camera.cards))
        self.assertEqual(camera.collection_id, 'gopro')
        self.assertEqual(config.collection('gopro').layout, 'camera_day')
        self.assertEqual(config.archive_mapping('gopro', 'video'),
                         self.values['ARCHIVE_VIDEO_ROOT_RELATIVE'] + '/' +
                         self.values['COLLECTION_GOPRO_ARCHIVE_RELATIVE'])
        self.assertEqual(camera.source_example_subdir, '100GOPRO')
        self.assertEqual(camera.cards, config.camera_cards)
        self.assertEqual(camera.cards[0].uuid, self.values['CAMERA_CARD_A_UUID'])
        self.assertEqual(configuration_identities(config).cameras, frozenset({'hero8'}))

    def test_brand_specific_camera_settings_and_layout_are_rejected(self):
        for key in ('GOPRO_PROFILES', 'GOPRO_CARDS', 'GOPRO_CARD_A_UUID',
                    'GOPRO_HERO8_MODEL'):
            with self.subTest(key=key):
                self.values[key] = 'PRIVATE_OLD_CAMERA_VALUE'
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertNotIn('PRIVATE_OLD_CAMERA_VALUE', str(caught.exception))
                del self.values[key]
        self.values['COLLECTION_GOPRO_LAYOUT'] = 'gopro_day'
        with self.assertRaises(ConfigError):
            self.load()

    def test_double_slash_paths_are_normalized_before_storage_checks(self):
        self.values['LOCAL_STAGING_ROOT'] = '/' + self.values['LOCAL_STAGING_ROOT']
        config = self.load()
        self.assertEqual(config.local.staging_root.anchor, '/')
        self.values['LOCAL_STAGING_ROOT'] = '/' + self.values['COLLECTION_PHOTOS_LOCAL_ROOT']
        with self.assertRaises(ConfigError):
            self.load()

    def test_double_slash_filesystem_root_is_rejected(self):
        self.values['LOCAL_STAGING_ROOT'] = '//'
        with self.assertRaises(ConfigError):
            self.load()

    def test_collection_paths_distinguish_case_but_reject_exact_duplicates(self):
        original = Path(self.values['COLLECTION_PHOTOS_LOCAL_ROOT'])
        variant = original.with_name(original.name.swapcase())
        self.assertNotEqual(original, variant)
        self.values['COLLECTIONS'] += ',case_variant'
        self.values.update(
            COLLECTION_CASE_VARIANT_LOCAL_ROOT=str(variant),
            COLLECTION_CASE_VARIANT_ARCHIVE_RELATIVE='case_variant',
            COLLECTION_CASE_VARIANT_POLICY='append',
        )
        config = self.load()
        self.assertEqual(config.collection('case_variant').local_root, variant)
        self.assertEqual(config.collection('photos').local_root, original)
        self.values['COLLECTION_CASE_VARIANT_LOCAL_ROOT'] = str(original)
        with self.assertRaises(ConfigError):
            self.load()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'config.env'
        self.values = registry_values(self.root)

    def load(self):
        write_config(self.path, self.values)
        return load_config(self.path)

    def add_archive_group(self, name, targets=('a',)):
        self.values['ARCHIVE_GROUPS'] += ',' + name
        prefix = f'ARCHIVE_{name.upper()}'
        self.values[f'{prefix}_ROOT_RELATIVE'] = 'Extra'
        self.values[f'{prefix}_TARGETS'] = ','.join(targets)
        for target in targets:
            target_prefix = f'{prefix}_TARGET_{target.upper()}'
            self.values[f'{target_prefix}_MOUNT'] = str(self.root / f'archive-{name}-{target}')
            self.values[f'{target_prefix}_UUID'] = f'uuid-{name}-{target}'

    def test_archive_group_target_marker_with_explicit_and_inferred_lists(self):
        for name in ('extra_target_group', 'extra_target_group_target_copy'):
            for infer_groups, infer_targets in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(name=name, infer_groups=infer_groups, infer_targets=infer_targets):
                    self.values = registry_values(self.root)
                    self.add_archive_group(name, ('b', 'a'))
                    if infer_groups:
                        del self.values['ARCHIVE_GROUPS']
                    if infer_targets:
                        del self.values[f'ARCHIVE_{name.upper()}_TARGETS']
                    config = self.load()
                    group = config.archive_group(name)
                    self.assertEqual(group.target('a').uuid, f'uuid-{name}-a')
                    expected = ('a', 'b') if infer_targets else ('b', 'a')
                    self.assertEqual(tuple(target.alias for target in group.targets), expected)
                    group_ids = tuple(item.id for item in config.archive_groups)
                    expected_groups = tuple(sorted(('data', 'video', name))) if infer_groups else ('data', 'video', name)
                    self.assertEqual(group_ids, expected_groups)

    def test_archive_target_names_can_also_contain_target_marker(self):
        for infer_targets in (False, True):
            with self.subTest(infer_targets=infer_targets):
                self.values = registry_values(self.root)
                self.add_archive_group('extra_target_group', ('a_target_b', 'a_target_b_target_c'))
                if infer_targets:
                    del self.values['ARCHIVE_EXTRA_TARGET_GROUP_TARGETS']
                group = self.load().archive_group('extra_target_group')
                self.assertEqual(group.target('a_target_b').uuid, 'uuid-extra_target_group-a_target_b')
                self.assertEqual(group.target('a_target_b_target_c').uuid, 'uuid-extra_target_group-a_target_b_target_c')

    def test_archive_shared_group_prefixes_use_explicit_target_membership(self):
        self.add_archive_group('extra', ('a',))
        self.add_archive_group('extra_target_group', ('b',))
        config = self.load()
        self.assertEqual(config.archive_group('extra').target('a').uuid, 'uuid-extra-a')
        self.assertEqual(config.archive_group('extra_target_group').target('b').uuid, 'uuid-extra_target_group-b')
        self.assertEqual(len(config.archive_group('extra').targets), 1)

    def test_ambiguous_archive_target_keys_are_rejected_without_private_values(self):
        for explicit_collision in (False, True):
            with self.subTest(explicit_collision=explicit_collision):
                self.values = registry_values(self.root)
                self.add_archive_group('extra', ('a',))
                self.add_archive_group('extra_target_group', ('b',))
                if explicit_collision:
                    self.values['ARCHIVE_EXTRA_TARGETS'] = 'a,group_target_b'
                else:
                    del self.values['ARCHIVE_EXTRA_TARGETS']
                self.values['ARCHIVE_EXTRA_TARGET_GROUP_TARGET_B_UUID'] = 'PRIVATE_DEVICE_UUID'
                with self.assertRaisesRegex(ConfigError, 'Niejednoznaczne') as caught:
                    self.load()
                self.assertNotIn('PRIVATE_DEVICE_UUID', str(caught.exception))
                self.assertNotIn(str(self.root), str(caught.exception))

    def test_target_only_entries_cannot_create_an_unregistered_archive_group(self):
        for infer_groups in (False, True):
            with self.subTest(infer_groups=infer_groups):
                self.values = registry_values(self.root)
                self.values['ARCHIVE_UNKNOWN_TARGET_A_UUID'] = 'PRIVATE_DEVICE_UUID'
                if infer_groups:
                    del self.values['ARCHIVE_GROUPS']
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertNotIn('PRIVATE_DEVICE_UUID', str(caught.exception))

    def test_archive_target_settings_outside_explicit_list_are_rejected(self):
        self.add_archive_group('extra_target_group', ('a',))
        self.values['ARCHIVE_EXTRA_TARGET_GROUP_TARGET_UNLISTED_UUID'] = 'PRIVATE_DEVICE_UUID'
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertNotIn('PRIVATE_DEVICE_UUID', str(caught.exception))

    def test_template_loads_without_writes_or_device_access(self):
        write_config(self.path, self.values)
        before = self.path.read_bytes()
        with patch('subprocess.run', side_effect=AssertionError('device access')), \
             patch('sqlite3.connect', side_effect=AssertionError('database access')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('directory write')):
            config = load_config(self.path)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.assertEqual(config.backup_profile('data').collection_ids, ('photos', 'documents'))
        self.assertEqual(config.backup_profile('video').collection_ids, ('gopro',))
        self.assertEqual(config.phone('main').collection_id, 'photos_main')
        self.assertFalse(config.phone('secondary').enabled)
        self.assertEqual(config.camera('hero8').collection_id, 'gopro')

    def test_entire_photo_scope_and_months_include_both_sources(self):
        config = self.load()
        self.assertEqual([item.id for item in config.profile_collections('data')], ['photos', 'documents'])
        self.assertEqual([item.id for item in config.month_collections('photos')], ['photos_main', 'photos_secondary'])
        self.assertEqual(config.month_collections('documents'), ())
        self.assertEqual([item.id for item in config.month_collections('gopro')], ['gopro'])
        self.assertEqual(config.backup_profile('data').month_filter, 'pending_selected_targets')

    def test_nearest_policy_applies_for_all_and_nested_scopes(self):
        config = self.load()
        root = config.collection('photos').local_root
        cases = {
            root / '1.to-review' / 'picture.jpg': 'selection',
            root / '2.reviewed' / 'subfolder' / 'picture.jpg': 'selection',
            root / '3.sent-to-print' / 'picture.jpg': 'selection',
            root / 'Print backlog' / 'picture.jpg': 'backlog',
            root / 'Printed' / 'picture.jpg': 'append',
            root / 'Milan' / 'video.mp4': 'append',
            root / 'notes.txt': 'append',
            root / 'All' / 'Main' / '2026' / '2026.11' / 'x.jpg': 'append',
        }
        for path, policy_id in cases.items():
            with self.subTest(path=path):
                self.assertEqual(config.policy_for_path('photos', path).id, policy_id)
        self.assertEqual(config.policy_for_path('review', root / '1.to-review' / 'x.jpg').id, 'selection')
        self.assertEqual(config.policy('backlog').changes, 'keep_both')
        self.assertEqual(config.policy('documents').deletions, 'keep')

    def test_same_content_in_required_paths_is_not_collapsed(self):
        config = self.load()
        root = config.collection('photos').local_root
        # These are two distinct required locations, regardless of file hash.
        original = config.collection_for_path('photos', root / 'All' / 'Main' / 'x.jpg')
        selected = config.collection_for_path('photos', root / '1.to-review' / 'x.jpg')
        self.assertNotEqual(original.id, selected.id)
        self.assertNotEqual(original.archive_relative, selected.archive_relative)

    def test_scope_lookup_rejects_escape_and_relative_paths(self):
        config = self.load()
        root = config.collection('photos').local_root
        for path in (Path('relative/x.jpg'), self.root / 'outside', root / '..' / 'outside'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                config.policy_for_path('photos', path)

    def test_third_profile_with_generic_folder_and_reused_policy(self):
        self.values.update(
            COLLECTIONS=self.values['COLLECTIONS'] + ',finished',
            COLLECTION_FINISHED_LABEL='Finished videos',
            COLLECTION_FINISHED_LOCAL_ROOT=str(self.root / 'working-volume' / 'Finished'),
            COLLECTION_FINISHED_ARCHIVE_RELATIVE='Finished',
            COLLECTION_FINISHED_POLICY='append',
            ARCHIVE_GROUPS='data,video,finished',
            ARCHIVE_FINISHED_ROOT_RELATIVE='Export', ARCHIVE_FINISHED_TARGETS='new',
            ARCHIVE_FINISHED_TARGET_NEW_MOUNT=str(self.root / 'finished-disk'),
            ARCHIVE_FINISHED_TARGET_NEW_UUID='finished-uuid',
            BACKUP_PROFILES='data,video,finished',
            BACKUP_FINISHED_LABEL='Finished', BACKUP_FINISHED_ARCHIVE_GROUP='finished',
            BACKUP_FINISHED_COLLECTIONS='finished',
        )
        config = self.load()
        self.assertEqual(len(config.backup_profiles), 3)
        self.assertEqual(config.backup_profile('finished').collection_ids, ('finished',))
        self.assertEqual(config.backup_profile('video').collection_ids, ('gopro',))
        self.assertEqual(config.month_collections('finished'), ())
        # Reuse the document replacement policy without code changes.
        self.values['COLLECTION_FINISHED_POLICY'] = 'documents'
        self.assertEqual(self.load().collection('finished').policy_id, 'documents')

    def test_collection_can_wait_without_archive_assignment(self):
        self.values['COLLECTIONS'] += ',finished'
        self.values.update(
            COLLECTION_FINISHED_LOCAL_ROOT=str(self.root / 'working-volume' / 'Finished'),
            COLLECTION_FINISHED_ARCHIVE_RELATIVE='Finished', COLLECTION_FINISHED_POLICY='append',
        )
        config = self.load()
        self.assertEqual(config.collection('finished').policy_id, 'append')
        self.assertNotIn('finished', config.backup_profile('video').collection_ids)

    def test_no_fixed_categories_or_required_phone_camera_profiles(self):
        keep = ('APP_', 'LOCAL_VOLUME', 'LOCAL_STAGING_', 'LOCAL_UNDATED_')
        self.values = {key: value for key, value in self.values.items() if key.startswith(keep)}
        self.values.update(
            POLICIES='safe', POLICY_SAFE_CHANGES='keep_both', POLICY_SAFE_DELETIONS='keep',
            COLLECTIONS='research', COLLECTION_RESEARCH_LOCAL_ROOT=str(self.root / 'working-volume' / 'Research'),
            COLLECTION_RESEARCH_ARCHIVE_RELATIVE='Papers', COLLECTION_RESEARCH_POLICY='safe',
            ARCHIVE_GROUPS='lab', ARCHIVE_LAB_TARGETS='one', ARCHIVE_LAB_ROOT_RELATIVE='Storage',
            ARCHIVE_LAB_TARGET_ONE_MOUNT=str(self.root / 'lab-disk'), ARCHIVE_LAB_TARGET_ONE_UUID='lab-uuid',
            BACKUP_PROFILES='lab', BACKUP_LAB_ARCHIVE_GROUP='lab', BACKUP_LAB_COLLECTIONS='research',
        )
        config = self.load()
        self.assertEqual(len(config.collections), 1)
        self.assertEqual(config.phones, ())
        self.assertEqual(config.cameras, ())
        self.assertEqual(config.profile_collections('lab')[0].id, 'research')

    def test_source_paths_are_defined_only_by_collection(self):
        before = self.load()
        old_root = self.values['COLLECTION_PHOTOS_LOCAL_ROOT']
        new_root = str(self.root / 'working-volume' / 'MovedPhotos')
        for key, value in tuple(self.values.items()):
            if key.startswith('COLLECTION_') and key.endswith('_LOCAL_ROOT') and value.startswith(old_root):
                self.values[key] = new_root + value[len(old_root):]
        after = self.load()
        self.assertEqual(before.phone('main'), after.phone('main'))
        self.assertNotEqual(before.collection('photos_main').local_root, after.collection('photos_main').local_root)
        self.assertEqual(before.archive_mapping('photos_main', 'data'), after.archive_mapping('photos_main', 'data'))
        self.assertNotIn('destination_root', type(after.phone('main')).model_fields)

    def test_archive_mapping_is_normalized_and_independent_of_mount(self):
        before = self.load()
        self.values['ARCHIVE_DATA_TARGET_A_MOUNT'] = str(self.root / 'new-mount')
        self.values['ARCHIVE_DATA_ROOT_RELATIVE'] = 'Data//./'
        after = self.load()
        self.assertEqual(before.archive_mapping('photos_main', 'data'), after.archive_mapping('photos_main', 'data'))
        # No database exists yet: a first read cannot enforce historical locks.
        self.values['ARCHIVE_DATA_ROOT_RELATIVE'] = 'Different'
        self.assertNotEqual(before.archive_mapping('photos', 'data'), self.load().archive_mapping('photos', 'data'))
        self.assertFalse(after.app.database_file.exists())

    def test_new_source_on_second_volume_has_no_category_constraint(self):
        self.values.update(
            LOCAL_VOLUMES='working,new', LOCAL_VOLUME_NEW_ROOT=str(self.root / 'other-disk'),
            LOCAL_VOLUME_NEW_UUID='other-working-uuid',
            COLLECTIONS=self.values['COLLECTIONS'] + ',travel',
            COLLECTION_TRAVEL_LOCAL_ROOT=str(self.root / 'other-disk' / 'Trips'),
            COLLECTION_TRAVEL_ARCHIVE_RELATIVE='Trips', COLLECTION_TRAVEL_POLICY='append',
            COLLECTION_TRAVEL_LAYOUT='year_month', PHONE_PROFILES='main,secondary,main_work',
            PHONE_MAIN_WORK_COLLECTION='travel', PHONE_MAIN_WORK_INITIAL_POLICY='baseline_skip_missing',
            PHONE_MAIN_WORK_TRANSPORT='adb', PHONE_MAIN_WORK_SERIAL='travel-device',
            PHONE_MAIN_WORK_CAMERA_DIR='/sdcard/Trips',
            BACKUP_DATA_COLLECTIONS='photos,documents,travel',
        )
        config = self.load()
        self.assertEqual(config.phone('main_work').collection_id, 'travel')
        self.assertEqual(config.phone('main_work').initial_policy, 'baseline_skip_missing')
        self.assertNotEqual(config.phone('main_work').device.serial, config.phone('main').device.serial)
        self.assertEqual(config.collection('travel').local_root, self.root / 'other-disk' / 'Trips')

    def test_read_only_and_new_disk_do_not_change_source_or_mapping(self):
        before = self.load()
        self.values.update(
            ARCHIVE_DATA_TARGETS='a,b,c', ARCHIVE_DATA_TARGET_A_STATE='read_only',
            ARCHIVE_DATA_TARGET_C_MOUNT=str(self.root / 'new-disk'), ARCHIVE_DATA_TARGET_C_UUID='new-uuid',
        )
        after = self.load()
        self.assertEqual(after.archive_group('data').target('a').state, 'read_only')
        self.assertEqual(before.phone('main'), after.phone('main'))
        self.assertEqual(before.archive_mapping('photos', 'data'), after.archive_mapping('photos', 'data'))

    def test_invalid_references_and_registry_lists(self):
        cases = {
            'PHONE_MAIN_COLLECTION': 'missing', 'CAMERA_HERO8_COLLECTION': 'missing',
            'COLLECTION_PHOTOS_MAIN_POLICY': 'missing', 'COLLECTION_PHOTOS_MAIN_PARENT': 'missing',
            'BACKUP_DATA_ARCHIVE_GROUP': 'missing', 'BACKUP_DATA_COLLECTIONS': 'photos,missing',
            'COLLECTIONS': 'photos,PHOTOS', 'POLICIES': 'append',
            'ARCHIVE_GROUPS': 'data', 'BACKUP_PROFILES': 'data',
            'ARCHIVE_DATA_TARGETS': 'a', 'LOCAL_VOLUMES': 'working,WORKING',
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = original

    def test_invalid_tree_and_scope_duplicates(self):
        cases = {
            'COLLECTION_PHOTOS_MAIN_PARENT': '',
            'COLLECTION_PHOTOS_PARENT': 'photos_main',
            'COLLECTION_PHOTOS_MAIN_ARCHIVE_RELATIVE': 'Unrelated',
            'COLLECTION_PHOTOS_MAIN_LOCAL_ROOT': self.values['COLLECTION_PHOTOS_LOCAL_ROOT'],
            'COLLECTION_DOCUMENTS_ARCHIVE_RELATIVE': 'Photos/Documents',
            'BACKUP_DATA_COLLECTIONS': 'photos,review,documents',
            'BACKUP_VIDEO_COLLECTIONS': 'gopro,photos',
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                existed, original = key in self.values, self.values.get(key)
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                if existed:
                    self.values[key] = original
                else:
                    del self.values[key]

    def test_deletion_permission_is_explicit_and_not_inherited(self):
        self.values['COLLECTION_REVIEW_ALLOW_DELETE'] = 'false'
        with self.assertRaises(ConfigError):
            self.load()
        self.values['COLLECTION_REVIEW_ALLOW_DELETE'] = 'true'
        self.values['COLLECTIONS'] += ',review_child'
        self.values.update(
            COLLECTION_REVIEW_CHILD_LOCAL_ROOT=self.values['COLLECTION_REVIEW_LOCAL_ROOT'] + '/Child',
            COLLECTION_REVIEW_CHILD_ARCHIVE_RELATIVE='Photos/1.to-review/Child',
            COLLECTION_REVIEW_CHILD_PARENT='review', COLLECTION_REVIEW_CHILD_POLICY='selection',
        )
        with self.assertRaises(ConfigError):
            self.load()
        self.values['COLLECTION_REVIEW_CHILD_ALLOW_DELETE'] = 'true'
        self.assertTrue(self.load().collection('review_child').allow_delete)

    def test_invalid_types_policies_layout_and_raw_media_policy(self):
        cases = (
            ('POLICY_APPEND_CHANGES', 'overwrite'), ('POLICY_APPEND_DELETIONS', 'always'),
            ('COLLECTION_REVIEW_ALLOW_DELETE', 'maybe'), ('COLLECTION_PHOTOS_MAIN_LAYOUT', 'unknown'),
            ('PHONE_MAIN_INITIAL_POLICY', 'always_skip'), ('COLLECTION_PHOTOS_MAIN_POLICY', 'documents'),
            ('COLLECTION_GOPRO_LAYOUT', 'plain'), ('COLLECTION_PHOTOS_MAIN_LAYOUT', 'plain'),
            ('BACKUP_DATA_MONTH_FILTER', 'all_dates'), ('ARCHIVE_DATA_TARGET_A_STATE', 'unknown'),
        )
        for key, value in cases:
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError):
                    self.load()
                self.values[key] = original

    def test_storage_boundaries_uuid_and_state_privacy(self):
        cases = {
            'LOCAL_STAGING_ROOT': self.values['COLLECTION_PHOTOS_LOCAL_ROOT'] + '/stage',
            'LOCAL_UNDATED_ROOT': self.values['ARCHIVE_DATA_TARGET_A_MOUNT'] + '/undated',
            'APP_DATABASE_FILE': self.values['LOCAL_VOLUME_WORKING_ROOT'] + '/state.sqlite3',
            'COLLECTION_DOCUMENTS_LOCAL_ROOT': str(self.root / 'unregistered'),
            'ARCHIVE_DATA_TARGET_B_UUID': self.values['ARCHIVE_DATA_TARGET_A_UUID'].upper(),
            'CAMERA_CARD_A_UUID': self.values['LOCAL_VOLUME_WORKING_UUID'],
            'APP_ARCHIVE_STATE_DIR': 'Data', 'ARCHIVE_DATA_TARGET_B_MOUNT': self.values['ARCHIVE_DATA_TARGET_A_MOUNT'],
        }
        for key, value in cases.items():
            with self.subTest(key=key):
                original = self.values[key]
                self.values[key] = value
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertNotIn(str(self.root), str(caught.exception))
                self.values[key] = original

    def test_mixed_formats_are_rejected_without_ignoring_old_values(self):
        for key in ('COLLECTION_PHOTOS_LOCAL_ROOT', 'COLLECTION_PHOTOS_MAIN_LOCAL_ROOT', 'CAMERA_MODEL', 'ARCHIVE_DATA_TARGETS'):
            with self.subTest(key=key):
                self.values[key] = 'PRIVATE_CONFLICTING_VALUE'
                with self.assertRaises(ConfigError) as caught:
                    self.load()
                self.assertNotIn('PRIVATE_CONFLICTING_VALUE', str(caught.exception))
                del self.values[key]





    def test_private_representations_and_errors_for_new_models(self):
        config = self.load()
        for item in (*config.collections, *config.policies, *config.backup_profiles, *config.archive_groups):
            self.assertNotIn(str(self.root), repr(item))
            self.assertNotIn(str(self.root), str(item))
        self.values['COLLECTION_PHOTOS_LOCAL_ROOT'] = 'PRIVATE_BAD_PATH'
        with self.assertRaises(ConfigError) as caught:
            self.load()
        self.assertNotIn('PRIVATE_BAD_PATH', str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == '__main__':
    unittest.main()
