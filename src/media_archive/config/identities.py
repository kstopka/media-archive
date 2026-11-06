"""Pure identity checks; registration storage belongs to the future state layer."""

from .base import ConfigModel
from .errors import ConfigError
from .field_types import Identifier, RegistryId
from .models import Configuration


class ConfigurationIdentities(ConfigModel):
    """IDs registered in state; labels, paths and hardware are not identity keys."""

    phones: frozenset[Identifier] = frozenset()
    cameras: frozenset[Identifier] = frozenset()
    collections: frozenset[RegistryId] = frozenset()
    backup_profiles: frozenset[RegistryId] = frozenset()
    archive_groups: frozenset[RegistryId] = frozenset()


def configuration_identities(config: Configuration) -> ConfigurationIdentities:
    """Describe configured IDs without registering or persisting anything."""
    return ConfigurationIdentities(
        phones=frozenset(profile.alias for profile in config.phones),
        cameras=frozenset(profile.alias for profile in config.cameras),
        collections=frozenset(item.id for item in config.collections),
        backup_profiles=frozenset(item.id for item in config.backup_profiles),
        archive_groups=frozenset(item.id for item in config.archive_groups),
    )


def validate_registered_identities(
    config: Configuration, registered: ConfigurationIdentities,
) -> None:
    """Reject unknown IDs; never infer a rename, register a source or reset state.

    The caller must supply IDs loaded from trusted registration state, not a
    snapshot freshly derived from the candidate configuration. Removed entries
    may stay registered for offline history. New entries need explicit
    registration before execution. load_config() does not call this guard.
    """
    current = configuration_identities(config)
    unknown = [name for name in ConfigurationIdentities.model_fields
               if getattr(current, name) - getattr(registered, name)]
    if unknown:
        raise ConfigError(
            "Niezarejestrowane identyfikatory konfiguracji: " + ", ".join(unknown)
            + ". Dodanie lub zmiana ID wymaga jawnej rejestracji; "
            "nie wolno automatycznie tworzyć historii ani ponawiać inicjalizacji."
        )
