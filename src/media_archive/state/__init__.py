"""Explicit SQLite state API. Importing this package does not access files."""

from .database import create_database, migrate_database, open_database, transaction
from .errors import StateError
from .migrations import schema_version

__all__ = [
    "StateError", "create_database", "open_database", "migrate_database",
    "schema_version", "transaction",
]
