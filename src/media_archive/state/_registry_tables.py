"""SQLAlchemy Core statement targets for the registry's persistent tables."""

from sqlalchemy import column, table
from sqlalchemy.sql.selectable import TableClause


def _table(name: str, *fields: str) -> TableClause:
    return table(f"registry_{name}", *(column(field) for field in fields), schema="main")


collections = _table("collections", "id", "label")
sources = _table("sources", "id", "kind", "alias", "label", "collection_id")
archive_groups = _table("archive_groups", "id", "root_relative")
backup_profiles = _table("backup_profiles", "id", "label", "group_id")
archive_mappings = _table("archive_mappings", "collection_id", "backup_id", "parent_id", "archive_relative")
device_slots = _table("device_slots", "id", "role", "alias", "group_id", "source_id")
devices = _table("devices", "id", "slot_id", "serial", "serial_key")
volume_episodes = _table("volume_episodes", "id", "device_id", "uuid", "uuid_key")
