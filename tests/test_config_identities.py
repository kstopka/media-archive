"""Display labels do not change identity; unknown IDs require registration."""

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from media_archive.config import (
    ConfigError, ConfigurationIdentities, configuration_identities, load_config,
    validate_registered_identities,
)
from tests.config_fixtures import registry_values, write_config


class ConfigurationIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'config.env'
        self.values = registry_values(self.root)

    def load(self):
        write_config(self.path, self.values)
        return load_config(self.path)

    def test_display_renames_preserve_ids_(self) -> None:
        for factory in (registry_values,):
            with self.subTest(format=factory.__name__):
                self.values = factory(self.root)
                before = self.load()
                registered = configuration_identities(before)
                self.values.update(
                    PHONE_MAIN_LABEL='Telefon podróżny',
                    PHONE_SECONDARY_LABEL='Telefon rodzinny',
                    CAMERA_HERO8_LABEL='Kamera na wycieczki',
                )
                if factory is registry_values:
                    self.values['COLLECTION_PHOTOS_MAIN_LABEL'] = 'Nowa nazwa zbioru'
                    self.values['BACKUP_DATA_LABEL'] = 'Nowa nazwa backupu'
                after = self.load()
                self.assertEqual(after.phone('main').label, 'Telefon podróżny')
                self.assertEqual(after.camera('hero8').label, 'Kamera na wycieczki')
                self.assertEqual(configuration_identities(after), registered)
                self.assertEqual(after.phone('main').collection_id, 'photos_main')
                self.assertEqual(after.phone('main').initial_policy, before.phone('main').initial_policy)
                validate_registered_identities(after, registered)

    def test_missing_labels_default_to_alias_without_changing_identity(self) -> None:
        for factory in (registry_values,):
            with self.subTest(format=factory.__name__):
                self.values = factory(self.root)
                for key in ('PHONE_MAIN_LABEL', 'PHONE_SECONDARY_LABEL', 'CAMERA_HERO8_LABEL'):
                    self.values.pop(key, None)
                config = self.load()
                self.assertEqual(config.phone('main').label, 'main')
                self.assertEqual(config.phone('secondary').label, 'secondary')
                self.assertEqual(config.camera('hero8').label, 'hero8')


    def test_duplicate_labels_are_allowed_but_empty_labels_are_rejected(self) -> None:
        for factory in (registry_values,):
            with self.subTest(format=factory.__name__):
                self.values = factory(self.root)
                self.values.update(PHONE_MAIN_LABEL='Telefon', PHONE_SECONDARY_LABEL='Telefon')
                config = self.load()
                self.assertNotEqual(config.phone('main').alias, config.phone('secondary').alias)
                for key in ('PHONE_MAIN_LABEL', 'PHONE_SECONDARY_LABEL', 'CAMERA_HERO8_LABEL'):
                    for value in ('', '   '):
                        with self.subTest(key=key, value=value):
                            previous = self.values.get(key)
                            self.values[key] = value
                            with self.assertRaises(ConfigError):
                                self.load()
                            if previous is None:
                                del self.values[key]
                            else:
                                self.values[key] = previous

    def test_changed_ids_require_registration_in_every_supported_namespace(self) -> None:
        new_id = 'private_new_identity'
        for namespace in ('phones', 'cameras', 'collections', 'backup_profiles', 'archive_groups'):
            with self.subTest(namespace=namespace):
                self.values = registry_values(self.root)
                registered = configuration_identities(self.load())
                if namespace == 'phones':
                    self.values['PHONE_MAIN_ALIAS'] = new_id
                elif namespace == 'cameras':
                    self.values['CAMERA_HERO8_ALIAS'] = new_id
                else:
                    registry, prefix, old_id = {
                        'collections': ('COLLECTIONS', 'COLLECTION', 'photos_main'),
                        'backup_profiles': ('BACKUP_PROFILES', 'BACKUP', 'data'),
                        'archive_groups': ('ARCHIVE_GROUPS', 'ARCHIVE', 'data'),
                    }[namespace]
                    self.values[registry] = ','.join(new_id if item == old_id else item
                                                   for item in self.values[registry].split(','))
                    start = f'{prefix}_{old_id.upper()}_'
                    for key in tuple(self.values):
                        if key.startswith(start):
                            self.values[f'{prefix}_{new_id.upper()}_' + key[len(start):]] = self.values.pop(key)
                    if namespace == 'collections':
                        self.values['PHONE_MAIN_COLLECTION'] = new_id
                    elif namespace == 'archive_groups':
                        self.values['BACKUP_DATA_ARCHIVE_GROUP'] = new_id
                # Structural checks still work without a database. Historical
                # checks explicitly use the previously registered identity set.
                candidate = self.load()
                with self.assertRaises(ConfigError) as caught:
                    validate_registered_identities(candidate, registered)
                self.assertIn(namespace, str(caught.exception))
                self.assertNotIn(new_id, str(caught.exception))
                self.assertNotIn(str(self.root), str(caught.exception))

    def test_new_profile_needs_explicit_registration_before_execution(self) -> None:
        registered = configuration_identities(self.load())
        self.values.update(
            PHONE_PROFILES='main,secondary,family', PHONE_FAMILY_COLLECTION='photos_main',
            PHONE_FAMILY_LABEL='Another phone',
        )
        candidate = self.load()
        with self.assertRaises(ConfigError):
            validate_registered_identities(candidate, registered)
        updated = ConfigurationIdentities(**{
            **registered.model_dump(), 'phones': registered.phones | {'family'},
        })
        validate_registered_identities(candidate, updated)
        self.assertNotIn('family', registered.phones)

    def test_removed_profile_does_not_erase_its_registered_identity(self) -> None:
        registered = configuration_identities(self.load())
        self.values = {key: value for key, value in self.values.items() if not key.startswith('PHONE_MAIN_')}
        self.values['PHONE_PROFILES'] = 'secondary'
        validate_registered_identities(self.load(), registered)
        self.assertIn('main', registered.phones)

    def test_hardware_replacement_and_initial_policy_do_not_create_new_identity(self) -> None:
        registered = configuration_identities(self.load())
        self.values.update(
            PHONE_MAIN_SERIAL='replacement-device', PHONE_MAIN_INITIAL_POLICY='baseline_skip_missing',
            CAMERA_CARD_A_UUID='formatted-card',
        )
        validate_registered_identities(self.load(), registered)

    def test_identity_checks_do_not_write_or_expose_registered_values(self) -> None:
        config = self.load()
        before = self.path.read_bytes()
        with patch('sqlite3.connect', side_effect=AssertionError('database write')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('directory write')):
            registered = configuration_identities(config)
            validate_registered_identities(config, registered)
            with self.assertRaises(ConfigError):
                validate_registered_identities(config, ConfigurationIdentities())
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        self.assertEqual(repr(registered), 'ConfigurationIdentities()')


if __name__ == '__main__':
    unittest.main()
