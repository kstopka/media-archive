"""Store approved hardware evidence separately from filesystem UUIDs."""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("registry_devices", sa.Column("hardware_id", sa.Text), schema="main")
