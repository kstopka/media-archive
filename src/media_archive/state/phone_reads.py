"""Remember digests obtained from complete reads of identified phone files."""

from sqlalchemy import column, select, table
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import Connection

from ._history_tables import contents
from .migrations import schema_version


_reads = table("phone_reads", *(column(name) for name in (
    "source_id", "device_id", "path", "attributes", "content_id",
)), schema="main")


def load_reads(connection: Connection, source_id: int, device_id: int) -> dict[str, tuple[str, str]]:
    if schema_version(connection) < "0014":
        return {}
    rows = connection.execute(
        select(_reads.c.path, _reads.c.attributes, contents.c.sha256)
        .join_from(_reads, contents, contents.c.id == _reads.c.content_id)
        .where(_reads.c.source_id == source_id, _reads.c.device_id == device_id)
    )
    return {path: (attributes, sha256) for path, attributes, sha256 in rows}


def save_read(connection: Connection, source_id: int, device_id: int, path: str,
              attributes: str, content_id: int) -> None:
    statement = insert(_reads).values(source_id=source_id, device_id=device_id, path=path,
                                      attributes=attributes, content_id=content_id)
    connection.execute(statement.on_conflict_do_update(
        index_elements=[_reads.c.source_id, _reads.c.device_id, _reads.c.path],
        set_={"attributes": attributes, "content_id": content_id},
    ))
