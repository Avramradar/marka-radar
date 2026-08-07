import re
from decimal import Decimal

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.integrations.openfoodfacts_client import (
    OpenFoodFactsClient,
    OpenFoodFactsProduct,
)
from app.services.category_mapper import (
    map_external_category,
)
from app.services.product_merge_service import (
    ExternalProductData,
    ProductMergeResult,
    merge_external_product,
)


QUANTITY_PATTERN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)"
    r"\s*"
    r"(?P<unit>"
    r"kg|g|gr|л|l|ml|мл|кг|г"
    r")",
    re.IGNORECASE,
)


def parse_quantity(
    quantity: str | None,
) -> tuple[
    Decimal | None,
    str | None,
]:
    """
    Преобразует:

        95 g
        1 l
        0.5 L
        900 мл

    в package_value/package_unit.
    """

    if not quantity:
        return None, None

    match = QUANTITY_PATTERN.search(
        quantity
    )

    if match is None:
        return None, None

    raw_value = (
        match.group(
            "value"
        )
        .replace(
            ",",
            ".",
        )
    )

    try:
        value = Decimal(
            raw_value
        )
    except Exception:
        return None, None

    raw_unit = (
        match.group(
            "unit"
        )
        .strip()
        .lower()
    )

    unit_map = {
        "g": "г",
        "gr": "г",
        "г": "г",
        "kg": "кг",
        "кг": "кг",
        "ml": "мл",
        "мл": "мл",
        "l": "л",
        "л": "л",
    }

    unit = unit_map.get(
        raw_unit
    )

    return value, unit


def build_keywords(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Использует категории и labels
    как дополнительные поисковые признаки.
    """

    values: list[str] = []
    seen: set[str] = set()

    for value in (
        *product.categories,
        *product.categories_tags,
        *product.labels,
    ):
        cleaned = " ".join(
            str(value).strip().split()
        )

        if not cleaned:
            continue

        key = cleaned.lower()

        if key in seen:
            continue

        seen.add(
            key
        )

        values.append(
            cleaned
        )

    if not values:
        return None

    return ", ".join(
        values
    )


def choose_brand(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Open Food Facts иногда возвращает
    несколько брендов через запятую.

    На первом этапе берём первый.
    """

    if not product.brands:
        return None

    brand = (
        product.brands
        .split(
            ",",
            1,
        )[0]
        .strip()
    )

    return brand or None


def choose_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает имя для MarkaRadar.
    """

    if product.product_name:
        return product.product_name

    if product.generic_name:
        return product.generic_name

    return None


async def import_openfoodfacts_product(
    *,
    session: AsyncSession,
    barcode: str,
    commit: bool = False,
) -> ProductMergeResult | None:
    """
    Полный импорт товара:

    barcode
      ↓
    Open Food Facts
      ↓
    Category Mapper
      ↓
    Product Merge Engine
      ↓
    MarkaRadar DB
    """

    client = OpenFoodFactsClient()

    external_product = (
        await client.get_product(
            barcode
        )
    )

    if external_product is None:
        return None

    product_name = choose_name(
        external_product
    )

    if not product_name:
        return None

    category_values = [
        *external_product.categories,
        *external_product.categories_tags,
    ]

    category_mapping = (
        await map_external_category(
            session=session,
            categories=category_values,
        )
    )

    category_id = None

    if (
        category_mapping.category
        is not None
    ):
        category_id = int(
            category_mapping.category.id
        )

    package_value, package_unit = (
        parse_quantity(
            external_product.quantity
        )
    )

    image_url = (
        external_product.image_front_url
        or external_product.image_url
    )

    description = (
        external_product.ingredients_text
        or external_product.generic_name
    )

    incoming = ExternalProductData(
        source="openfoodfacts",
        name=product_name,
        brand_name=choose_brand(
            external_product
        ),
        barcode=(
            external_product.barcode
        ),
        category_id=category_id,
        package_value=package_value,
        package_unit=package_unit,
        description=description,
        image_url=image_url,
        keywords=build_keywords(
            external_product
        ),
    )

    # Существующий товар можно дополнить
    # даже если category_id не определён.
    #
    # Новый товар без category_id Merge Engine
    # намеренно не создаст.
    return await merge_external_product(
        session=session,
        incoming=incoming,
        commit=commit,
    )
