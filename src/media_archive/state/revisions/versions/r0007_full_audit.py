"""Persist the selected volume episodes and SHA-256 checkpoints for full audits."""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_runs",
        sa.Column("id", sa.Text, sa.ForeignKey("main.history_runs.id"), primary_key=True),
        sa.Column("group_id", sa.Text, sa.ForeignKey("main.registry_archive_groups.id"), nullable=False),
        sa.Column("root", sa.Text, nullable=False),
        sa.Column("distinct_disks", sa.Boolean, nullable=False),
        schema="main",
    )
    op.create_table(
        "audit_targets",
        sa.Column("run_id", sa.Text, sa.ForeignKey("main.audit_runs.id"), primary_key=True),
        sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("volume_id", sa.Integer, sa.ForeignKey("main.registry_volume_episodes.id"), nullable=False),
        sa.Column("hardware", sa.Text, nullable=False),
        sa.Column("inventory", sa.Text),
        sa.Column("scanned_at", sa.Text),
        sa.CheckConstraint("position IN (1, 2)"),
        sa.UniqueConstraint("run_id", "volume_id"),
        schema="main",
    )
    op.create_table(
        "audit_checkpoints",
        sa.Column("run_id", sa.Text, primary_key=True),
        sa.Column("position", sa.Integer, primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        sa.Column("attributes", sa.Text, nullable=False),
        sa.Column("content_id", sa.Integer, sa.ForeignKey("main.history_contents.id"), nullable=False),
        sa.Column("read_at", sa.Text, nullable=False),
        sa.ForeignKeyConstraint(["run_id", "position"], ["main.audit_targets.run_id", "main.audit_targets.position"]),
        schema="main",
    )
