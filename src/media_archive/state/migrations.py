"""Run the packaged Alembic revisions inside a caller-owned transaction."""

from pathlib import Path
from threading import Lock

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from .errors import StateError


APPLICATION_ID = 0x4D415243  # MARC, stored in SQLite's application_id header.
_SCRIPT_LOCATION = Path(__file__).with_name("revisions")
_ALEMBIC_LOCK = Lock()  # Alembic's env.py uses a module-level context proxy.


def _config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_SCRIPT_LOCATION).replace("%", "%%"))
    return config


def _head_revision() -> str:
    try:
        revision = ScriptDirectory.from_config(_config()).get_current_head()
    except CommandError:
        raise StateError("Nie można odczytać bieżącej rewizji bazy stanu.") from None
    if revision is None:
        raise StateError("Brak bieżącej rewizji bazy stanu.")
    return revision


def schema_version(connection: Connection) -> str:
    """Return the applied Alembic revision after checking application identity."""
    try:
        if connection.exec_driver_sql("PRAGMA main.application_id").scalar_one() != APPLICATION_ID:
            raise StateError("Plik nie jest bazą stanu Media Archive.")
        context = MigrationContext.configure(connection, opts={"version_table_schema": "main"})
        heads = context.get_current_heads()
        if len(heads) == 1 and isinstance(heads[0], str) and heads[0]:
            revision = ScriptDirectory.from_config(_config()).get_revision(heads[0])
            if revision is not None and revision.revision == heads[0]:
                return revision.revision
    except (SQLAlchemyError, CommandError):
        raise StateError("Nie można odczytać rewizji bazy stanu.") from None
    raise StateError("Nieobsługiwana lub niespójna rewizja bazy stanu.")


def _upgrade(connection: Connection) -> None:
    config = _config()
    config.attributes["connection"] = connection
    try:
        with _ALEMBIC_LOCK:
            command.upgrade(config, "head")
        if connection.exec_driver_sql("PRAGMA main.foreign_key_check").first() is not None:
            raise StateError("Migracja narusza powiązania danych w bazie stanu.")
    except CommandError:
        raise StateError("Nie można wykonać migracji bazy stanu.") from None
