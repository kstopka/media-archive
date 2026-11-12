"""Explicit SQLite state API. Importing this package does not access files."""

from .database import create_database, migrate_database, open_database, transaction
from .errors import StateError
from .migrations import schema_version
from .registry import (
    DeviceChange, DeviceKey, RegisteredDevice, load_registered_devices,
    load_registered_identities, register_configuration, validate_registered_configuration,
)

__all__ = [
    "StateError", "create_database", "open_database", "migrate_database",
    "schema_version", "transaction",
    "DeviceChange", "DeviceKey", "RegisteredDevice", "load_registered_devices",
    "load_registered_identities", "register_configuration", "validate_registered_configuration",
]
