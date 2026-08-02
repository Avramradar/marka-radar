from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_alias import ProductAlias
from app.utils.text import normalize_text


async def search_products(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[tuple[Product, Brand, Category]]:
    normalized_query = normalize_text(query)
    search_pattern = f"%{normalized_query}%"

    alias = aliased(ProductAlias)

    statement = (
        select(Product, Brand, Category)
        .join(Brand, Product.brand_id == Brand.id)
        .join(Category, Product.category_id == Category.id)
        .outerjoin(alias, alias.product_id == Product.id)
        .where(
            Product.is_active.is_(True),
            or_(
                Product.normalized_name.ilike(search_pattern),
                Brand.normalized_name.ilike(search_pattern),
                Brand.aliases.ilike(search_pattern),
                Category.normalized_name.ilike(search_pattern),
                Product.keywords.ilike(search_pattern),
                Product.barcode == query.strip(),
                alias.normalized_alias.ilike(search_pattern),
            ),
        )
        .distinct()
        .limit(limit)
    )

    result = await session.execute(statement)

    return list(result.all())
