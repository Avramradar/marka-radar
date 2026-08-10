"""Add product sources. Revision ID: 0005_add_product_sources Revises: 0004_add_product_families Create Date: 2026-08-10 """

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0005_add_product_sources"
down_revision: str | None = "0004_add_product_families"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "product_sources",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "product_id",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "provider",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(length=255),
            nullable=False,
        ),
        sa.Column(
            "source_url",
            sa.String(length=2048),
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
            ["product_id"],
            ["products.id"],
            name="fk_product_sources_product_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "provider",
            "source_id",
            name=(
                "uq_product_sources_"
                "provider_source_id"
            ),
        ),
    )

    op.create_index(
        "ix_product_sources_product_id",
        "product_sources",
        ["product_id"],
        unique=False,
    )

    op.create_index(
        "ix_product_sources_provider",
        "product_sources",
        ["provider"],
        unique=False,
    )

    op.create_index(
        "ix_product_sources_source_id",
        "product_sources",
        ["source_id"],
        unique=False,
    )

    op.create_index(
        "ix_product_sources_updated_at",
        "product_sources",
        ["updated_at"],
        unique=False,
    )

    op.create_index(
        "ix_product_sources_product_provider",
        "product_sources",
        [
            "product_id",
            "provider",
        ],
        unique=False,
    )

    op.create_index(
        "ix_product_sources_provider_source",
        "product_sources",
        [
            "provider",
            "source_id",
        ],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_product_sources_provider_source",
        table_name="product_sources",
    )

    op.drop_index(
        "ix_product_sources_product_provider",
        table_name="product_sources",
    )

    op.drop_index(
        "ix_product_sources_updated_at",
        table_name="product_sources",
    )

    op.drop_index(
        "ix_product_sources_source_id",
        table_name="product_sources",
    )

    op.drop_index(
        "ix_product_sources_provider",
        table_name="product_sources",
    )

    op.drop_index(
        "ix_product_sources_product_id",
        table_name="product_sources",
    )

    op.drop_table(
        "product_sources"
    )
