import logging
import re
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

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


logger = logging.getLogger(__name__)


QUANTITY_PATTERN = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)"
    r"\s*"
    r"(?P<unit>"
    r"kg|kgs|g|gr|гр|л|l|lt|ml|мл|кг|г"
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
    "продукт",
    "product",
}


GENERIC_BRANDS = {
    "",
    "unknown",
    "no brand",
    "без бренда",
    "бренд не указан",
    "не указан",
}


def clean_text(
    value: Any,
) -> str:
    """
    Убирает лишние пробелы.
    """

    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def normalized(
    value: Any,
) -> str:
    """
    Простая нормализация
    для внутренних сравнений.
    """

    return (
        clean_text(value)
        .lower()
        .replace("ё", "е")
    )


def humanize_tag(
    value: str | None,
) -> str:
    """
    Делает технический tag OpenFoodFacts
    немного более человеческим.

    Например:

        en:poetti -> Poetti
        en:instant-coffee -> Instant coffee
    """

    cleaned = clean_text(
        value
    )

    if not cleaned:
        return ""

    if ":" in cleaned:
        prefix, remainder = (
            cleaned.split(
                ":",
                1,
            )
        )

        if (
            len(prefix) <= 3
            and remainder
        ):
            cleaned = remainder

    cleaned = cleaned.replace(
        "-",
        " ",
    )

    cleaned = " ".join(
        cleaned.split()
    )

    if not cleaned:
        return ""

    return (
        cleaned[0].upper()
        + cleaned[1:]
    )


def unique_values(
    values: Iterable[Any],
) -> list[str]:
    """
    Убирает пустые значения и дубли,
    сохраняя порядок.
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


def is_generic_name(
    value: str | None,
) -> bool:
    """
    Проверяет слишком общее название.
    """

    return (
        normalized(value)
        in GENERIC_NAMES
    )


def is_real_brand(
    value: str | None,
) -> bool:
    """
    Проверяет, что бренд содержательный.
    """

    normalized_value = normalized(
        value
    )

    if not normalized_value:
        return False

    return (
        normalized_value
        not in GENERIC_BRANDS
    )


def parse_quantity(
    quantity: str | None,
) -> tuple[
    Decimal | None,
    str | None,
]:
    """
    Парсит строковую упаковку.

    Примеры:

        250 g
        95 gr
        1 l
        900 мл
        0.5 kg
    """

    if not quantity:
        return None, None

    match = QUANTITY_PATTERN.search(
        clean_text(
            quantity
        )
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

    if value <= 0:
        return None, None

    raw_unit = (
        match.group(
            "unit"
        )
        .lower()
        .strip()
    )

    unit_map = {
        "g": "г",
        "gr": "г",
        "гр": "г",
        "г": "г",

        "kg": "кг",
        "kgs": "кг",
        "кг": "кг",

        "ml": "мл",
        "мл": "мл",

        "l": "л",
        "lt": "л",
        "л": "л",
    }

    unit = unit_map.get(
        raw_unit
    )

    if not unit:
        return None, None

    return value, unit


def parse_structured_quantity(
    product: OpenFoodFactsProduct,
) -> tuple[
    Decimal | None,
    str | None,
]:
    """
    Определяет упаковку.

    Приоритет:

    1. product_quantity +
       product_quantity_unit;
    2. quantity;
    3. serving_size.
    """

    raw_value = clean_text(
        product.product_quantity
    )

    raw_unit = clean_text(
        product.product_quantity_unit
    )

    if raw_value and raw_unit:
        try:
            value = Decimal(
                raw_value.replace(
                    ",",
                    ".",
                )
            )
        except Exception:
            value = None

        unit_aliases = {
            "g": "г",
            "gram": "г",
            "grams": "г",
            "гр": "г",
            "г": "г",

            "kg": "кг",
            "kgs": "кг",
            "kilogram": "кг",
            "kilograms": "кг",
            "кг": "кг",

            "ml": "мл",
            "milliliter": "мл",
            "milliliters": "мл",
            "мл": "мл",

            "l": "л",
            "lt": "л",
            "liter": "л",
            "liters": "л",
            "л": "л",
        }

        unit = unit_aliases.get(
            normalized(
                raw_unit
            )
        )

        if (
            value is not None
            and value > 0
            and unit
        ):
            return value, unit

    quantity_result = parse_quantity(
        product.quantity
    )

    if (
        quantity_result[0]
        is not None
    ):
        return quantity_result

    return parse_quantity(
        product.serving_size
    )


def choose_brand(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает бренд.

    Приоритет:

    1. brands;
    2. brands_tags.
    """

    brands = clean_text(
        product.brands
    )

    if brands:
        candidates = re.split(
            r"[,;/|]",
            brands,
        )

        for value in candidates:
            candidate = clean_text(
                value
            )

            if is_real_brand(
                candidate
            ):
                return candidate

    for raw_tag in product.brands_tags:
        candidate = humanize_tag(
            raw_tag
        )

        if is_real_brand(
            candidate
        ):
            return candidate

    return None


