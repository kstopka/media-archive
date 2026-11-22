"""Record SORT moves, outstanding cleanup, and the variants of a name conflict."""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    previous = sa.Table("history_occurrences", sa.MetaData(), schema="main", autoload_with=op.get_bind())
    old_kind = next(constraint for constraint in previous.constraints if isinstance(constraint, sa.CheckConstraint))
    previous.constraints.remove(old_kind)
    with op.batch_alter_table("history_occurrences", schema="main", recreate="always", copy_from=previous) as batch:
        batch.create_check_constraint("ck_history_occurrences_kind",
                                      "kind IN ('source', 'staging', 'local', 'archive', 'undated')")
    op.create_table(
        "sort_moves",
        sa.Column("operation_id", sa.Integer, sa.ForeignKey("main.journal_operations.id"), primary_key=True),
        sa.Column("import_id", sa.Integer, sa.ForeignKey("main.history_imports.id"), nullable=False),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("main.history_occurrences.id"), nullable=False),
        sa.Column("verification_id", sa.Integer, sa.ForeignKey("main.history_verifications.id"), unique=True),
        sa.Column("cleanup_done", sa.Boolean, nullable=False, server_default=sa.false()),
        schema="main",
    )
    op.create_index("ix_sort_moves_import", "sort_moves", ["import_id"], schema="main")
    op.create_table(
        "sort_conflicts",
        sa.Column("group_path", sa.Text, primary_key=True),
        sa.Column("original_name", sa.Text, nullable=False),
        schema="main",
    )
    op.create_table(
        "sort_conflict_members",
        sa.Column("group_path", sa.Text, sa.ForeignKey("main.sort_conflicts.group_path"), primary_key=True),
        sa.Column("content_id", sa.Integer, sa.ForeignKey("main.history_contents.id"), primary_key=True),
        sa.Column("volume_id", sa.Integer, sa.ForeignKey("main.registry_volume_episodes.id"), primary_key=True),
        sa.Column("path", sa.Text, primary_key=True),
        schema="main",
    )
