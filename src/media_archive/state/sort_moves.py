"""Durable SORT intents, cleanup checkpoints and name conflicts."""

from sqlalchemy import column, select, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, RowMapping

from . import _history_tables as history
from ._history_queries import find, require_write_transaction
from .errors import StateError
from .migrations import schema_version


def _table(name: str, *fields: str):
    return table(name, *(column(field) for field in fields), schema="main")


moves = _table("sort_moves", "operation_id", "import_id", "source_id", "verification_id", "cleanup_done")
_groups = _table("sort_conflicts", "group_path", "original_name", "kept_both_at")
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
    from .journal import operations

    occurrences = history.occurrences
    query = (select(moves.c.import_id, *occurrences.c).select_from(moves)
             .join(operations, operations.c.id == moves.c.operation_id)
             .join(occurrences, occurrences.c.id == operations.c.target_occurrence_id)
             .where(moves.c.verification_id.is_not(None), occurrences.c.kind == "undated")
             .order_by(moves.c.operation_id))
    return {row.import_id: row for row in connection.execute(query).mappings()}


def undated_targets(connection: Connection) -> tuple[RowMapping, ...]:
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
    added = connection.execute(insert(_members).values(group_path=group, content_id=content_id,
                                                        volume_id=volume_id, path=path).on_conflict_do_nothing())
    if added.rowcount == 1:
        connection.execute(_groups.update().filter_by(group_path=group).values(kept_both_at=None))


def load_conflicts(connection: Connection) -> tuple[RowMapping, ...]:
    occurrence = history.occurrences
    latest = (select(occurrence.c.id)
              .where(occurrence.c.path == _members.c.path, occurrence.c.volume_id == _members.c.volume_id,
                     occurrence.c.content_id == _members.c.content_id, occurrence.c.kind.in_(("local", "undated")))
              .order_by(occurrence.c.id.desc()).limit(1).correlate(_members).scalar_subquery())
    query = (select(*_groups.c, *_members.c, history.contents.c.sha256, history.contents.c.size,
                    latest.label("occurrence_id"))
             .select_from(_groups).join(_members, _members.c.group_path == _groups.c.group_path)
             .join(history.contents, history.contents.c.id == _members.c.content_id)
             .order_by(_groups.c.group_path, _members.c.path, _members.c.content_id))
    return tuple(connection.execute(query).mappings())


def keep_conflict_variants(connection: Connection, group: str, reviewed_at: str) -> None:
    require_write_transaction(connection)
    members = connection.execute(select(_members.c.content_id).filter_by(group_path=group).distinct()).scalars()
    if len(set(members)) < 2:
        raise StateError("Nie znaleziono grupy z co najmniej dwoma zapisanymi wariantami.")
    connection.execute(_groups.update().filter_by(group_path=group, kept_both_at=None)
                       .values(kept_both_at=reviewed_at))
