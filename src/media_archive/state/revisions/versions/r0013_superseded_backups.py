"""Record a newer verified archive copy that superseded an unfinished replacement."""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_superseded_backups",
        sa.Column("operation_id", sa.Integer, sa.ForeignKey("main.journal_operations.id"), primary_key=True),
        sa.Column("verification_id", sa.Integer, sa.ForeignKey("main.history_verifications.id"), nullable=False),
        schema="main",
    )