def get_raw_names(
    product: OpenFoodFactsProduct,
) -> list[str]:
    """
    Собирает возможные названия товара
    из нормализованных и сырых полей OFF.
    """

    values: list[Any] = [
        product.product_name_ru,
        product.product_name,
        product.product_name_en,
        product.abbreviated_product_name,
        product.generic_name_ru,
        product.generic_name,
        product.generic_name_en,
    ]

    raw = product.raw

    if isinstance(
        raw,
        dict,
    ):
        preferred_keys = (
            "product_name_ru",
            "product_name",
            "product_name_en",
            "abbreviated_product_name",
            "generic_name_ru",
            "generic_name",
            "generic_name_en",
        )

        for key in preferred_keys:
            values.append(
                raw.get(
                    key
                )
            )

        for key, value in raw.items():
            if (
                key.startswith(
                    "product_name_"
                )
                or key.startswith(
                    "generic_name_"
                )
            ):
                values.append(
                    value
                )

    return unique_values(
        values
    )


def choose_specific_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает наиболее содержательное
    название товара.

    Например:

        Leggenda Original

    предпочтительнее, чем:

        Кофе
    """

    names = get_raw_names(
        product
    )

    if not names:
        return None

    non_generic = [
        value
        for value in names
        if not is_generic_name(
            value
        )
    ]

    if not non_generic:
        return names[0]

    non_generic.sort(
        key=lambda value: (
            len(
                value.split()
            ),
            len(
                value
            ),
        ),
        reverse=True,
    )

    return non_generic[0]


def category_text(
    product: OpenFoodFactsProduct,
) -> str:
    """
    Собирает категории OFF
    в одну нормализованную строку.
    """

    values = (
        *product.categories,
        *product.categories_tags,
        *product.categories_tags_ru,
        *product.categories_tags_en,
    )

    return normalized(
        " ".join(
            clean_text(
                value
            )
            for value in values
            if clean_text(
                value
            )
        )
    )


def choose_product_kind(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Определяет базовый вид продукта
    по категориям.
    """

    text = category_text(
        product
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
        (
            "Сыр",
            (
                "cheese",
                "сыр",
            ),
        ),
        (
            "Йогурт",
            (
                "yogurt",
                "yoghurt",
                "йогурт",
            ),
        ),
    )

    for title, terms in rules:
        if any(
            term in text
            for term in terms
        ):
            return title

    return None


def format_package_for_name(
    *,
    value: Decimal | None,
    unit: str | None,
) -> str | None:
    """
    Формирует короткий размер упаковки
    для названия товара.
    """

    if (
        value is None
        or not unit
    ):
        return None

    if (
        value
        == value.to_integral()
    ):
        number = str(
            int(
                value
            )
        )
    else:
        number = format(
            value.normalize(),
            "f",
        )

    return (
        f"{number} {unit}"
    )


