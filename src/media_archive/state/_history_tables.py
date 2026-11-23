"""SQLAlchemy Core statement targets for revision 0003."""

from sqlalchemy import column, table
from sqlalchemy.sql.selectable import TableClause


def _table(name: str, *fields: str) -> TableClause:
    return table(f"history_{name}", *(column(field) for field in fields), schema="main")


runs = _table("runs", "id")
contents = _table("contents", "id", "sha256", "size")
occurrences = _table("occurrences", "id", "content_id", "kind", "path", "source_id", "collection_id",
                     "device_id", "volume_id")
observations = _table("observations", "id", "occurrence_id", "run_id", "observed_at", "present", "size", "mtime_ns")
verifications = _table("verifications", "id", "occurrence_id", "run_id", "verified_at", "scope", "expected_sha256",
                       "read_sha256", "bytes_read", "result")
imports = _table("imports", "id", "source_occurrence_id", "staging_occurrence_id", "verification_id")
placements = _table("placements", "import_id", "local_occurrence_id", "verification_id")
pairs = _table("archive_pairs", "local_occurrence_id", "first_device_id", "second_device_id", "required_path")
archive_paths = _table("archive_paths", "local_occurrence_id", "device_id", "path")
copies = _table("archive_copies", "verification_id", "local_occurrence_id")
initializations = _table("initializations", "source_id", "policy", "boundary", "baseline_device_id",
                         "started_run_id", "completed_run_id")
scopes = _table("initialization_scopes", "source_id", "phase", "scope", "cursor", "run_id", "completed")
baseline_skips = _table("baseline_skips", "occurrence_id", "source_id", "run_id")
