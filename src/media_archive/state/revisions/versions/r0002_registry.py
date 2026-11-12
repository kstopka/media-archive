"""Add the persistent configuration and device registries."""

from alembic import op
import sqlalchemy as sa


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registry_collections",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        schema="main",
    )
    op.create_table(
        "registry_sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("collection_id", sa.Text, sa.ForeignKey("main.registry_collections.id"), nullable=False),
        sa.CheckConstraint("kind IN ('phone', 'camera')"),
        sa.UniqueConstraint("kind", "alias"),
        schema="main",
    )
    op.create_table(
        "registry_archive_groups",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("root_relative", sa.Text, nullable=False),
        schema="main",
    )
    op.create_table(
        "registry_backup_profiles",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("label", sa.Text, nullable=False),
        sa.Column("group_id", sa.Text, sa.ForeignKey("main.registry_archive_groups.id"), nullable=False),
        schema="main",
    )
    op.create_table(
        "registry_archive_mappings",
        sa.Column("collection_id", sa.Text, sa.ForeignKey("main.registry_collections.id"), primary_key=True),
        sa.Column("backup_id", sa.Text, sa.ForeignKey("main.registry_backup_profiles.id"), nullable=False),
        sa.Column("parent_id", sa.Text, sa.ForeignKey("main.registry_archive_mappings.collection_id")),
        sa.Column("archive_relative", sa.Text, nullable=False),
        schema="main",
    )
    op.create_table(
        "registry_device_slots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("group_id", sa.Text, sa.ForeignKey("main.registry_archive_groups.id")),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("main.registry_sources.id")),
        sa.CheckConstraint("role IN ('phone', 'card', 'local', 'archive')"),
        sa.CheckConstraint("(role = 'archive') = (group_id IS NOT NULL)"),
        sa.CheckConstraint("source_id IS NULL OR role IN ('phone', 'card')"),
        sa.CheckConstraint("role != 'phone' OR source_id IS NOT NULL"),
        sa.UniqueConstraint("group_id", "alias"),
        schema="main",
    )
    op.create_index(
        "uq_registry_device_slots_without_group", "registry_device_slots", ["role", "alias"],
        unique=True, sqlite_where=sa.text("group_id IS NULL"), schema="main",
    )
    op.create_table(
        "registry_devices",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slot_id", sa.Integer, sa.ForeignKey("main.registry_device_slots.id"), nullable=False),
        sa.Column("serial", sa.Text),
        sa.Column("serial_key", sa.Text, unique=True),
        sa.CheckConstraint("(serial IS NULL) = (serial_key IS NULL)"),
        schema="main",
    )
    op.create_index("ix_registry_devices_slot", "registry_devices", ["slot_id"], schema="main")
    op.create_table(
        "registry_volume_episodes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("device_id", sa.Integer, sa.ForeignKey("main.registry_devices.id"), nullable=False),
        sa.Column("uuid", sa.Text, nullable=False),
        sa.Column("uuid_key", sa.Text, nullable=False, unique=True),
        schema="main",
    )
    op.create_index(
        "ix_registry_volume_episodes_device", "registry_volume_episodes", ["device_id"], schema="main",
    )
