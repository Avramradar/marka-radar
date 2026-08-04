import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.repositories.product_family_repository import (
    assign_product_family,
)
from app.database.session import async_session_maker
from app.importers.open_food_facts_client import (
    OpenFoodFactsClient,
)
from app.search.index_builder import build_search_index
from app.utils.text import normalize_text


logger = logging.getLogger(__name__)


DEFAULT_BRAND_NAME = "Бренд не указан"
DEFAULT_CATEGORY_NAME = "Продукты"


@dataclass
class ImportStatistics:
    received: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    brands_created: int = 0
    categories_created: int = 0


@dataclass
class PreparedProduct:
    barcode: str
    name: str
    brand_name: str
    category_name: str
    package_value: Decimal | None
    package_unit: str | None
    image_url: str | None
    keywords: str | None
    search_text: str


def clean_text(
    value: Any,
    *,
    max_length: int | None = None,
) -> str | None:
    """
    Очищает внешнее текстовое значение.

    Удаляет лишние пробелы и при необходимости
    ограничивает максимальную длину строки.
    """

    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    if max_length is not None:
        text = text[:max_length].strip()

    return text or None


def choose_product_name(
    raw_product: dict[str, Any],
) -> str | None:
    """
    Выбирает наиболее подходящее название товара.

    Приоритет:
    1. русское название;
    2. основное название;
    3. общее название.
    """

    candidates = (
        raw_product.get("product_name_ru"),
        raw_product.get("product_name"),
        raw_product.get("generic_name"),
    )

    for candidate in candidates:
        cleaned = clean_text(
            candidate,
            max_length=255,
        )

        if cleaned:
            return cleaned

    return None


def choose_brand_name(
    raw_product: dict[str, Any],
) -> str:
    """
    Выбирает основной бренд товара.

    Open Food Facts может передавать несколько брендов
    через запятую. В основное поле сохраняется первый.
    """

    brands = clean_text(
        raw_product.get("brands"),
        max_length=500,
    )

    if not brands:
        return DEFAULT_BRAND_NAME

    first_brand = brands.split(",")[0].strip()

    if not first_brand:
        return DEFAULT_BRAND_NAME

    return first_brand[:128]


def category_from_tag(
    tag: str,
) -> str | None:
    """
    Преобразует технический тег категории
    Open Food Facts в читаемое название.
    """

    cleaned = tag.strip()

    if not cleaned:
        return None

    if ":" in cleaned:
        _, cleaned = cleaned.split(
            ":",
            1,
        )

    cleaned = cleaned.replace(
        "-",
        " ",
    )

    cleaned = cleaned.replace(
        "_",
        " ",
    )

    cleaned = re.sub(
        r"\s+",
        " ",
        cleaned,
    ).strip()

    if not cleaned:
        return None

    return cleaned[:128].capitalize()


def choose_category_name(
    raw_product: dict[str, Any],
) -> str:
    """
    Выбирает наиболее конкретную категорию товара.
    """

    category_tags = raw_product.get(
        "categories_tags"
    )

    if isinstance(category_tags, list):
        for tag in reversed(category_tags):
            if not isinstance(tag, str):
                continue

            category_name = category_from_tag(
                tag
            )

            if category_name:
                return category_name

    categories = clean_text(
        raw_product.get("categories"),
        max_length=500,
    )

    if categories:
        last_category = categories.split(",")[-1].strip()

        if last_category:
            return last_category[:128]

    return DEFAULT_CATEGORY_NAME


def normalize_package_unit(
    unit: str | None,
) -> str | None:
    """
    Приводит единицы измерения к единому формату.
    """

    if not unit:
        return None

    normalized = unit.strip().lower()

    unit_map = {
        "g": "г",
        "gram": "г",
        "grams": "г",
        "гр": "г",
        "kg": "кг",
        "kilogram": "кг",
        "kilograms": "кг",
        "кг": "кг",
        "ml": "мл",
        "мл": "мл",
        "cl": "мл",
        "l": "л",
        "litre": "л",
        "liter": "л",
        "литр": "л",
        "литра": "л",
        "литров": "л",
        "л": "л",
    }

    return unit_map.get(
        normalized,
        normalized[:20],
    )


