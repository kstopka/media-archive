"""Find previously verified content for unchanged local and archive files."""

from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import column, select, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from ._history_tables import contents
from .audit import archive_checkpoint_reads


_reads = table("backup_reads", *(column(name) for name in (
    "volume_id", "path", "attributes", "content_id",
)), schema="main")


def load_reads(connection: Connection, volume_ids: set[int], *, archive_group: str,
               archive_root: Path) -> dict[tuple[int, Path, str], str]:
    reads = archive_checkpoint_reads(connection, archive_group, archive_root, volume_ids)
    rows = connection.execute(
        select(_reads.c.volume_id, _reads.c.path, _reads.c.attributes, contents.c.sha256)
        .join_from(_reads, contents, contents.c.id == _reads.c.content_id)
        .where(_reads.c.volume_id.in_(volume_ids))
    )
    reads.update({(volume, Path(path), attributes): digest for volume, path, attributes, digest in rows})
    return reads


def save_reads(connection: Connection, reads: Iterable[tuple[int, str, str, int]]) -> None:
    rows = [{"volume_id": volume, "path": path, "attributes": attributes, "content_id": content}
            for volume, path, attributes, content in reads]
    if rows:
        statement = insert(_reads)
        connection.execute(statement.on_conflict_do_update(
            index_elements=[_reads.c.volume_id, _reads.c.path],
            set_={"attributes": statement.excluded.attributes, "content_id": statement.excluded.content_id},
        ), rows)
