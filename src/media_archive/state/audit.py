"""SQLite state for resumable full archive audits."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import and_, bindparam, column, select, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection, RowMapping
from sqlalchemy.sql.selectable import TableClause

from . import _history_tables as history
from ._history_queries import find
from .database import transaction
from .errors import StateError
from .history import create_run, record_content, record_observation, record_occurrence


def _table(name: str, *fields: str) -> TableClause:
    return table(name, *(column(field) for field in fields), schema="main")


_runs = _table("audit_runs", "id", "group_id", "root", "distinct_disks")
_targets = _table("audit_targets", "run_id", "position", "alias", "volume_id", "hardware", "inventory", "scanned_at")
_checkpoints = _table("audit_checkpoints", "run_id", "position", "path", "attributes", "content_id", "read_at")


@dataclass(frozen=True)
class Checkpoint:
    attributes: str
    sha256: str
    read_at: str
    content_id: int


def start_full_audit(connection: Connection, group: str, root: Path, targets: Iterable[tuple[str, int]]) -> str:
    with transaction(connection):
        run_id = create_run(connection)
        connection.execute(_runs.insert().values(id=run_id, group_id=group, root=root.as_posix(), distinct_disks=False))
        connection.execute(_targets.insert(), [
            {"run_id": run_id, "position": position, "alias": alias, "volume_id": volume_id, "hardware": "[]"}
            for position, (alias, volume_id) in enumerate(targets, start=1)
        ])
    return run_id


def load_full_audit(connection: Connection, run_id: str) -> tuple[RowMapping, tuple[RowMapping, ...]]:
    with connection.begin():
        run = find(connection, _runs, id=run_id)
        if run is None:
            raise StateError("Nie znaleziono pełnego audytu o podanym ID.")
        targets = tuple(connection.execute(
            _targets.select().filter_by(run_id=run_id).order_by(_targets.c.position)
        ).mappings())
        if len(targets) != 2:
            raise StateError("Zapis pełnego audytu nie zawiera dwóch nośników.")
    return run, targets


def confirm_distinct_disks(connection: Connection, run_id: str) -> None:
    with transaction(connection):
        connection.execute(_runs.update().where(_runs.c.id == run_id).values(distinct_disks=True))


def update_full_audit_target(connection: Connection, run_id: str, position: int, *,
                             inventory: str | None, scanned_at: str | None, hardware: str) -> None:
    with transaction(connection):
        connection.execute(_targets.update().where(
            _targets.c.run_id == run_id, _targets.c.position == position,
        ).values(inventory=inventory, scanned_at=scanned_at, hardware=hardware))


def load_checkpoints(connection: Connection, run_id: str, position: int) -> dict[Path, Checkpoint]:
    with connection.begin():
        rows = connection.execute(select(
            _checkpoints.c.path, _checkpoints.c.attributes, history.contents.c.sha256, _checkpoints.c.read_at,
            _checkpoints.c.content_id,
        ).join_from(_checkpoints, history.contents, _checkpoints.c.content_id == history.contents.c.id).where(
            _checkpoints.c.run_id == run_id, _checkpoints.c.position == position,
        ))
        return {Path(path): Checkpoint(attributes, sha256, read_at, content_id)
                for path, attributes, sha256, read_at, content_id in rows}


def archive_checkpoint_reads(connection: Connection, group: str, root: Path,
                             volume_ids: set[int]) -> dict[tuple[int, Path, str], str]:
    other = _targets.alias()
    rows = connection.execute(
        select(_targets.c.volume_id, _checkpoints.c.path, _checkpoints.c.attributes, history.contents.c.sha256)
        .select_from(_runs.join(_targets, _runs.c.id == _targets.c.run_id)
                     .join(other, and_(other.c.run_id == _runs.c.id, other.c.position != _targets.c.position))
                     .join(_checkpoints, and_(_checkpoints.c.run_id == _runs.c.id,
                                              _checkpoints.c.position == _targets.c.position))
                     .join(history.contents, history.contents.c.id == _checkpoints.c.content_id))
        .where(_runs.c.group_id == group, _runs.c.root == root.as_posix(), _runs.c.distinct_disks.is_(True),
               _targets.c.scanned_at.is_not(None), other.c.scanned_at.is_not(None),
               _targets.c.volume_id.in_(volume_ids))
        .order_by(_checkpoints.c.read_at)
    )
    return {(volume, root / path, attributes): digest for volume, path, attributes, digest in rows}


def discard_checkpoints(connection: Connection, run_id: str, position: int, paths: Iterable[Path]) -> None:
    rows = [{"old_path": path.as_posix()} for path in paths]
    if rows:
        with transaction(connection):
            connection.execute(_checkpoints.delete().where(
                _checkpoints.c.run_id == run_id, _checkpoints.c.position == position,
                _checkpoints.c.path == bindparam("old_path"),
            ), rows)


def save_checkpoint(connection: Connection, run_id: str, position: int, volume_id: int, root: Path,
                    path: Path, attributes: str, size: int, modified_ns: int, sha256: str,
                    read_at: datetime) -> Checkpoint:
    with transaction(connection):
        content = record_content(connection, sha256, size)
        occurrence = record_occurrence(connection, "archive", (root / path).as_posix(),
                                       volume_id=volume_id, content_id=content)
        record_observation(connection, occurrence, run_id, observed_at=read_at,
                           present=True, size=size, mtime_ns=modified_ns)
        statement = insert(_checkpoints).values(run_id=run_id, position=position, path=path.as_posix(),
                                               attributes=attributes, content_id=content, read_at=read_at.isoformat())
        connection.execute(statement.on_conflict_do_update(
            index_elements=[_checkpoints.c.run_id, _checkpoints.c.position, _checkpoints.c.path],
            set_={"attributes": attributes, "content_id": content, "read_at": read_at.isoformat()},
        ))
    return Checkpoint(attributes, sha256, read_at.isoformat(), content)