def parse_quantity_text(
    quantity: str | None,
) -> tuple[Decimal | None, str | None]:
    """
    Извлекает значение и единицу измерения
    из текстового поля quantity.

    Примеры:
    500 g
    1.5 l
    250 мл
    """

    if not quantity:
        return None, None

    normalized = quantity.strip().lower()

    normalized = normalized.replace(
        ",",
        ".",
    )

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*"
        r"(kg|g|гр|кг|ml|мл|cl|l|л)\b",
        normalized,
    )

    if not match:
        return None, None

    value_text = match.group(1)
    raw_unit = match.group(2)

    unit_map = {
        "g": "г",
        "гр": "г",
        "кг": "кг",
        "kg": "кг",
        "ml": "мл",
        "мл": "мл",
        "cl": "мл",
        "l": "л",
        "л": "л",
    }

    try:
        value = Decimal(value_text)
    except InvalidOperation:
        return None, None

    unit = unit_map.get(raw_unit)

    # Один сантилитр равен десяти миллилитрам.
    if raw_unit == "cl":
        value *= Decimal("10")

    return value, unit


def parse_package(
    raw_product: dict[str, Any],
) -> tuple[Decimal | None, str | None]:
    """
    Извлекает размер упаковки товара.

    Сначала используются структурированные поля.
    При их отсутствии разбирается строка quantity.
    """

    raw_value = raw_product.get(
        "product_quantity"
    )

    raw_unit = clean_text(
        raw_product.get(
            "product_quantity_unit"
        ),
        max_length=20,
    )

    if raw_value is not None:
        try:
            value = Decimal(
                str(raw_value)
            )

            unit = normalize_package_unit(
                raw_unit
            )

            if value > 0 and unit:
                return value, unit

        except InvalidOperation:
            pass

    quantity = clean_text(
        raw_product.get("quantity"),
        max_length=100,
    )

    return parse_quantity_text(
        quantity
    )


def choose_image_url(
    raw_product: dict[str, Any],
) -> str | None:
    """
    Выбирает доступное изображение товара.
    """

    candidates = (
        raw_product.get("image_front_url"),
        raw_product.get("image_url"),
        raw_product.get(
            "image_front_small_url"
        ),
    )

    for candidate in candidates:
        image_url = clean_text(
            candidate,
            max_length=2000,
        )

        if (
            image_url
            and image_url.startswith(
                (
                    "https://",
                    "http://",
                )
            )
        ):
            return image_url

    return None


def build_keywords(
    raw_product: dict[str, Any],
    *,
    name: str,
    brand_name: str,
    category_name: str,
) -> str:
    """
    Формирует базовую строку ключевых слов товара.
    """

    values: list[str] = [
        name,
        brand_name,
        category_name,
    ]

    categories = clean_text(
        raw_product.get("categories"),
        max_length=1000,
    )

    generic_name = clean_text(
        raw_product.get("generic_name"),
        max_length=500,
    )

    if categories:
        values.append(categories)

    if generic_name:
        values.append(generic_name)

    normalized_values = [
        normalize_text(value)
        for value in values
        if value
    ]

    unique_values = list(
        dict.fromkeys(
            value
            for value in normalized_values
            if value
        )
    )

    return " ".join(
        unique_values
    )[:3000]


def prepare_product(
    raw_product: dict[str, Any],
) -> PreparedProduct | None:
    """
    Проверяет и преобразует товар Open Food Facts
    в структуру, готовую для записи в базу.
    """

    barcode = clean_text(
        raw_product.get("code"),
        max_length=32,
    )

    if not barcode or not barcode.isdigit():
        return None

    name = choose_product_name(
        raw_product
    )

    if not name:
        return None

    brand_name = choose_brand_name(
        raw_product
    )

    category_name = choose_category_name(
        raw_product
    )

    package_value, package_unit = parse_package(
        raw_product
    )

    image_url = choose_image_url(
        raw_product
    )

    keywords = build_keywords(
        raw_product,
        name=name,
        brand_name=brand_name,
        category_name=category_name,
    )

    search_text = build_search_index(
        name=name,
        brand=brand_name,
        category=category_name,
        keywords=keywords,
    )

    return PreparedProduct(
        barcode=barcode,
        name=name,
        brand_name=brand_name,
        category_name=category_name,
        package_value=package_value,
        package_unit=package_unit,
        image_url=image_url,
        keywords=keywords,
        search_text=search_text,
    )