def build_informative_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Формирует информативное имя.

    Например:

        Кофе + Poetti
        ->
        Кофе Poetti

        Leggenda Original + Poetti
        ->
        Poetti Leggenda Original

        Кофе + Poetti + 250 г
        ->
        Кофе Poetti 250 г
    """

    specific_name = (
        choose_specific_name(
            product
        )
    )

    product_kind = (
        choose_product_kind(
            product
        )
    )

    brand = choose_brand(
        product
    )

    package_value, package_unit = (
        parse_structured_quantity(
            product
        )
    )

    package_text = (
        format_package_for_name(
            value=package_value,
            unit=package_unit,
        )
    )

    base_name = specific_name

    if not base_name:
        base_name = product_kind

    if not base_name:
        return None

    parts: list[str] = []

    if (
        brand
        and not is_generic_name(
            base_name
        )
        and normalized(
            brand
        )
        not in normalized(
            base_name
        )
    ):
        parts.append(
            brand
        )

    parts.append(
        base_name
    )

    if (
        brand
        and is_generic_name(
            base_name
        )
        and normalized(
            brand
        )
        not in normalized(
            " ".join(
                parts
            )
        )
    ):
        parts.append(
            brand
        )

    if (
        package_text
        and normalized(
            package_text
        )
        not in normalized(
            " ".join(
                parts
            )
        )
    ):
        parts.append(
            package_text
        )

    return " ".join(
        unique_values(
            parts
        )
    )


def choose_name(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает итоговое имя товара
    для MarkaRadar.
    """

    informative = (
        build_informative_name(
            product
        )
    )

    if informative:
        return informative

    return choose_specific_name(
        product
    )


