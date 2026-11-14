"""Read the durable requirement left by a restore."""

from sqlalchemy import column, table
from sqlalchemy.engine import Connection, RowMapping

from .errors import StateError
from .migrations import schema_version

restore_state = table("restore_reconciliation", column("id"), column("generation_id"),
                      column("restored_at"), schema="main")


def load_reconciliation(connection: Connection) -> RowMapping | None:
    if schema_version(connection) in ("0001", "0002", "0003", "0004"):
        return None
    return connection.execute(restore_state.select()).mappings().one_or_none()


def require_reconciled(connection: Connection) -> None:
    if load_reconciliation(connection) is not None:
        raise StateError("Przywrócony stan wymaga uzgodnienia z nośnikami przed zapisem mediów.")
