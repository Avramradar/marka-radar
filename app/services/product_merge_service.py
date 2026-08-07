from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
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

    В будущем в него смогут преобразовываться:

    - OpenFoodFacts;
    - Яндекс Маркет;
    - файлы поставщиков;
    - каталоги производителей;
    - другие разрешённые API.
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
        str(value).strip().split()
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

    return normalize_text(
        cleaned
    )


def normalize_barcode(
    barcode: str | None,
) -> str | None:
    """
    Нормализует штрихкод.

    Оставляем только цифры.
    """

    if not barcode:
        return None

    digits = "".join(
        char
        for char in str(barcode)
        if char.isdigit()
    )

    if not digits:
        return None

    # Не считаем слишком короткое число
    # нормальным товарным штрихкодом.
    if len(digits) < 8:
        return None

    return digits


def normalize_package_unit(
    unit: str | None,
) -> str | None:
    """
    Приводит единицы упаковки
    к более стабильному виду.
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
        clean_text(unit).lower(),
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
    Проверяет служебные названия бренда.
    """

    return (
        normalized(
            brand_name
        )
        in {
            normalized(item)
            for item in UNKNOWN_BRAND_NAMES
        }
    )


def is_generic_product_name(
    name: str | None,
) -> bool:
    """
    Определяет слишком общее название.

    Например:

        Кофе
        Молоко
        Пицца

    Такое имя можно безопасно заменить
    более информативным внешним названием.
    """

    normalized_name = normalized(
        name
    )

    return (
        normalized_name
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

    Главный принцип:
    хорошие данные не портим.

    Но:

        Кофе

    можно заменить на:

        Кофе растворимый Carte Noire 95 г
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

    if normalized(current) == normalized(incoming):
        return False

    if (
        is_generic_product_name(current)
        and len(incoming) > len(current)
    ):
        return True

    # Сильно более подробное имя
    # также можно считать улучшением.
    if (
        len(current) <= 10
        and len(incoming)
        >= len(current) + 8
    ):
        return True

    return False


def should_fill_text(
    current_value: Any,
    incoming_value: Any,
) -> bool:
    """
    Простое правило для дополнительного поля:

    заполняем только если у MarkaRadar
    сейчас ничего нет.
    """

    return (
        not clean_text(current_value)
        and bool(clean_text(incoming_value))
    )


def combine_keywords(
    current_keywords: str | None,
    incoming_keywords: str | None,
) -> str | None:
    """
    Аккуратно объединяет ключевые слова.
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
        # Поддерживаем как запятые,
        # так и обычные строковые наборы.
        parts = [
            part.strip()
            for part in text.replace(
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
) -> str:
    """
    Перестраивает внутреннее поисковое поле товара.

    Сюда входят только существующие данные.
    """

    parts = (
        product.name,
        brand.name,
        product.subtype,
        product.keywords,
        product.description,
        product.package_value,
        product.package_unit,
        product.barcode,
    )

    normalized_parts = [
        normalized(part)
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

    Для отсутствующего бренда используется
    единая служебная запись.
    """

    clean_brand = clean_text(
        brand_name
    )

    if (
        not clean_brand
        or is_unknown_brand(clean_brand)
    ):
        clean_brand = "Бренд не указан"

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


async def find_product_by_barcode(
    *,
    session: AsyncSession,
    barcode: str | None,
) -> Product | None:
    """
    Самый надёжный способ сопоставления товара.
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
    Второй уровень сопоставления.

    Используется только точное нормализованное
    совпадение бренда и названия.

    Намеренно не используем агрессивный fuzzy-match:
    случайное объединение двух разных товаров
    гораздо опаснее создания дубля.
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
    Очень осторожный fallback.

    По одному названию объединяем только тогда,
    когда существует ровно один товар
    с таким точным normalized_name.

    Если найдено два товара — не угадываем.
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
    Ищет канонический товар.

    Приоритет:

    1. штрихкод;
    2. бренд + точное название;
    3. уникальное точное название.

    Никаких опасных автоматических
    fuzzy-слияний на первом этапе.
    """

    barcode_match = await find_product_by_barcode(
        session=session,
        barcode=incoming.barcode,
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

    # Если бренд не указан, по одному общему
    # названию вроде «Кофе» не объединяем.
    if (
        not is_generic_product_name(
            incoming.name
        )
    ):
        name_match = await find_safe_name_match(
            session=session,
            name=incoming.name,
        )

        if name_match is not None:
            return (
                name_match,
                ProductMatchType.NAME,
            )

    return None, None


def merge_product_fields(
    *,
    product: Product,
    brand: Brand,
    incoming: ExternalProductData,
) -> list[str]:
    """
    Безопасно дополняет существующую карточку.

    Ключевое правило:

    внешний источник не должен превращать
    хорошую карточку MarkaRadar в плохую.
    """

    updated_fields: list[str] = []

    incoming_name = clean_text(
        incoming.name
    )

    if is_better_name(
        current_name=product.name,
        incoming_name=incoming_name,
    ):
        product.name = incoming_name
        product.normalized_name = normalized(
            incoming_name
        )

        updated_fields.append(
            "name"
        )

    normalized_barcode = normalize_barcode(
        incoming.barcode
    )

    if (
        not product.barcode
        and normalized_barcode
    ):
        product.barcode = normalized_barcode

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
        and incoming_package_value is not None
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
        product.subtype = clean_text(
            incoming.subtype
        )

        updated_fields.append(
            "subtype"
        )

    if should_fill_text(
        product.description,
        incoming.description,
    ):
        product.description = clean_text(
            incoming.description
        )

        updated_fields.append(
            "description"
        )

    if should_fill_text(
        product.image_url,
        incoming.image_url,
    ):
        product.image_url = clean_text(
            incoming.image_url
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
        product.keywords = merged_keywords

        updated_fields.append(
            "keywords"
        )

    # Если исходная карточка имела служебный бренд,
    # а внешний источник дал настоящий —
    # привязываем настоящий бренд.
    current_brand_name = (
        product.brand.name
        if getattr(
            product,
            "brand",
            None,
        )
        else None
    )

    if (
        brand.id != product.brand_id
        and is_unknown_brand(
            current_brand_name
        )
        and not is_unknown_brand(
            brand.name
        )
    ):
        product.brand_id = brand.id
        product.brand = brand

        updated_fields.append(
            "brand_id"
        )

    # category_id автоматически не меняем.
    #
    # Ошибка категории может сломать поиск
    # гораздо сильнее, чем отсутствие категории.
    #
    # На следующем этапе сделаем Category Mapper.

    product.search_text = build_search_text(
        product=product,
        brand=brand,
    )

    if "search_text" not in updated_fields:
        updated_fields.append(
            "search_text"
        )

    return updated_fields


async def create_product(
    *,
    session: AsyncSession,
    incoming: ExternalProductData,
    brand: Brand,
) -> Product:
    """
    Создаёт новый товар.

    category_id обязателен для новой карточки,
    потому что поле Product.category_id
    в текущей модели nullable=False.
    """

    if incoming.category_id is None:
        raise ValueError(
            "Невозможно создать новый товар "
            "без category_id. "
            "Сначала требуется Category Mapper."
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
        package_value=normalize_package_value(
            incoming.package_value
        ),
        package_unit=normalize_package_unit(
            incoming.package_unit
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

    product.brand = brand

    product.search_text = build_search_text(
        product=product,
        brand=brand,
    )

    session.add(
        product
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

    Пример:

        result = await merge_external_product(
            session=session,
            incoming=ExternalProductData(
                source="openfoodfacts",
                barcode="4601234567890",
                brand_name="Carte Noire",
                name="Кофе растворимый 95 г",
                category_id=12,
            ),
        )

    Алгоритм:

    1. нормализует бренд;
    2. ищет товар по штрихкоду;
    3. затем по brand + name;
    4. осторожно проверяет уникальное имя;
    5. дополняет существующую карточку;
    6. либо создаёт новый товар.

    Рейтинги, отзывы и пользовательские данные
    эта функция вообще не изменяет.
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

    brand = await get_or_create_brand(
        session=session,
        brand_name=incoming.brand_name,
    )

    product, match_type = (
        await find_matching_product(
            session=session,
            incoming=incoming,
            brand=brand,
        )
    )

    created = False

    if product is None:
        product = await create_product(
            session=session,
            incoming=incoming,
            brand=brand,
        )

        match_type = (
            ProductMatchType.CREATED
        )

        created = True

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
        updated_fields = tuple(
            merge_product_fields(
                product=product,
                brand=brand,
                incoming=incoming,
            )
        )

    await session.flush()

    if commit:
        await session.commit()

    return ProductMergeResult(
        product=product,
        brand=brand,
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
