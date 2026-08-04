"""Add search_text to products.

Revision ID: 0003_add_search_text
Revises: 0002_add_search_indexes
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0003_add_search_text"
down_revision: str | None = "0002_add_search_indexes"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "search_text",
            sa.Text(),
            nullable=True,
        ),
    )

    op.execute(
        """
        UPDATE products
        SET search_text = CONCAT_WS(
            ' ',
            normalized_name,
            keywords,
            subtype
        )
        WHERE search_text IS NULL
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_products_search_text_trgm
        ON products
        USING gin (search_text gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_products_search_text_trgm
        """
    )

    op.drop_column(
        "products",
        "search_text",
    )
