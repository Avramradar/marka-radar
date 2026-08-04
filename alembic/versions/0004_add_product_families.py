"""Add product families.

Revision ID: 0004_add_product_families
Revises: 0003_add_search_text
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0004_add_product_families"
down_revision: str | None = "0003_add_search_text"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_families",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "normalized_name",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "category_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["categories.id"],
            name="fk_product_families_category_id",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "normalized_name",
            "category_id",
            name=(
                "uq_product_families_"
                "normalized_name_category"
            ),
        ),
    )

    op.create_index(
        "ix_product_families_normalized_name",
        "product_families",
        ["normalized_name"],
        unique=False,
    )

    op.create_index(
        "ix_product_families_category_id",
        "product_families",
        ["category_id"],
        unique=False,
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_product_families_normalized_name_trgm
        ON product_families
        USING gin (
            normalized_name gin_trgm_ops
        )
        """
    )

    op.add_column(
        "products",
        sa.Column(
            "family_id",
            sa.Integer(),
            nullable=True,
        ),
    )

    op.create_foreign_key(
        "fk_products_family_id",
        "products",
        "product_families",
        ["family_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "ix_products_family_id",
        "products",
        ["family_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_products_family_id",
        table_name="products",
    )

    op.drop_constraint(
        "fk_products_family_id",
        "products",
        type_="foreignkey",
    )

    op.drop_column(
        "products",
        "family_id",
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_product_families_normalized_name_trgm
        """
    )

    op.drop_index(
        "ix_product_families_category_id",
        table_name="product_families",
    )

    op.drop_index(
        "ix_product_families_normalized_name",
        table_name="product_families",
    )

    op.drop_table(
        "product_families"
    )
