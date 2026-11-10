"""Establish the baseline Alembic revision before application tables are added."""

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the baseline in Alembic's revision table."""
