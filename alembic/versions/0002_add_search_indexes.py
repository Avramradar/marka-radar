"""Add trigram search indexes.

Revision ID: 0002_add_search_indexes
Revises: 0001_initial_schema
Create Date: 2026-08-04
"""

from collections.abc import Sequence

from alembic import op


revision: str = "0002_add_search_indexes"
down_revision: str | None = "0001_initial_schema"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # Расширение для поиска похожих слов и опечаток.
    op.execute(
        "CREATE EXTENSION IF NOT EXISTS pg_trgm"
    )

    # Поиск по названию товара.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_products_normalized_name_trgm
        ON products
        USING gin (normalized_name gin_trgm_ops)
        """
    )

    # Поиск по ключевым словам товара.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_products_keywords_trgm
        ON products
        USING gin (keywords gin_trgm_ops)
        """
    )

    # Поиск по бренду.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_brands_normalized_name_trgm
        ON brands
        USING gin (normalized_name gin_trgm_ops)
        """
    )

    # Поиск по альтернативным названиям бренда.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_brands_aliases_trgm
        ON brands
        USING gin (aliases gin_trgm_ops)
        """
    )

    # Поиск по категории.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_categories_normalized_name_trgm
        ON categories
        USING gin (normalized_name gin_trgm_ops)
        """
    )

    # Поиск по синонимам товаров.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
        ix_product_aliases_normalized_alias_trgm
        ON product_aliases
        USING gin (normalized_alias gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_product_aliases_normalized_alias_trgm
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_categories_normalized_name_trgm
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_brands_aliases_trgm
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_brands_normalized_name_trgm
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_products_keywords_trgm
        """
    )

    op.execute(
        """
        DROP INDEX IF EXISTS
        ix_products_normalized_name_trgm
        """
    )

    # Само расширение не удаляем:
    # оно может использоваться другими таблицами или индексами.
