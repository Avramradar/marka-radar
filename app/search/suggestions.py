from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.product import Product
from app.utils.text import (
    build_search_variants,
    normalize_text,
)


async def get_search_suggestions(
    session: AsyncSession,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Возвращает подсказки поиска.

    Используется при вводе пользователем
    первых букв товара.

    Пример:

        бар

    →

        Barilla Spaghetti
        Barilla Penne
        Barilla Fusilli
    """

    normalized = normalize_text(query)

    if len(normalized) < 2:
        return []

    variants = build_search_variants(query)

    suggestions: dict[int, dict] = {}

    for variant in variants:

        statement = (
            select(
                Product.id,
                Product.name,
                Brand.name.label("brand"),
                func.greatest(
                    func.word_similarity(
                        variant,
                        Product.search_text,
                    ),
                    func.similarity(
                        Product.search_text,
                        variant,
                    ),
                ).label("score"),
            )
            .join(
                Brand,
                Product.brand_id == Brand.id,
            )
            .where(
                Product.is_active.is_(True),
                Product.search_text.is_not(None),
                func.word_similarity(
                    variant,
                    Product.search_text,
                )
                >= 0.30,
            )
            .order_by(
                func.greatest(
                    func.word_similarity(
                        variant,
                        Product.search_text,
                    ),
                    func.similarity(
                        Product.search_text,
                        variant,
                    ),
                ).desc(),
                Brand.name,
                Product.name,
            )
            .limit(limit)
        )

        result = await session.execute(statement)

        for row in result:

            if row.id in suggestions:
                continue

            suggestions[row.id] = {
                "product_id": row.id,
                "name": row.name,
                "brand": row.brand,
                "score": float(row.score),
            }

    return sorted(
        suggestions.values(),
        key=lambda item: item["score"],
        reverse=True,
    )[:limit]
