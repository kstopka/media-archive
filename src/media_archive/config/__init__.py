"""Public configuration API; start with load_config in loader.py."""

from .collections import BackupPolicy, BackupProfile, LocalCollection
from .env import parse_env
from .errors import ConfigError
from .identities import ConfigurationIdentities, configuration_identities, validate_registered_identities
from .loader import load_config
from .models import (
    AppSettings, ArchiveGroup, ArchiveTarget, ConfigModel, Configuration,
    CameraProfile, CameraCard, LocalSettings, LocalVolume, PhoneDevice, PhoneProfile,
)

__all__ = [
    "load_config", "parse_env", "ConfigError", "Configuration", "ConfigModel",
    "AppSettings", "LocalSettings", "PhoneProfile", "CameraProfile", "CameraCard",
    "ArchiveTarget", "ArchiveGroup", "LocalVolume", "PhoneDevice",
    "LocalCollection", "BackupPolicy", "BackupProfile",
    "ConfigurationIdentities", "configuration_identities", "validate_registered_identities",
]
