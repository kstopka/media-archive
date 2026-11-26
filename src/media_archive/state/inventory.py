"""Store each completed inventory scope as one atomic history run."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from . import _history_tables as history
from ._history_queries import find
from .database import transaction
from .history import record_content, record_observation, record_occurrence
from .backup_reads import save_reads


@dataclass(frozen=True)
class InventoryFile:
    path: str
    collection_id: str | None
    size: int
    modified_ns: int
    observed_at: datetime
    sha256: str | None = None
    attributes: str | None = None


def scope_file_count(connection: Connection, run_id: str) -> int | None:
    """Return the completed scope's file count, including zero for an empty scope."""
    with connection.begin():
        if find(connection, history.runs, id=run_id) is None:
            return None
        return connection.execute(select(func.count()).select_from(history.observations)
                                  .filter_by(run_id=run_id)).scalar_one()


def save_scope(connection: Connection, run_id: str, kind: Literal["local", "archive"],
               volume_id: int, files: Iterable[InventoryFile]) -> None:
    with transaction(connection):
        connection.execute(history.runs.insert().values(id=run_id))
        reads = []
        for file in files:
            content_id = None if file.sha256 is None else record_content(connection, file.sha256, file.size)
            occurrence = record_occurrence(connection, kind, file.path, content_id=content_id,
                                           collection_id=file.collection_id, volume_id=volume_id)
            record_observation(connection, occurrence, run_id, observed_at=file.observed_at,
                               present=True, size=file.size, mtime_ns=file.modified_ns)
            if content_id is not None and file.attributes is not None:
                reads.append((volume_id, file.path, file.attributes, content_id))
        save_reads(connection, reads)
