"""initial schema — pgvector extension plus the six core tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-added: autogenerate diffs tables, and an extension is not one.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "author",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_author")),
        sa.UniqueConstraint("name", name=op.f("uq_author_name")),
    )
    op.create_table(
        "category",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_category")),
        sa.UniqueConstraint("code", name=op.f("uq_category_code")),
    )
    op.create_table(
        "ingest_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("categories_key", sa.Text(), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_state")),
        sa.UniqueConstraint("categories_key", name=op.f("uq_ingest_state_categories_key")),
    )
    op.create_table(
        "paper",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("arxiv_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("primary_category", sa.String(length=64), nullable=False),
        sa.Column("published", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("doi", sa.String(length=255), nullable=True),
        sa.Column("journal_ref", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_paper")),
        sa.UniqueConstraint("arxiv_id", name=op.f("uq_paper_arxiv_id")),
    )
    op.create_table(
        "paper_author",
        sa.Column("paper_id", sa.Integer(), nullable=False),
        sa.Column("author_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["author.id"],
            name=op.f("fk_paper_author_author_id_author"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_paper_author_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_id", "author_id", name=op.f("pk_paper_author")),
    )
    op.create_table(
        "paper_category",
        sa.Column("paper_id", sa.Integer(), nullable=False),
        sa.Column("category_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
            name=op.f("fk_paper_category_category_id_category"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["paper_id"],
            ["paper.id"],
            name=op.f("fk_paper_category_paper_id_paper"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_id", "category_id", name=op.f("pk_paper_category")),
    )


def downgrade() -> None:
    # The extension is left in place: it is database-wide, so dropping it would reach
    # further than this migration ever did.
    op.drop_table("paper_category")
    op.drop_table("paper_author")
    op.drop_table("paper")
    op.drop_table("ingest_state")
    op.drop_table("category")
    op.drop_table("author")
