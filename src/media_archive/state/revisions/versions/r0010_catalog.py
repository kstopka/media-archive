"""Record catalog relocations and explicit reviews of SORT conflicts."""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sort_conflicts", sa.Column("kept_both_at", sa.Text), schema="main")
    op.create_table(
        "catalog_moves",
        sa.Column("previous_id", sa.Integer, sa.ForeignKey("main.history_occurrences.id"), primary_key=True),
        sa.Column("current_id", sa.Integer, sa.ForeignKey("main.history_occurrences.id"), nullable=False),
        schema="main",
    )
