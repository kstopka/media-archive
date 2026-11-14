"""Add media history, archive requirements and initialization checkpoints."""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def _ref(name: str, target: str, *, text: bool = False, nullable: bool = False, primary: bool = False) -> sa.Column:
    return sa.Column(name, sa.Text if text else sa.Integer, sa.ForeignKey(f"main.{target}"),
                     nullable=nullable, primary_key=primary)


def upgrade() -> None:
    op.create_table("history_runs", sa.Column("id", sa.Text, primary_key=True), schema="main")
    op.create_table(
        "history_contents",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("sha256", sa.Text, nullable=False),
        sa.Column("size", sa.Integer, nullable=False),
        sa.CheckConstraint("size >= 0"),
        sa.UniqueConstraint("sha256", "size"),
        schema="main",
    )
    op.create_table(
        "history_occurrences",
        sa.Column("id", sa.Integer, primary_key=True),
        _ref("content_id", "history_contents.id", nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        _ref("source_id", "registry_sources.id", nullable=True),
        _ref("collection_id", "registry_collections.id", text=True, nullable=True),
        _ref("device_id", "registry_devices.id", nullable=True),
        _ref("volume_id", "registry_volume_episodes.id", nullable=True),
        sa.CheckConstraint("kind IN ('source', 'staging', 'local', 'archive')"),
        schema="main",
    )
    op.create_table(
        "history_observations",
        sa.Column("id", sa.Integer, primary_key=True),
        _ref("occurrence_id", "history_occurrences.id"),
        _ref("run_id", "history_runs.id", text=True),
        sa.Column("observed_at", sa.Text, nullable=False),
        sa.Column("present", sa.Boolean(create_constraint=True), nullable=False),
        sa.Column("size", sa.Integer),
        sa.Column("mtime_ns", sa.Integer),
        schema="main",
    )
    op.create_table(
        "history_verifications",
        sa.Column("id", sa.Integer, primary_key=True),
        _ref("occurrence_id", "history_occurrences.id"),
        _ref("run_id", "history_runs.id", text=True),
        sa.Column("verified_at", sa.Text, nullable=False),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column("expected_sha256", sa.Text, nullable=False),
        sa.Column("read_sha256", sa.Text),
        sa.Column("bytes_read", sa.Integer),
        sa.Column("result", sa.Text, nullable=False),
        sa.CheckConstraint("scope IN ('full', 'partial')"),
        sa.CheckConstraint("result IN ('match', 'mismatch', 'incomplete')"),
        schema="main",
    )
    op.create_table(
        "history_imports",
        sa.Column("id", sa.Integer, primary_key=True),
        _ref("source_occurrence_id", "history_occurrences.id"),
        _ref("staging_occurrence_id", "history_occurrences.id"),
        _ref("verification_id", "history_verifications.id"),
        sa.UniqueConstraint("staging_occurrence_id"),
        schema="main",
    )
    op.create_table(
        "history_placements",
        _ref("import_id", "history_imports.id", primary=True),
        _ref("local_occurrence_id", "history_occurrences.id"),
        _ref("verification_id", "history_verifications.id"),
        schema="main",
    )
    op.create_table(
        "history_archive_pairs",
        _ref("local_occurrence_id", "history_occurrences.id", primary=True),
        _ref("first_device_id", "registry_devices.id"),
        _ref("second_device_id", "registry_devices.id"),
        sa.Column("required_path", sa.Text, nullable=False),
        sa.CheckConstraint("first_device_id < second_device_id"),
        schema="main",
    )
    op.create_table(
        "history_archive_copies",
        _ref("verification_id", "history_verifications.id", primary=True),
        _ref("local_occurrence_id", "history_archive_pairs.local_occurrence_id", primary=True),
        schema="main",
    )
    op.create_table(
        "history_initializations",
        _ref("source_id", "registry_sources.id", primary=True),
        sa.Column("policy", sa.Text, nullable=False),
        sa.Column("boundary", sa.Text, nullable=False),
        _ref("baseline_device_id", "registry_devices.id", nullable=True),
        _ref("started_run_id", "history_runs.id", text=True),
        _ref("completed_run_id", "history_runs.id", text=True, nullable=True),
        sa.CheckConstraint("policy IN ('import_missing', 'baseline_skip_missing')"),
        sa.CheckConstraint("(policy = 'baseline_skip_missing') = (baseline_device_id IS NOT NULL)"),
        schema="main",
    )
    op.create_table(
        "history_initialization_scopes",
        _ref("source_id", "history_initializations.source_id", primary=True),
        sa.Column("phase", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text, primary_key=True),
        sa.Column("cursor", sa.Text),
        _ref("run_id", "history_runs.id", text=True, nullable=True),
        sa.Column("completed", sa.Boolean(create_constraint=True), nullable=False),
        sa.CheckConstraint("phase IN ('comparison', 'processing')"),
        sa.CheckConstraint("completed = 0 OR run_id IS NOT NULL"),
        schema="main",
    )
    op.create_table(
        "history_baseline_skips",
        _ref("occurrence_id", "history_occurrences.id", primary=True),
        _ref("source_id", "history_initializations.source_id"),
        _ref("run_id", "history_runs.id", text=True),
        schema="main",
    )
    op.create_index("ix_history_occurrences_location", "history_occurrences", ["kind", "path"], schema="main")
    for table, column in (
        ("occurrences", "source_id"),
        ("observations", "occurrence_id"),
        ("verifications", "occurrence_id"),
        ("imports", "source_occurrence_id"),
        ("archive_copies", "local_occurrence_id"),
    ):
        op.create_index(f"ix_history_{table}_{column}", f"history_{table}", [column], schema="main")
