"""Persist the requirement to reconcile media after restoring state."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "restore_reconciliation",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("generation_id", sa.Text, nullable=False),
        sa.Column("restored_at", sa.Text, nullable=False),
        sa.CheckConstraint("id = 1"),
        schema="main",
    )
