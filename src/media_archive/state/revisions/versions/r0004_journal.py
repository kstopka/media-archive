"""Record operation intent, attempts, transitions and a single committed result."""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_operations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("operation_key", sa.Text, nullable=False, unique=True),
        sa.Column("intent", sa.Text, nullable=False),
        sa.Column("target_occurrence_id", sa.Integer, sa.ForeignKey("main.history_occurrences.id"),
                  nullable=False, unique=True),
        schema="main",
    )
    op.create_table(
        "journal_resources",
        sa.Column("operation_id", sa.Integer, sa.ForeignKey("main.journal_operations.id"), primary_key=True),
        sa.Column("kind", sa.Text, primary_key=True),
        sa.Column("resource_id", sa.Text, primary_key=True),
        sa.CheckConstraint("kind IN ('collection', 'archive', 'source')"),
        schema="main",
    )
    op.create_table(
        "journal_attempts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("operation_id", sa.Integer, sa.ForeignKey("main.journal_operations.id"), nullable=False),
        sa.Column("run_id", sa.Text, sa.ForeignKey("main.history_runs.id"), nullable=False),
        sa.Column("owner_token", sa.Text, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("reconciling", sa.Boolean(create_constraint=True), nullable=False),
        sa.Column("verification_floor", sa.Integer, nullable=False),
        sa.Column("observation_floor", sa.Integer, nullable=False),
        sa.Column("verification_id", sa.Integer, sa.ForeignKey("main.history_verifications.id"), unique=True),
        sa.CheckConstraint(
            "state IN ('planned', 'transferring', 'transferred', 'verifying', 'verified', 'published', "
            "'committed', 'interrupted', 'failed', 'conflict', 'needs_review')"
        ),
        sa.CheckConstraint("state NOT IN ('verified', 'published', 'committed') OR verification_id IS NOT NULL"),
        sa.UniqueConstraint("operation_id", "run_id"),
        schema="main",
    )
    op.create_index("ix_journal_attempt_operation", "journal_attempts", ["operation_id"], schema="main")
    op.create_index(
        "uq_journal_committed_operation", "journal_attempts", ["operation_id"], unique=True,
        sqlite_where=sa.text("state = 'committed'"), schema="main",
    )
    op.create_index(
        "uq_journal_active_operation", "journal_attempts", ["operation_id"], unique=True,
        sqlite_where=sa.text("state IN ('planned', 'transferring', 'transferred', 'verifying', 'verified', 'published')"),
        schema="main",
    )
    op.create_table(
        "journal_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("attempt_id", sa.Integer, sa.ForeignKey("main.journal_attempts.id"), nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("recorded_at", sa.Text, nullable=False),
        sa.Column("error_code", sa.Text),
        sa.Column("observation_id", sa.Integer, sa.ForeignKey("main.history_observations.id")),
        schema="main",
    )
    op.create_index("ix_journal_event_attempt", "journal_events", ["attempt_id"], schema="main")
