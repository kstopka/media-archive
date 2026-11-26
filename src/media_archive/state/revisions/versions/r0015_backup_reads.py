"""Store complete file reads for incremental BACKUP."""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backup_reads",
        sa.Column("volume_id", sa.Integer, sa.ForeignKey("main.registry_volume_episodes.id"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("attributes", sa.Text, nullable=False),
        sa.Column("content_id", sa.Integer, sa.ForeignKey("main.history_contents.id"), nullable=False),
        schema="main",
    )