async def get_or_create_brand(
    session: AsyncSession,
    name: str,
    statistics: ImportStatistics,
) -> Brand:
    """
    Возвращает существующий бренд
    или создаёт новый.
    """

    normalized_name = normalize_text(
        name
    )

    result = await session.execute(
        select(Brand).where(
            Brand.normalized_name
            == normalized_name
        )
    )

    brand = result.scalar_one_or_none()

    if brand is not None:
        return brand

    brand = Brand(
        name=name,
        normalized_name=normalized_name,
    )

    session.add(brand)
    await session.flush()

    statistics.brands_created += 1

    return brand


async def get_or_create_category(
    session: AsyncSession,
    name: str,
    statistics: ImportStatistics,
) -> Category:
    """
    Возвращает существующую категорию
    или создаёт новую.
    """

    normalized_name = normalize_text(
        name
    )

    result = await session.execute(
        select(Category).where(
            Category.normalized_name
            == normalized_name
        )
    )

    category = result.scalar_one_or_none()

    if category is not None:
        return category

    category = Category(
        name=name,
        normalized_name=normalized_name,
    )

    session.add(category)
    await session.flush()

    statistics.categories_created += 1

    return category


async def save_product(
    session: AsyncSession,
    prepared: PreparedProduct,
    statistics: ImportStatistics,
) -> None:
    """
    Создаёт новый товар или обновляет существующий
    по уникальному штрихкоду.

    После создания или обновления автоматически
    определяет семейство товара.
    """

    brand = await get_or_create_brand(
        session=session,
        name=prepared.brand_name,
        statistics=statistics,
    )

    category = await get_or_create_category(
        session=session,
        name=prepared.category_name,
        statistics=statistics,
    )

    result = await session.execute(
        select(Product).where(
            Product.barcode
            == prepared.barcode
        )
    )

    product = result.scalar_one_or_none()

    if product is None:
        product = Product(
            name=prepared.name,
            normalized_name=normalize_text(
                prepared.name
            ),
            brand_id=brand.id,
            category_id=category.id,
            barcode=prepared.barcode,
            package_value=prepared.package_value,
            package_unit=prepared.package_unit,
            image_url=prepared.image_url,
            keywords=prepared.keywords,
            search_text=prepared.search_text,
            is_active=True,
        )

        session.add(product)

        await assign_product_family(
            session=session,
            product=product,
            brand_name=brand.name,
            category=category,
        )

        statistics.created += 1
        return

    product.name = prepared.name

    product.normalized_name = normalize_text(
        prepared.name
    )

    product.brand_id = brand.id
    product.category_id = category.id

    product.package_value = (
        prepared.package_value
    )

    product.package_unit = (
        prepared.package_unit
    )

    product.image_url = (
        prepared.image_url
        or product.image_url
    )

    product.keywords = prepared.keywords
    product.search_text = prepared.search_text
    product.is_active = True

    await assign_product_family(
        session=session,
        product=product,
        brand_name=brand.name,
        category=category,
    )

    statistics.updated += 1


async def import_open_food_facts_products(
    *,
    page: int = 1,
    page_size: int = 100,
    country: str = "russia",
) -> ImportStatistics:
    """
    Загружает страницу товаров Open Food Facts
    и сохраняет её в базу данных.
    """

    client = OpenFoodFactsClient()

    raw_products = await client.search_products(
        page=page,
        page_size=page_size,
        country=country,
    )

    statistics = ImportStatistics(
        received=len(raw_products)
    )

    async with async_session_maker() as session:
        for raw_product in raw_products:
            prepared = prepare_product(
                raw_product
            )

            if prepared is None:
                statistics.skipped += 1
                continue

            try:
                async with session.begin_nested():
                    await save_product(
                        session=session,
                        prepared=prepared,
                        statistics=statistics,
                    )

            except Exception:
                statistics.errors += 1

                logger.exception(
                    "Не удалось импортировать товар "
                    "со штрихкодом %s",
                    prepared.barcode,
                )

        await session.commit()

    logger.info(
        "Импорт Open Food Facts завершён. "
        "Получено: %s; создано: %s; "
        "обновлено: %s; пропущено: %s; "
        "ошибок: %s; новых брендов: %s; "
        "новых категорий: %s.",
        statistics.received,
        statistics.created,
        statistics.updated,
        statistics.skipped,
        statistics.errors,
        statistics.brands_created,
        statistics.categories_created,
    )

    return statistics
