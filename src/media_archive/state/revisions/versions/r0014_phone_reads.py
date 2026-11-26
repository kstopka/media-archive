"""Cache complete phone reads for incremental import planning."""

from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "phone_reads",
        sa.Column("source_id", sa.Integer, sa.ForeignKey("main.registry_sources.id"), primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("main.registry_devices.id"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("attributes", sa.Text, nullable=False),
        sa.Column("content_id", sa.Integer, sa.ForeignKey("main.history_contents.id"), nullable=False),
        schema="main",
    )
