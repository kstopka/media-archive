"""Keep approved archive removal intents until filesystem and history agree."""

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "history_pending_archive_removals",
        sa.Column("volume_id", sa.Integer, sa.ForeignKey("main.registry_volume_episodes.id"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("main.history_runs.id"), nullable=False),
        schema="main",
    )