def build_keywords(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Собирает поисковые признаки.
    """

    values: list[str] = []

    source_values = (
        *product.categories,
        *product.categories_tags,
        *product.categories_tags_ru,
        *product.categories_tags_en,
        *product.labels,
        *product.labels_tags,
        *product.packaging_tags,
        *product.countries_tags,
    )

    for raw_value in source_values:
        value = humanize_tag(
            raw_value
        )

        if value:
            values.append(
                value
            )

    brand = choose_brand(
        product
    )

    if brand:
        values.append(
            brand
        )

    product_kind = (
        choose_product_kind(
            product
        )
    )

    if product_kind:
        values.append(
            product_kind
        )

    values = unique_values(
        values
    )

    if not values:
        return None

    return ", ".join(
        values
    )


def build_description(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Выбирает описание.
    """

    candidates = (
        product.generic_name_ru,
        product.generic_name,
        product.generic_name_en,
        product.ingredients_text,
    )

    for value in candidates:
        cleaned = clean_text(
            value
        )

        if (
            cleaned
            and not is_generic_name(
                cleaned
            )
        ):
            return cleaned

    return None


def build_subtype(
    product: OpenFoodFactsProduct,
) -> str | None:
    """
    Определяет полезный подтип товара.
    """

    text = category_text(
        product
    )

    rules = (
        (
            "Растворимый",
            (
                "instant coffee",
                "soluble coffee",
                "растворим",
            ),
        ),
        (
            "Молотый",
            (
                "ground coffee",
                "ground coffees",
                "молот",
            ),
        ),
        (
            "В зёрнах",
            (
                "coffee beans",
                "whole bean",
                "whole beans",
                "зернах",
                "зёрнах",
            ),
        ),
        (
            "Без кофеина",
            (
                "decaf",
                "decaffeinated",
                "без кофеина",
            ),
        ),
        (
            "Пастеризованное",
            (
                "pasteurized milk",
                "pasteurised milk",
                "пастеризован",
            ),
        ),
        (
            "Ультрапастеризованное",
            (
                "uht milk",
                "ультрапастеризован",
            ),
        ),
    )

    for title, terms in rules:
        if any(
            term in text
            for term in terms
        ):
            return title

    return None


def log_openfoodfacts_debug(
    *,
    product: OpenFoodFactsProduct,
    chosen_name: str | None,
    chosen_brand: str | None,
    category_id: int | None,
    package_value: Decimal | None,
    package_unit: str | None,
    subtype: str | None,
    description: str | None,
    image_url: str | None,
) -> None:
    """
    Временная диагностика OpenFoodFacts.

    После исправления импорта этот лог
    можно удалить или перевести на DEBUG.
    """

    logger.warning(
        "OFF DEBUG "
        "barcode=%s | "
        "product_name=%r | "
        "product_name_ru=%r | "
        "product_name_en=%r | "
        "abbreviated=%r | "
        "generic_name=%r | "
        "generic_name_ru=%r | "
        "generic_name_en=%r | "
        "brands=%r | "
        "brands_tags=%r | "
        "quantity=%r | "
        "product_quantity=%r | "
        "product_quantity_unit=%r | "
        "serving_size=%r | "
        "categories=%r | "
        "categories_tags=%r | "
        "categories_tags_ru=%r | "
        "categories_tags_en=%r | "
        "chosen_name=%r | "
        "chosen_brand=%r | "
        "chosen_category_id=%r | "
        "chosen_package_value=%r | "
        "chosen_package_unit=%r | "
        "chosen_subtype=%r | "
        "chosen_description=%r | "
        "image=%r",
        product.barcode,
        product.product_name,
        product.product_name_ru,
        product.product_name_en,
        product.abbreviated_product_name,
        product.generic_name,
        product.generic_name_ru,
        product.generic_name_en,
        product.brands,
        product.brands_tags,
        product.quantity,
        product.product_quantity,
        product.product_quantity_unit,
        product.serving_size,
        product.categories,
        product.categories_tags,
        product.categories_tags_ru,
        product.categories_tags_en,
        chosen_name,
        chosen_brand,
        category_id,
        package_value,
        package_unit,
        subtype,
        description,
        image_url,
    )


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
        OpenFoodFacts
          ↓
        нормализация
          ↓
        Category Mapper
          ↓
        Product Merge Engine
          ↓
        MarkaRadar
    """

    client = OpenFoodFactsClient()

    external_product = (
        await client.get_product(
            barcode
        )
    )

    if external_product is None:
        logger.info(
            "OpenFoodFacts не нашёл "
            "штрихкод %s",
            barcode,
        )

        return None

    product_name = choose_name(
        external_product
    )

    if not product_name:
        logger.warning(
            "OFF DEBUG barcode=%s: "
            "не удалось определить название",
            barcode,
        )

        return None

    #
    # BRAND
    #

    brand_name = choose_brand(
        external_product
    )

    #
    # CATEGORY
    #

    category_values = unique_values(
        (
            *external_product.categories,
            *external_product.categories_tags,
            *external_product.categories_tags_ru,
            *external_product.categories_tags_en,
        )
    )

    category_mapping = (
        await map_external_category(
            session=session,
            categories=category_values,
        )
    )

    category_id: int | None = None

    if (
        category_mapping.category
        is not None
    ):
        category_id = int(
            category_mapping.category.id
        )

    #
    # PACKAGE
    #

    package_value, package_unit = (
        parse_structured_quantity(
            external_product
        )
    )

    #
    # SUBTYPE
    #

    subtype = build_subtype(
        external_product
    )

    #
    # DESCRIPTION
    #

    description = (
        build_description(
            external_product
        )
    )

    #
    # IMAGE
    #

    image_url = (
        external_product.image_front_url
        or external_product.image_url
        or external_product.image_front_small_url
    )

    #
    # KEYWORDS
    #

    keywords = build_keywords(
        external_product
    )

    #
    # ДИАГНОСТИКА
    #

    log_openfoodfacts_debug(
        product=external_product,
        chosen_name=product_name,
        chosen_brand=brand_name,
        category_id=category_id,
        package_value=package_value,
        package_unit=package_unit,
        subtype=subtype,
        description=description,
        image_url=image_url,
    )

    #
    # MERGE
    #

    incoming = ExternalProductData(
        source="openfoodfacts",
        name=product_name,
        brand_name=brand_name,
        barcode=external_product.barcode,
        category_id=category_id,
        package_value=package_value,
        package_unit=package_unit,
        subtype=subtype,
        description=description,
        image_url=image_url,
        keywords=keywords,
    )

    merge_result = (
        await merge_external_product(
            session=session,
            incoming=incoming,
            commit=commit,
        )
    )

    logger.warning(
        "OFF MERGE DEBUG "
        "barcode=%s | "
        "product_id=%s | "
        "created=%s | "
        "match=%s | "
        "updated_fields=%s | "
        "final_name=%r | "
        "final_brand=%r | "
        "category_id=%r | "
        "package_value=%r | "
        "package_unit=%r",
        external_product.barcode,
        merge_result.product.id,
        merge_result.created,
        merge_result.match_type,
        merge_result.updated_fields,
        merge_result.product.name,
        merge_result.brand.name,
        merge_result.product.category_id,
        merge_result.product.package_value,
        merge_result.product.package_unit,
    )

    return merge_result
