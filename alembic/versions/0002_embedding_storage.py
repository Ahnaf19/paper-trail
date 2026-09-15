"""embedding storage — one vector per paper, plus what makes it stale."""

from collections.abc import Sequence

import sqlalchemy as sa

# Autogenerate emits `pgvector.sqlalchemy.vector.VECTOR(...)` but never this import,
# so the generated file raises NameError. Custom column types are its known blind spot.
from pgvector.sqlalchemy import Vector

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding",
        sa.Column("paper_id", sa.Integer(), nullable=False),
        sa.Column("vector", Vector(dim=384), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"], ["paper.id"], name=op.f("fk_embedding_paper_id_paper"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("paper_id", name=op.f("pk_embedding")),
    )
    # No ANN index, deliberately: at a few thousand rows an exact scan is sub-millisecond
    # and an index would be measurable overhead. Revisit past ~100k vectors (PRD §6.1).


def downgrade() -> None:
    op.drop_table("embedding")
