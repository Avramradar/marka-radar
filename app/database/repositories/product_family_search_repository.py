from typing import TypedDict

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_family import ProductFamily
from app.utils.text import (
    build_search_variants,
    normalize_text,
)


class ProductFamilySearchResult(TypedDict):
    family_id: int
    name: str
    category: str | None
    products_count: int
    score: float


async def search_product_families(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 10,
) -> list[ProductFamilySearchResult]:
    """
    Ищет семейства товаров и возвращает количество
    активных товаров внутри каждого семейства.

    Пример запроса:

        сельдь

    Возможный результат:

        Сельдь филе в масле — 18 товаров
        Сельдь слабосолёная — 11 товаров
        Сельдь по-царски — 6 товаров
    """

    normalized_query = normalize_text(query)

    if len(normalized_query) < 2:
        return []

    safe_limit = max(
        1,
        min(
            limit,
            30,
        ),
    )

    search_variants = build_search_variants(
        query
    )

    if not search_variants:
        return []

    conditions = []

    score_expressions = []

    for variant in search_variants[:4]:
        if len(variant) < 2:
            continue

        contains_pattern = f"%{variant}%"
        prefix_pattern = f"{variant}%"

        conditions.extend(
            [
                ProductFamily.normalized_name.ilike(
                    contains_pattern
                ),
                ProductFamily.normalized_name.op("%")(
                    variant
                ),
                func.word_similarity(
                    variant,
                    ProductFamily.normalized_name,
                )
                >= 0.35,
            ]
        )

        score_expressions.extend(
            [
                func.similarity(
                    ProductFamily.normalized_name,
                    variant,
                ),
                func.word_similarity(
                    variant,
                    ProductFamily.normalized_name,
                ),
                func.coalesce(
                    func.cast(
                        ProductFamily.normalized_name.ilike(
                            prefix_pattern
                        ),
                        ProductFamily.id.type,
                    ),
                    0,
                )
                * 0.30,
            ]
        )

    if not conditions:
        return []

    relevance_score = func.greatest(
        *score_expressions
    )

    statement = (
        select(
            ProductFamily.id.label(
                "family_id"
            ),
            ProductFamily.name.label(
                "family_name"
            ),
            Category.name.label(
                "category_name"
            ),
            func.count(
                Product.id
            ).label(
                "products_count"
            ),
            relevance_score.label(
                "score"
            ),
        )
        .join(
            Product,
            Product.family_id
            == ProductFamily.id,
        )
        .outerjoin(
            Category,
            ProductFamily.category_id
            == Category.id,
        )
        .where(
            Product.is_active.is_(True),
            or_(*conditions),
        )
        .group_by(
            ProductFamily.id,
            ProductFamily.name,
            ProductFamily.normalized_name,
            Category.name,
        )
        .having(
            func.count(Product.id) > 0
        )
        .order_by(
            relevance_score.desc(),
            func.count(Product.id).desc(),
            ProductFamily.name.asc(),
        )
        .limit(
            safe_limit
        )
    )

    result = await session.execute(
        statement
    )

    families: list[
        ProductFamilySearchResult
    ] = []

    for row in result:
        families.append(
            {
                "family_id": int(
                    row.family_id
                ),
                "name": str(
                    row.family_name
                ),
                "category": (
                    str(row.category_name)
                    if row.category_name
                    else None
                ),
                "products_count": int(
                    row.products_count
                ),
                "score": float(
                    row.score or 0.0
                ),
            }
        )

    return families


async def get_family_products(
    session: AsyncSession,
    family_id: int,
    *,
    limit: int = 100,
) -> list[tuple[Product, Category]]:
    """
    Возвращает активные товары выбранного семейства.

    Товары сортируются по названию и идентификатору.
    Бренд можно получить через отдельный JOIN
    в обработчике или расширить эту функцию позже.
    """

    safe_limit = max(
        1,
        min(
            limit,
            200,
        ),
    )

    statement = (
        select(
            Product,
            Category,
        )
        .join(
            Category,
            Product.category_id
            == Category.id,
        )
        .where(
            Product.family_id
            == family_id,
            Product.is_active.is_(True),
        )
        .order_by(
            Product.name.asc(),
            Product.id.asc(),
        )
        .limit(
            safe_limit
        )
    )

    result = await session.execute(
        statement
    )

    return list(
        result.all()
    )


async def get_product_family(
    session: AsyncSession,
    family_id: int,
) -> ProductFamily | None:
    """
    Возвращает семейство по идентификатору.
    """

    statement = (
        select(
            ProductFamily
        )
        .where(
            ProductFamily.id
            == family_id
        )
        .limit(1)
    )

    result = await session.execute(
        statement
    )

    return result.scalar_one_or_none()
