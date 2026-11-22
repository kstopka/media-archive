"""Store explicit calendar dates independently of embedded metadata."""

from datetime import date

from sqlalchemy import column, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from ._history_queries import require_write_transaction

_dates = table("sort_dates", column("import_id"), column("calendar_date"), schema="main")


def load_sort_dates(connection: Connection) -> dict[int, date]:
    return {import_id: date.fromisoformat(value) for import_id, value in connection.execute(_dates.select())}


def record_sort_date(connection: Connection, import_id: int, value: date) -> None:
    require_write_transaction(connection)
    connection.execute(insert(_dates).values(import_id=import_id, calendar_date=value.isoformat())
                       .on_conflict_do_update(index_elements=["import_id"], set_={"calendar_date": value.isoformat()}))
