"""Durable SORT intents, cleanup checkpoints and name conflicts."""

from sqlalchemy import column, select, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, RowMapping

from ._history_queries import find, require_write_transaction
from .migrations import schema_version


def _table(name: str, *fields: str):
    return table(name, *(column(field) for field in fields), schema="main")


moves = _table("sort_moves", "operation_id", "import_id", "source_id", "verification_id", "cleanup_done")
_groups = _table("sort_conflicts", "group_path", "original_name")
_members = _table("sort_conflict_members", "group_path", "content_id", "volume_id", "path")


def supported(connection: Connection) -> bool:
    return schema_version(connection) >= "0009"


def load_moves(connection: Connection) -> tuple[RowMapping, ...]:
    return tuple(connection.execute(moves.select().order_by(moves.c.operation_id)).mappings())


def find_move(connection: Connection, operation_id: int) -> RowMapping | None:
    return find(connection, moves, operation_id=operation_id)


def record_move(connection: Connection, operation_id: int, import_id: int, source_id: int) -> None:
    require_write_transaction(connection)
    connection.execute(moves.insert().values(operation_id=operation_id, import_id=import_id, source_id=source_id))


def verify_move(connection: Connection, operation_id: int, verification_id: int) -> None:
    require_write_transaction(connection)
    connection.execute(moves.update().filter_by(operation_id=operation_id).values(verification_id=verification_id))


def finish_cleanup(connection: Connection, operation_id: int) -> None:
    require_write_transaction(connection)
    connection.execute(moves.update().filter_by(operation_id=operation_id).values(cleanup_done=True))


def undated_locations(connection: Connection) -> dict[int, RowMapping]:
    if not supported(connection):
        return {}
    from . import _history_tables as history
    from .journal import operations

    occurrences = history.occurrences
    query = (select(moves.c.import_id, *occurrences.c).select_from(moves)
             .join(operations, operations.c.id == moves.c.operation_id)
             .join(occurrences, occurrences.c.id == operations.c.target_occurrence_id)
             .where(moves.c.verification_id.is_not(None), occurrences.c.kind == "undated")
             .order_by(moves.c.operation_id))
    return {row.import_id: row for row in connection.execute(query).mappings()}


def undated_targets(connection: Connection) -> tuple[RowMapping, ...]:
    from . import _history_tables as history
    from .journal import operations

    occurrences = history.occurrences
    query = (select(occurrences.c.path, occurrences.c.volume_id, history.contents.c.sha256, history.contents.c.size)
             .select_from(moves).join(operations, operations.c.id == moves.c.operation_id)
             .join(occurrences, occurrences.c.id == operations.c.target_occurrence_id)
             .join(history.contents, history.contents.c.id == occurrences.c.content_id)
             .where(occurrences.c.kind == "undated"))
    return tuple(connection.execute(query).mappings())


def record_conflict_member(connection: Connection, group: str, original_name: str,
                           content_id: int, volume_id: int, path: str) -> None:
    require_write_transaction(connection)
    connection.execute(insert(_groups).values(group_path=group, original_name=original_name).on_conflict_do_nothing())
    connection.execute(insert(_members).values(group_path=group, content_id=content_id,
                                               volume_id=volume_id, path=path).on_conflict_do_nothing())
