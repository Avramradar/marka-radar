import re
from decimal import Decimal
from typing import Iterable

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
    r"kg|g|gr|гр|л|l|ml|мл|кг|г"
    r")",
    re.IGNORECASE,
)


GENERIC_NAMES = {
    "кофе",
    "coffee",
    "молоко",
    "milk",
    "пицца",
    "pizza",
    "чай",
    "tea",
    "вода",
    "water",
    "сыр",
    "cheese",
    "масло",
    "butter",
    "йогурт",
    "yogurt",
    "yoghurt",
    "кефир",
    "kefir",
    "сельдь",
    "herring",
}


def clean_text(
    value: str | None,
) -> str:
    """
    Убирает лишние пробелы.
    """

    return " ".join(
        str(
            value or ""
        )
        .strip()
        .split()
    )


def normalized(
    value: str | None,
) -> str:
    """
    Простая нормализация для сравнений.
    """

    return (
        clean_text(
            value
        )
        .lower()
        .replace(
            "ё",
            "е",
        )
    )


def is_generic_name(
    value: str | None,
) -> bool:
    """
    Проверяет слишком общее название товара.
    """

    return (
        normalized(
            value
        )
        in GENERIC_NAMES
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
        250 гр

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
        "гр": "г",
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


def unique_values(
    values: Iterable[str],
) -> list[str]:
    """
    Удаляет пустые значения и дубли,
    сохраняя исходный порядок.
    """

    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        cleaned = clean_text(
            value
        )

        if not cleaned:
            continue

        key = normalized(
            cleaned
        )

        if not key:
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            cleaned
        )

    return result


def build_keywords(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Использует категории и labels
    как дополнительные поисковые признаки.
    """

    values = unique_values(
        (
            *product.categories,
            *product.categories_tags,
            *product.labels,
        )
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

    Берём первый содержательный бренд.
    """

    brands = clean_text(
        product.brands
    )

    if not brands:
        return None

    candidates = [
        clean_text(
            value
        )
        for value in re.split(
            r"[,;/|]",
            brands,
        )
    ]

    for candidate in candidates:
        if candidate:
            return candidate

    return None


def choose_base_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает базовое название товара.
    """

    candidates = (
        product.product_name,
        product.generic_name,
    )

    for candidate in candidates:
        cleaned = clean_text(
            candidate
        )

        if cleaned:
            return cleaned

    return None


def choose_product_kind(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Пытается определить тип продукта
    из категорий.

    Используется только как fallback,
    если название слишком общее.
    """

    category_text = " ".join(
        (
            *product.categories,
            *product.categories_tags,
        )
    )

    normalized_categories = normalized(
        category_text
    )

    rules = (
        (
            "Кофе",
            (
                "coffee",
                "кофе",
            ),
        ),
        (
            "Молоко",
            (
                "milk",
                "молоко",
            ),
        ),
        (
            "Пицца",
            (
                "pizza",
                "пицца",
            ),
        ),
        (
            "Чай",
            (
                "tea",
                "чай",
            ),
        ),
        (
            "Сельдь",
            (
                "herring",
                "сельдь",
                "селед",
            ),
        ),
    )

    for title, terms in rules:
        if any(
            term
            in normalized_categories
            for term in terms
        ):
            return title

    return None


def build_informative_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Строит более полезное имя товара.

    Если OFF отдал просто:

        Кофе

    но есть бренд:

        Poetti

    получим хотя бы:

        Кофе Poetti

    Если есть упаковка:

        Кофе Poetti 250 г

    Это лучше, чем оставлять десятки
    одинаковых карточек "Кофе".
    """

    base_name = choose_base_name(
        product
    )

    brand = choose_brand(
        product
    )

    package_value, package_unit = (
        parse_quantity(
            product.quantity
        )
    )

    product_kind = choose_product_kind(
        product
    )

    if not base_name:
        base_name = product_kind

    if not base_name:
        return None

    parts: list[str] = [
        clean_text(
            base_name
        )
    ]

    current_text = normalized(
        " ".join(parts)
    )

    if (
        brand
        and normalized(brand)
        not in current_text
    ):
        parts.append(
            brand
        )

    if (
        package_value is not None
        and package_unit
    ):
        package_text = (
            f"{package_value.normalize()} "
            f"{package_unit}"
        )

        if (
            normalized(package_text)
            not in normalized(
                " ".join(parts)
            )
        ):
            parts.append(
                package_text
            )

    return " ".join(
        part
        for part in parts
        if part
    )


def choose_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает итоговое имя для MarkaRadar.

    Если название уже хорошее —
    сохраняем его.

    Если оно слишком общее —
    пытаемся обогатить его брендом
    и упаковкой.
    """

    base_name = choose_base_name(
        product
    )

    if not base_name:
        return build_informative_name(
            product
        )

    if not is_generic_name(
        base_name
    ):
        return base_name

    informative_name = (
        build_informative_name(
            product
        )
    )

    if (
        informative_name
        and normalized(
            informative_name
        )
        != normalized(
            base_name
        )
    ):
        return informative_name

    return base_name


def build_description(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Формирует описание товара.

    Состав используем только если
    другого полезного описания нет.
    """

    generic_name = clean_text(
        product.generic_name
    )

    ingredients = clean_text(
        product.ingredients_text
    )

    if (
        generic_name
        and not is_generic_name(
            generic_name
        )
    ):
        return generic_name

    if ingredients:
        return ingredients

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

    category_values = unique_values(
        (
            *external_product.categories,
            *external_product.categories_tags,
        )
    )

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
        description=build_description(
            external_product
        ),
        image_url=image_url,
        keywords=build_keywords(
            external_product
        ),
    )

    return await merge_external_product(
        session=session,
        incoming=incoming,
        commit=commit,
    )
