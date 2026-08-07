from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.utils.text import normalize_text


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


GENERIC_PRODUCT_NAMES = {
    "кофе",
    "молоко",
    "чай",
    "вода",
    "пицца",
    "сыр",
    "масло",
    "йогурт",
    "кефир",
    "сельдь",
    "сок",
}


GENERIC_CATEGORY_NAMES = {
    "",
    "продукты",
    "продукт",
    "еда",
    "food",
    "foods",
    "products",
    "product",
    "прочее",
    "другое",
    "other",
}


class ProductMatchType(StrEnum):
    """
    Каким способом внешний товар
    был сопоставлен с MarkaRadar.
    """

    BARCODE = "barcode"
    BRAND_AND_NAME = "brand_and_name"
    NAME = "name"
    CREATED = "created"


@dataclass(slots=True)
class ExternalProductData:
    """
    Универсальный формат товара
    из любого внешнего источника.
    """

    source: str

    name: str

    brand_name: str | None = None
    barcode: str | None = None

    category_id: int | None = None
    family_id: int | None = None

    package_value: Decimal | float | int | None = None
    package_unit: str | None = None

    subtype: str | None = None
    description: str | None = None
    image_url: str | None = None
    keywords: str | None = None


@dataclass(slots=True)
class ProductMergeResult:
    """
    Результат объединения внешнего товара
    с канонической базой MarkaRadar.
    """

    product: Product
    brand: Brand

    created: bool
    match_type: ProductMatchType

    updated_fields: tuple[str, ...]
    source: str


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
    Нормализация для сравнения.
    """

    cleaned = clean_text(
        value
    )

    if not cleaned:
        return ""

    return (
        normalize_text(
            cleaned
        )
        .replace(
            "ё",
            "е",
        )
    )


def normalize_barcode(
    barcode: str | None,
) -> str | None:
    """
    Нормализует штрихкод.
    """

    if not barcode:
        return None

    digits = "".join(
        char
        for char in str(barcode)
        if char.isdigit()
    )

    if len(digits) < 8:
        return None

    return digits


def normalize_package_unit(
    unit: str | None,
) -> str | None:
    """
    Приводит единицы упаковки
    к единому виду.
    """

    clean_unit = normalized(
        unit
    )

    if not clean_unit:
        return None

    aliases = {
        "гр": "г",
        "грамм": "г",
        "грамма": "г",
        "граммов": "г",
        "gram": "г",
        "grams": "г",
        "g": "г",

        "килограмм": "кг",
        "килограмма": "кг",
        "килограммов": "кг",
        "kg": "кг",

        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитров": "мл",
        "ml": "мл",

        "литр": "л",
        "литра": "л",
        "литров": "л",
        "l": "л",
    }

    return aliases.get(
        clean_unit,
        clean_text(
            unit
        ).lower(),
    )


def normalize_package_value(
    value: Decimal | float | int | None,
) -> Decimal | None:
    """
    Переводит размер упаковки в Decimal.
    """

    if value is None:
        return None

    try:
        decimal_value = Decimal(
            str(value)
        )
    except Exception:
        return None

    if decimal_value <= 0:
        return None

    return decimal_value


def is_unknown_brand(
    brand_name: str | None,
) -> bool:
    """
    Проверяет служебный бренд.
    """

    normalized_name = normalized(
        brand_name
    )

    return (
        not normalized_name
        or normalized_name
        in {
            normalized(item)
            for item in UNKNOWN_BRAND_NAMES
        }
    )


def is_generic_category(
    category_name: str | None,
) -> bool:
    """
    Проверяет слишком общую категорию.

    Например:

        Продукты
        Food
        Другое

    Такую категорию разрешается заменить
    более конкретной.
    """

    return (
        normalized(
            category_name
        )
        in {
            normalized(item)
            for item in GENERIC_CATEGORY_NAMES
        }
    )


def is_generic_product_name(
    name: str | None,
) -> bool:
    """
    Определяет слишком общее название.
    """

    return (
        normalized(
            name
        )
        in {
            normalized(item)
            for item in GENERIC_PRODUCT_NAMES
        }
    )


def is_better_name(
    *,
    current_name: str | None,
    incoming_name: str | None,
) -> bool:
    """
    Решает, можно ли заменить название.

    Например:

        Кофе

    можно заменить на:

        Poetti Leggenda Original
    """

    current = clean_text(
        current_name
    )

    incoming = clean_text(
        incoming_name
    )

    if not incoming:
        return False

    if not current:
        return True

    if (
        normalized(current)
        == normalized(incoming)
    ):
        return False

    if (
        is_generic_product_name(
            current
        )
        and not is_generic_product_name(
            incoming
        )
    ):
        return True

    if (
        is_generic_product_name(
            current
        )
        and len(incoming) > len(current)
    ):
        return True

    if (
        len(current) <= 10
        and len(incoming)
        >= len(current) + 5
    ):
        return True

    return False


def should_fill_text(
    current_value: Any,
    incoming_value: Any,
) -> bool:
    """
    Заполняет пустое поле новыми данными.
    """

    return (
        not clean_text(
            current_value
        )
        and bool(
            clean_text(
                incoming_value
            )
        )
    )


def combine_keywords(
    current_keywords: str | None,
    incoming_keywords: str | None,
) -> str | None:
    """
    Объединяет ключевые слова
    без повторений.
    """

    current = clean_text(
        current_keywords
    )

    incoming = clean_text(
        incoming_keywords
    )

    if not current and not incoming:
        return None

    if not current:
        return incoming

    if not incoming:
        return current

    values: list[str] = []
    seen: set[str] = set()

    for text in (
        current,
        incoming,
    ):
        parts = [
            part.strip()
            for part
            in text.replace(
                ";",
                ",",
            ).split(",")
            if part.strip()
        ]

        if len(parts) == 1:
            parts = [
                text
            ]

        for part in parts:
            key = normalized(
                part
            )

            if not key:
                continue

            if key in seen:
                continue

            seen.add(
                key
            )

            values.append(
                part
            )

    return ", ".join(
        values
    )


def build_search_text(
    *,
    product: Product,
    brand: Brand,
    category: Category | None = None,
) -> str:
    """
    Перестраивает внутренний поисковый текст.
    """

    parts = (
        product.name,
        brand.name,
        (
            category.name
            if category is not None
            else None
        ),
        product.subtype,
        product.keywords,
        product.description,
        product.package_value,
        product.package_unit,
        product.barcode,
    )

    normalized_parts = [
        normalized(
            part
        )
        for part in parts
        if part is not None
    ]

    return " ".join(
        part
        for part in normalized_parts
        if part
    )


async def find_brand(
    *,
    session: AsyncSession,
    brand_name: str,
) -> Brand | None:
    """
    Ищет существующий бренд.
    """

    normalized_name = normalized(
        brand_name
    )

    if not normalized_name:
        return None

    statement = (
        select(
            Brand
        )
        .where(
            or_(
                Brand.normalized_name
                == normalized_name,
                Brand.name.ilike(
                    brand_name
                ),
            )
        )
        .limit(
            1
        )
    )

    result = await session.execute(
        statement
    )

    return result.scalar_one_or_none()


async def get_or_create_brand(
    *,
    session: AsyncSession,
    brand_name: str | None,
) -> Brand:
    """
    Возвращает существующий бренд
    или создаёт новый.
    """

    clean_brand = clean_text(
        brand_name
    )

    if (
        not clean_brand
        or is_unknown_brand(
            clean_brand
        )
    ):
        clean_brand = (
            "Бренд не указан"
        )

    existing = await find_brand(
        session=session,
        brand_name=clean_brand,
    )

    if existing is not None:
        return existing

    brand = Brand(
        name=clean_brand,
        normalized_name=normalized(
            clean_brand
        ),
    )

    session.add(
        brand
    )

    await session.flush()

    return brand


async def get_brand_by_id(
    *,
    session: AsyncSession,
    brand_id: int,
) -> Brand | None:
    """
    Загружает текущий бренд товара.
    """

    result = await session.execute(
        select(
            Brand
        )
        .where(
            Brand.id == brand_id
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def get_category_by_id(
    *,
    session: AsyncSession,
    category_id: int | None,
) -> Category | None:
    """
    Загружает категорию по ID.
    """

    if category_id is None:
        return None

    result = await session.execute(
        select(
            Category
        )
        .where(
            Category.id == category_id
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def find_product_by_barcode(
    *,
    session: AsyncSession,
    barcode: str | None,
) -> Product | None:
    """
    Самое надёжное сопоставление:
    точный штрихкод.
    """

    normalized_barcode = normalize_barcode(
        barcode
    )

    if normalized_barcode is None:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.barcode
            == normalized_barcode
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def find_product_by_brand_and_name(
    *,
    session: AsyncSession,
    brand: Brand,
    name: str,
) -> Product | None:
    """
    Сопоставление по бренду и имени.
    """

    normalized_name = normalized(
        name
    )

    if not normalized_name:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.brand_id
            == brand.id,
            Product.normalized_name
            == normalized_name,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def find_safe_name_match(
    *,
    session: AsyncSession,
    name: str,
) -> Product | None:
    """
    По одному названию объединяем товар
    только при единственном точном совпадении.
    """

    normalized_name = normalized(
        name
    )

    if not normalized_name:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.normalized_name
            == normalized_name,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            2
        )
    )

    products = list(
        result.scalars().all()
    )

    if len(products) != 1:
        return None

    return products[0]


async def find_matching_product(
    *,
    session: AsyncSession,
    incoming: ExternalProductData,
    brand: Brand,
) -> tuple[
    Product | None,
    ProductMatchType | None,
]:
    """
    Ищет существующий товар.

    Приоритет:

    1. barcode;
    2. brand + name;
    3. уникальное точное имя.
    """

    barcode_match = (
        await find_product_by_barcode(
            session=session,
            barcode=incoming.barcode,
        )
    )

    if barcode_match is not None:
        return (
            barcode_match,
            ProductMatchType.BARCODE,
        )

    brand_name_match = (
        await find_product_by_brand_and_name(
            session=session,
            brand=brand,
            name=incoming.name,
        )
    )

    if brand_name_match is not None:
        return (
            brand_name_match,
            ProductMatchType.BRAND_AND_NAME,
        )

    if not is_generic_product_name(
        incoming.name
    ):
        name_match = (
            await find_safe_name_match(
                session=session,
                name=incoming.name,
            )
        )

        if name_match is not None:
            return (
                name_match,
                ProductMatchType.NAME,
            )

    return None, None


async def merge_product_fields(
    *,
    session: AsyncSession,
    product: Product,
    incoming_brand: Brand,
    incoming: ExternalProductData,
) -> tuple[
    list[str],
    Brand,
    Category | None,
]:
    """
    Дополняет существующий товар.

    Здесь разрешено исправлять:

    - общее название;
    - служебный бренд;
    - общую категорию;
    - пустую упаковку;
    - пустое описание;
    - пустую картинку;
    - ключевые слова.

    Рейтинги, отзывы и ID товара
    не изменяются.
    """

    updated_fields: list[str] = []

    current_brand = (
        await get_brand_by_id(
            session=session,
            brand_id=product.brand_id,
        )
    )

    current_category = (
        await get_category_by_id(
            session=session,
            category_id=product.category_id,
        )
    )

    incoming_name = clean_text(
        incoming.name
    )

    if is_better_name(
        current_name=product.name,
        incoming_name=incoming_name,
    ):
        product.name = incoming_name

        product.normalized_name = (
            normalized(
                incoming_name
            )
        )

        updated_fields.append(
            "name"
        )

    normalized_barcode = (
        normalize_barcode(
            incoming.barcode
        )
    )

    if (
        not product.barcode
        and normalized_barcode
    ):
        product.barcode = (
            normalized_barcode
        )

        updated_fields.append(
            "barcode"
        )

    incoming_package_value = (
        normalize_package_value(
            incoming.package_value
        )
    )

    incoming_package_unit = (
        normalize_package_unit(
            incoming.package_unit
        )
    )

    if (
        product.package_value is None
        and incoming_package_value
        is not None
    ):
        product.package_value = (
            incoming_package_value
        )

        updated_fields.append(
            "package_value"
        )

    if (
        not product.package_unit
        and incoming_package_unit
    ):
        product.package_unit = (
            incoming_package_unit
        )

        updated_fields.append(
            "package_unit"
        )

    if should_fill_text(
        product.subtype,
        incoming.subtype,
    ):
        product.subtype = (
            clean_text(
                incoming.subtype
            )
        )

        updated_fields.append(
            "subtype"
        )

    if should_fill_text(
        product.description,
        incoming.description,
    ):
        product.description = (
            clean_text(
                incoming.description
            )
        )

        updated_fields.append(
            "description"
        )

    if should_fill_text(
        product.image_url,
        incoming.image_url,
    ):
        product.image_url = (
            clean_text(
                incoming.image_url
            )
        )

        updated_fields.append(
            "image_url"
        )

    merged_keywords = combine_keywords(
        product.keywords,
        incoming.keywords,
    )

    if (
        merged_keywords
        and merged_keywords
        != product.keywords
    ):
        product.keywords = (
            merged_keywords
        )

        updated_fields.append(
            "keywords"
        )

    #
    # BRAND MERGE
    #
    # Служебный бренд можно заменить
    # нормальным брендом OpenFoodFacts.
    #

    current_brand_name = (
        current_brand.name
        if current_brand is not None
        else None
    )

    if (
        not is_unknown_brand(
            incoming_brand.name
        )
        and (
            current_brand is None
            or is_unknown_brand(
                current_brand_name
            )
        )
        and product.brand_id
        != incoming_brand.id
    ):
        product.brand_id = (
            incoming_brand.id
        )

        current_brand = (
            incoming_brand
        )

        updated_fields.append(
            "brand_id"
        )

    #
    # CATEGORY MERGE
    #
    # Старую общую категорию "Продукты"
    # разрешаем заменить на более точную.
    #

    if incoming.category_id is not None:
        incoming_category = (
            await get_category_by_id(
                session=session,
                category_id=incoming.category_id,
            )
        )

        if incoming_category is not None:
            current_category_name = (
                current_category.name
                if current_category
                is not None
                else None
            )

            should_replace_category = (
                current_category is None
                or is_generic_category(
                    current_category_name
                )
            )

            if (
                should_replace_category
                and product.category_id
                != incoming_category.id
            ):
                product.category_id = (
                    incoming_category.id
                )

                current_category = (
                    incoming_category
                )

                updated_fields.append(
                    "category_id"
                )

    #
    # SEARCH TEXT
    #

    actual_brand = (
        current_brand
        or incoming_brand
    )

    product.search_text = (
        build_search_text(
            product=product,
            brand=actual_brand,
            category=current_category,
        )
    )

    updated_fields.append(
        "search_text"
    )

    return (
        updated_fields,
        actual_brand,
        current_category,
    )


async def create_product(
    *,
    session: AsyncSession,
    incoming: ExternalProductData,
    brand: Brand,
) -> Product:
    """
    Создаёт новый товар.
    """

    if incoming.category_id is None:
        raise ValueError(
            "Невозможно создать новый товар "
            "без category_id."
        )

    product_name = clean_text(
        incoming.name
    )

    if not product_name:
        raise ValueError(
            "Невозможно создать товар "
            "без названия."
        )

    product = Product(
        name=product_name,
        normalized_name=normalized(
            product_name
        ),
        brand_id=brand.id,
        category_id=incoming.category_id,
        family_id=incoming.family_id,
        barcode=normalize_barcode(
            incoming.barcode
        ),
        package_value=(
            normalize_package_value(
                incoming.package_value
            )
        ),
        package_unit=(
            normalize_package_unit(
                incoming.package_unit
            )
        ),
        subtype=(
            clean_text(
                incoming.subtype
            )
            or None
        ),
        description=(
            clean_text(
                incoming.description
            )
            or None
        ),
        image_url=(
            clean_text(
                incoming.image_url
            )
            or None
        ),
        keywords=(
            clean_text(
                incoming.keywords
            )
            or None
        ),
        is_active=True,
    )

    session.add(
        product
    )

    await session.flush()

    category = (
        await get_category_by_id(
            session=session,
            category_id=product.category_id,
        )
    )

    product.search_text = (
        build_search_text(
            product=product,
            brand=brand,
            category=category,
        )
    )

    await session.flush()

    return product


async def merge_external_product(
    *,
    session: AsyncSession,
    incoming: ExternalProductData,
    commit: bool = False,
) -> ProductMergeResult:
    """
    Главная точка входа Product Merge Engine.

    Приоритет сопоставления:

        barcode
          ↓
        brand + name
          ↓
        unique exact name

    Если товар существует, сохраняется его ID,
    рейтинги, отзывы и история.

    Внешние данные только улучшают карточку.
    """

    if not clean_text(
        incoming.source
    ):
        raise ValueError(
            "Не указан источник товара."
        )

    if not clean_text(
        incoming.name
    ):
        raise ValueError(
            "Не указано название товара."
        )

    incoming_brand = (
        await get_or_create_brand(
            session=session,
            brand_name=incoming.brand_name,
        )
    )

    product, match_type = (
        await find_matching_product(
            session=session,
            incoming=incoming,
            brand=incoming_brand,
        )
    )

    created = False

    result_brand = (
        incoming_brand
    )

    if product is None:
        product = await create_product(
            session=session,
            incoming=incoming,
            brand=incoming_brand,
        )

        created = True

        match_type = (
            ProductMatchType.CREATED
        )

        updated_fields = (
            "name",
            "brand_id",
            "category_id",
            "barcode",
            "package_value",
            "package_unit",
            "subtype",
            "description",
            "image_url",
            "keywords",
            "search_text",
        )

    else:
        (
            updated_list,
            result_brand,
            _category,
        ) = await merge_product_fields(
            session=session,
            product=product,
            incoming_brand=incoming_brand,
            incoming=incoming,
        )

        updated_fields = tuple(
            updated_list
        )

    await session.flush()

    if commit:
        await session.commit()

    return ProductMergeResult(
        product=product,
        brand=result_brand,
        created=created,
        match_type=(
            match_type
            or ProductMatchType.CREATED
        ),
        updated_fields=tuple(
            updated_fields
        ),
        source=clean_text(
            incoming.source
        ),
    )
