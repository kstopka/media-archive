"""Pin the required archive path separately for each member of a pair."""

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "history_archive_paths",
        sa.Column("local_occurrence_id", sa.Integer, sa.ForeignKey("main.history_archive_pairs.local_occurrence_id"),
                  primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("main.registry_devices.id"), primary_key=True),
        sa.Column("path", sa.Text, nullable=False),
        schema="main",
    )
    op.execute("""
        INSERT INTO main.history_archive_paths (local_occurrence_id, device_id, path)
        SELECT local_occurrence_id, first_device_id, required_path FROM main.history_archive_pairs
        UNION ALL
        SELECT local_occurrence_id, second_device_id, required_path FROM main.history_archive_pairs
    """)
