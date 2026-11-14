"""Small shared queries for history and initialization."""

from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.selectable import TableClause

from .errors import StateError


def require_write_transaction(connection: Connection) -> None:
    if not connection.in_transaction() or not connection.info.get("state_write_transaction"):
        raise StateError("Zapis historii wymaga otwartej transakcji zapisu bazy stanu.")


def find(connection: Connection, table: TableClause, **key: object) -> RowMapping | None:
    return connection.execute(table.select().filter_by(**key)).mappings().one_or_none()


def require_row(connection: Connection, table: TableClause, **key: object) -> RowMapping:
    row = find(connection, table, **key)
    if row is None:
        raise StateError("Brak wymaganego rekordu historii lub rejestru.")
    return row


def insert_id(connection: Connection, table: TableClause, **values: object) -> int:
    row_id = connection.execute(table.insert().values(**values)).lastrowid
    if row_id is None:
        raise StateError("Nie można odczytać identyfikatora zapisanego rekordu.")
    return row_id
