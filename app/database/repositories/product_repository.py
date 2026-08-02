from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_alias import ProductAlias
from app.utils.text import normalize_text


def build_token_condition(
    token: str,
    alias: type[ProductAlias],
):
    """
    Создаёт условие поиска для одного слова.

    Слово может совпасть с названием товара, брендом,
    категорией, ключевыми словами или синонимом.
    """

    pattern = f"%{token}%"

    return or_(
        Product.normalized_name.ilike(pattern),
        Brand.normalized_name.ilike(pattern),
        Brand.aliases.ilike(pattern),
        Category.normalized_name.ilike(pattern),
        Product.keywords.ilike(pattern),
        Product.subtype.ilike(pattern),
        alias.normalized_alias.ilike(pattern),
    )


async def search_products(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[tuple[Product, Brand, Category]]:
    normalized_query = normalize_text(query)
    raw_query = query.strip()

    if not normalized_query:
        return []

    alias = aliased(ProductAlias)

    # Штрихкод ищем отдельно и точно.
    if raw_query.isdigit():
        barcode_statement = (
            select(Product, Brand, Category)
            .join(
                Brand,
                Product.brand_id == Brand.id,
            )
            .join(
                Category,
                Product.category_id == Category.id,
            )
            .where(
                Product.is_active.is_(True),
                Product.barcode == raw_query,
            )
            .limit(limit)
        )

        barcode_result = await session.execute(
            barcode_statement
        )
        barcode_products = list(barcode_result.all())

        if barcode_products:
            return barcode_products

    tokens = [
        token
        for token in normalized_query.split()
        if token
    ]

    token_conditions = [
        build_token_condition(
            token=token,
            alias=alias,
        )
        for token in tokens
    ]

    full_pattern = f"%{normalized_query}%"

    # Более точные совпадения получают меньший номер
    # и показываются выше остальных результатов.
    relevance_order = case(
        (
            Product.normalized_name == normalized_query,
            0,
        ),
        (
            Brand.normalized_name == normalized_query,
            1,
        ),
        (
            Product.normalized_name.ilike(
                full_pattern
            ),
            2,
        ),
        (
            Brand.normalized_name.ilike(
                full_pattern
            ),
            3,
        ),
        (
            alias.normalized_alias
            == normalized_query,
            4,
        ),
        else_=5,
    )

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id == Brand.id,
        )
        .join(
            Category,
            Product.category_id == Category.id,
        )
        .outerjoin(
            alias,
            alias.product_id == Product.id,
        )
        .where(
            Product.is_active.is_(True),
            and_(*token_conditions),
        )
        .distinct()
        .order_by(
            relevance_order,
            Brand.name,
            Product.name,
        )
        .limit(limit)
    )

    result = await session.execute(statement)

    return list(result.all())
