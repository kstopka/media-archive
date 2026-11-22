"""Persist manually assigned dates for imports awaiting SORT."""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sort_dates",
        sa.Column("import_id", sa.Integer, sa.ForeignKey("main.history_imports.id"), primary_key=True),
        sa.Column("calendar_date", sa.Text, nullable=False),
        schema="main",
    )
