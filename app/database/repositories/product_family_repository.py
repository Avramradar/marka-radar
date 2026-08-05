from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_family import ProductFamily
from app.services.product_family_service import (
    build_product_family_name,
)
from app.utils.text import normalize_text


async def get_or_create_product_family(
    *,
    session: AsyncSession,
    product_name: str,
    brand_name: str,
    category: Category,
    subtype: str | None = None,
) -> ProductFamily | None:
    """
    Находит существующее семейство товара
    или создаёт новое.

    Семейство определяется по:
    - названию товара;
    - бренду;
    - категории;
    - подтипу.

    Пример:

    VICI Сельдь филе в масле 240 г
    ->
    Сельдь филе в масле
    """

    family_name = build_product_family_name(
        product_name=product_name,
        brand_name=brand_name,
        category_name=category.name,
        subtype=subtype,
    ) 

    normalized_family_name = normalize_text(
        family_name
    )

    if not normalized_family_name:
        return None

    statement = select(
        ProductFamily
    ).where(
        ProductFamily.normalized_name
        == normalized_family_name,
        ProductFamily.category_id
        == category.id,
    )

    result = await session.execute(
        statement
    )

    family = result.scalar_one_or_none()

    if family is not None:
        return family

    display_name = " ".join(
        word.capitalize()
        if index == 0
        else word
        for index, word in enumerate(
            normalized_family_name.split()
        )
    )

    family = ProductFamily(
        name=display_name,
        normalized_name=normalized_family_name,
        category_id=category.id,
    )

    session.add(family)
    await session.flush()

    return family


async def assign_product_family(
    *,
    session: AsyncSession,
    product: Product,
    brand_name: str,
    category: Category,
) -> ProductFamily | None:
    """
    Определяет семейство товара
    и сохраняет его в product.family_id.
    """

    family = await get_or_create_product_family(
        session=session,
        product_name=product.name,
        brand_name=brand_name,
        category=category,
        subtype=product.subtype,
    )

    if family is None:
        product.family_id = None
        return None

    product.family_id = family.id

    return family
