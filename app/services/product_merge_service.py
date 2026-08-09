from __future__ import annotations

import logging
import re
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


logger = logging.getLogger(__name__)


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}

GENERIC_PRODUCT_NAMES = {
    "кофе", "молоко", "чай", "вода", "пицца", "сыр", "масло",
    "йогурт", "кефир", "сельдь", "сок", "сметана", "творог",
    "сливки", "колбаса",
}

GENERIC_CATEGORY_NAMES = {
    "", "продукты", "продукт", "еда", "food", "foods",
    "products", "product", "прочее", "другое", "other",
}

PLACEHOLDER_IMAGE_MARKERS = (
    "placeholder",
    "no-image",
    "no_image",
    "default-product",
    "default_product",
    "image-not-found",
    "image_not_found",
)

MARKETING_DESCRIPTION_MARKERS = (
    "купить",
    "заказать",
    "доставка",
    "акция",
    "скидка",
    "выгодная цена",
    "лучшая цена",
)

PACKAGE_FAMILIES = {
    "г": "mass",
    "кг": "mass",
    "мл": "volume",
    "л": "volume",
}


class ProductMatchType(StrEnum):
    BARCODE = "barcode"
    BRAND_AND_NAME = "brand_and_name"
    NAME = "name"
    CREATED = "created"


@dataclass(slots=True)
class ExternalProductData:
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

    # Эти поля уже готовы для будущего слоя provenance.
    # Старые провайдеры их могут не передавать.
    source_priority: int = 50
    confidence: float = 100.0


@dataclass(slots=True)
class ProductMergeResult:
    product: Product
    brand: Brand
    created: bool
    match_type: ProductMatchType
    updated_fields: tuple[str, ...]
    source: str
    conflicts: tuple[str, ...] = ()


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).strip().split())


def normalized(value: Any) -> str:
    cleaned = clean_text(value)
    if not cleaned:
        return ""
    return normalize_text(cleaned).replace("ё", "е")


def normalize_barcode(barcode: str | None) -> str | None:
    if not barcode:
        return None

    digits = "".join(
        char
        for char in str(barcode)
        if char.isdigit()
    )

    if not 8 <= len(digits) <= 14:
        return None

    return digits


def normalize_package_unit(unit: str | None) -> str | None:
    clean_unit = normalized(unit)

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


def normalize_package_value( value: Decimal | float | int | None, ) -> Decimal | None:
    if value is None:
        return None

    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return None

    if decimal_value <= 0:
        return None

    return decimal_value


def package_to_base( value: Decimal | float | int | None, unit: str | None, ) -> tuple[str | None, Decimal | None]:
    normalized_value = normalize_package_value(value)
    normalized_unit = normalize_package_unit(unit)

    if normalized_value is None or normalized_unit is None:
        return None, None

    family = PACKAGE_FAMILIES.get(normalized_unit)

    if family is None:
        return None, None

    if normalized_unit in {"кг", "л"}:
        normalized_value *= Decimal("1000")

    return family, normalized_value


def package_values_compatible( *, current_value: Decimal | float | int | None, current_unit: str | None, incoming_value: Decimal | float | int | None, incoming_unit: str | None, tolerance_percent: Decimal = Decimal("3"), ) -> bool | None:
    current_family, current_base = package_to_base(
        current_value,
        current_unit,
    )
    incoming_family, incoming_base = package_to_base(
        incoming_value,
        incoming_unit,
    )

    if (
        current_base is None
        or incoming_base is None
        or current_family is None
        or incoming_family is None
    ):
        return None

    if current_family != incoming_family:
        return False

    larger = max(current_base, incoming_base)

    if larger <= 0:
        return None

    difference_percent = (
        abs(current_base - incoming_base)
        / larger
        * Decimal("100")
    )

    return difference_percent <= tolerance_percent


def is_unknown_brand(brand_name: str | None) -> bool:
    normalized_name = normalized(brand_name)

    return (
        not normalized_name
        or normalized_name
        in {
            normalized(item)
            for item in UNKNOWN_BRAND_NAMES
        }
    )


def is_generic_category(category_name: str | None) -> bool:
    return (
        normalized(category_name)
        in {
            normalized(item)
            for item in GENERIC_CATEGORY_NAMES
        }
    )


def is_generic_product_name(name: str | None) -> bool:
    return (
        normalized(name)
        in {
            normalized(item)
            for item in GENERIC_PRODUCT_NAMES
        }
    )


def tokenize_text(value: str | None) -> set[str]:
    return {
        token
        for token in re.findall(
            r"[a-zа-я0-9]+",
            normalized(value),
            flags=re.IGNORECASE,
        )
        if len(token) >= 2
    }


def text_similarity( left: str | None, right: str | None, ) -> float:
    left_tokens = tokenize_text(left)
    right_tokens = tokenize_text(right)

    if not left_tokens or not right_tokens:
        return 0.0

    union = len(left_tokens | right_tokens)

    if union <= 0:
        return 0.0

    return len(left_tokens & right_tokens) / union


def name_quality_score(value: str | None) -> float:
    text = clean_text(value)

    if not text:
        return 0.0

    score = 20.0

    if not is_generic_product_name(text):
        score += 30.0

    score += min(
        len(tokenize_text(text)) * 5.0,
        25.0,
    )

    if re.search(r"\d", text):
        score += 5.0

    if len(text) >= 12:
        score += 10.0

    if len(text) > 180:
        score -= 20.0

    return max(0.0, min(score, 100.0))


def description_quality_score(value: str | None) -> float:
    text = clean_text(value)

    if not text:
        return 0.0

    score = min(len(text) / 8.0, 70.0)
    normalized_text = normalized(text)

    marketing_hits = sum(
        1
        for marker in MARKETING_DESCRIPTION_MARKERS
        if marker in normalized_text
    )

    score -= marketing_hits * 10.0

    if len(text) >= 80:
        score += 10.0

    if len(text) >= 200:
        score += 10.0

    return max(0.0, min(score, 100.0))


def image_quality_score(value: str | None) -> float:
    image = clean_text(value)

    if not image:
        return 0.0

    normalized_image = normalized(image)

    if any(
        marker in normalized_image
        for marker in PLACEHOLDER_IMAGE_MARKERS
    ):
        return 10.0

    if image.startswith(("http://", "https://")):
        score = 70.0

        if re.search(
            r"\.(jpg|jpeg|png|webp)(?:$|\?)",
            image,
            flags=re.IGNORECASE,
        ):
            score += 10.0

        return min(score, 100.0)

    # Telegram file_id / локальное пользовательское изображение.
    return 95.0


def is_better_name( *, current_name: str | None, incoming_name: str | None, ) -> bool:
    current = clean_text(current_name)
    incoming = clean_text(incoming_name)

    if not incoming:
        return False

    if not current:
        return True

    if normalized(current) == normalized(incoming):
        return False

    if (
        is_generic_product_name(current)
        and not is_generic_product_name(incoming)
    ):
        return True

    similarity = text_similarity(current, incoming)

    # Хорошее существующее имя нельзя заменить
    # совершенно другим названием.
    if (
        not is_generic_product_name(current)
        and similarity < 0.20
    ):
        return False

    return (
        name_quality_score(incoming)
        >= name_quality_score(current) + 15.0
    )


def should_replace_description( *, current_value: str | None, incoming_value: str | None, ) -> bool:
    incoming = clean_text(incoming_value)

    if not incoming:
        return False

    current = clean_text(current_value)

    if not current:
        return True

    return (
        description_quality_score(incoming)
        >= description_quality_score(current) + 15.0
    )


def should_replace_image( *, current_value: str | None, incoming_value: str | None, ) -> bool:
    incoming = clean_text(incoming_value)

    if not incoming:
        return False

    current = clean_text(current_value)

    if not current:
        return True

    return (
        image_quality_score(incoming)
        >= image_quality_score(current) + 20.0
    )


def should_fill_text( current_value: Any, incoming_value: Any, ) -> bool:
    return (
        not clean_text(current_value)
        and bool(clean_text(incoming_value))
    )


def combine_keywords( current_keywords: str | None, incoming_keywords: str | None, ) -> str | None:
    current = clean_text(current_keywords)
    incoming = clean_text(incoming_keywords)

    if not current and not incoming:
        return None

    if not current:
        return incoming

    if not incoming:
        return current

    values: list[str] = []
    seen: set[str] = set()

    for text in (current, incoming):
        parts = [
            part.strip()
            for part in text.replace(";", ",").split(",")
            if part.strip()
        ]

        if len(parts) == 1:
            parts = [text]

        for part in parts:
            key = normalized(part)

            if not key or key in seen:
                continue

            seen.add(key)
            values.append(part)

    return ", ".join(values)


def build_search_text( *, product: Product, brand: Brand, category: Category | None = None, ) -> str:
    parts = (
        product.name,
        brand.name,
        category.name if category is not None else None,
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


async def find_brand( *, session: AsyncSession, brand_name: str, ) -> Brand | None:
    normalized_name = normalized(brand_name)

    if not normalized_name:
        return None

    statement = (
        select(Brand)
        .where(
            or_(
                Brand.normalized_name == normalized_name,
                Brand.name.ilike(brand_name),
            )
        )
        .limit(1)
    )

    result = await session.execute(statement)
    return result.scalar_one_or_none()


async def get_or_create_brand( *, session: AsyncSession, brand_name: str | None, ) -> Brand:
    clean_brand = clean_text(brand_name)

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
        normalized_name=normalized(clean_brand),
    )

    session.add(brand)
    await session.flush()

    return brand


async def get_brand_by_id( *, session: AsyncSession, brand_id: int, ) -> Brand | None:
    result = await session.execute(
        select(Brand)
        .where(Brand.id == brand_id)
        .limit(1)
    )

    return result.scalar_one_or_none()


async def get_category_by_id( *, session: AsyncSession, category_id: int | None, ) -> Category | None:
    if category_id is None:
        return None

    result = await session.execute(
        select(Category)
        .where(Category.id == category_id)
        .limit(1)
    )

    return result.scalar_one_or_none()


async def find_product_by_barcode( *, session: AsyncSession, barcode: str | None, ) -> Product | None:
    normalized_barcode = normalize_barcode(barcode)

    if normalized_barcode is None:
        return None

    result = await session.execute(
        select(Product)
        .where(Product.barcode == normalized_barcode)
        .limit(1)
    )

    return result.scalar_one_or_none()


def product_package_compatible_with_incoming( *, product: Product, incoming: ExternalProductData, ) -> bool:
    compatibility = package_values_compatible(
        current_value=product.package_value,
        current_unit=product.package_unit,
        incoming_value=incoming.package_value,
        incoming_unit=incoming.package_unit,
    )

    return compatibility is not False


async def find_product_by_brand_and_name( *, session: AsyncSession, brand: Brand, incoming: ExternalProductData, ) -> Product | None:
    normalized_name = normalized(incoming.name)

    if not normalized_name:
        return None

    result = await session.execute(
        select(Product)
        .where(
            Product.brand_id == brand.id,
            Product.normalized_name == normalized_name,
            Product.is_active.is_(True),
        )
        .limit(10)
    )

    products = list(result.scalars().all())

    compatible = [
        product
        for product in products
        if product_package_compatible_with_incoming(
            product=product,
            incoming=incoming,
        )
    ]

    if len(compatible) != 1:
        return None

    return compatible[0]


async def find_safe_name_match( *, session: AsyncSession, incoming: ExternalProductData, incoming_brand: Brand, ) -> Product | None:
    normalized_name = normalized(incoming.name)

    if not normalized_name:
        return None

    result = await session.execute(
        select(Product)
        .where(
            Product.normalized_name == normalized_name,
            Product.is_active.is_(True),
        )
        .limit(10)
    )

    products = list(result.scalars().all())
    compatible_products: list[Product] = []

    for product in products:
        if not product_package_compatible_with_incoming(
            product=product,
            incoming=incoming,
        ):
            continue

        existing_brand = await get_brand_by_id(
            session=session,
            brand_id=product.brand_id,
        )

        existing_brand_name = (
            existing_brand.name
            if existing_brand is not None
            else None
        )

        if (
            not is_unknown_brand(existing_brand_name)
            and not is_unknown_brand(incoming_brand.name)
            and normalized(existing_brand_name)
            != normalized(incoming_brand.name)
        ):
            continue

        compatible_products.append(product)

    if len(compatible_products) != 1:
        return None

    return compatible_products[0]


async def find_matching_product( *, session: AsyncSession, incoming: ExternalProductData, brand: Brand, ) -> tuple[
    Product | None,
    ProductMatchType | None,
]:
    barcode_match = await find_product_by_barcode(
        session=session,
        barcode=incoming.barcode,
    )

    if barcode_match is not None:
        return barcode_match, ProductMatchType.BARCODE

    brand_name_match = await find_product_by_brand_and_name(
        session=session,
        brand=brand,
        incoming=incoming,
    )

    if brand_name_match is not None:
        return (
            brand_name_match,
            ProductMatchType.BRAND_AND_NAME,
        )

    if not is_generic_product_name(incoming.name):
        name_match = await find_safe_name_match(
            session=session,
            incoming=incoming,
            incoming_brand=brand,
        )

        if name_match is not None:
            return name_match, ProductMatchType.NAME

    return None, None


async def merge_product_fields( *, session: AsyncSession, product: Product, incoming_brand: Brand, incoming: ExternalProductData, ) -> tuple[
    list[str],
    list[str],
    Brand,
    Category | None,
]:
    updated_fields: list[str] = []
    conflicts: list[str] = []

    current_brand = await get_brand_by_id(
        session=session,
        brand_id=product.brand_id,
    )

    current_category = await get_category_by_id(
        session=session,
        category_id=product.category_id,
    )

    incoming_name = clean_text(incoming.name)

    if is_better_name(
        current_name=product.name,
        incoming_name=incoming_name,
    ):
        product.name = incoming_name
        product.normalized_name = normalized(incoming_name)
        updated_fields.append("name")

    normalized_barcode = normalize_barcode(incoming.barcode)
    current_barcode = normalize_barcode(product.barcode)

    if current_barcode is None and normalized_barcode:
        product.barcode = normalized_barcode
        updated_fields.append("barcode")

    elif (
        current_barcode
        and normalized_barcode
        and current_barcode != normalized_barcode
    ):
        conflicts.append(
            f"barcode_conflict:{current_barcode}!={normalized_barcode}"
        )

    incoming_package_value = normalize_package_value(
        incoming.package_value
    )
    incoming_package_unit = normalize_package_unit(
        incoming.package_unit
    )

    package_compatibility = package_values_compatible(
        current_value=product.package_value,
        current_unit=product.package_unit,
        incoming_value=incoming_package_value,
        incoming_unit=incoming_package_unit,
    )

    if package_compatibility is False:
        conflicts.append(
            "package_conflict:"
            f"{product.package_value}{product.package_unit or ''}"
            "!="
            f"{incoming_package_value}{incoming_package_unit or ''}"
        )
    else:
        if (
            product.package_value is None
            and incoming_package_value is not None
        ):
            product.package_value = incoming_package_value
            updated_fields.append("package_value")

        if (
            not product.package_unit
            and incoming_package_unit
        ):
            product.package_unit = incoming_package_unit
            updated_fields.append("package_unit")

    if should_fill_text(
        product.subtype,
        incoming.subtype,
    ):
        product.subtype = clean_text(incoming.subtype)
        updated_fields.append("subtype")

    elif (
        clean_text(product.subtype)
        and clean_text(incoming.subtype)
        and normalized(product.subtype)
        != normalized(incoming.subtype)
    ):
        conflicts.append(
            "subtype_conflict:"
            f"{clean_text(product.subtype)}"
            "!="
            f"{clean_text(incoming.subtype)}"
        )

    if should_replace_description(
        current_value=product.description,
        incoming_value=incoming.description,
    ):
        product.description = clean_text(
            incoming.description
        )
        updated_fields.append("description")

    if should_replace_image(
        current_value=product.image_url,
        incoming_value=incoming.image_url,
    ):
        product.image_url = clean_text(
            incoming.image_url
        )
        updated_fields.append("image_url")

    merged_keywords = combine_keywords(
        product.keywords,
        incoming.keywords,
    )

    if (
        merged_keywords
        and merged_keywords != product.keywords
    ):
        product.keywords = merged_keywords
        updated_fields.append("keywords")

    current_brand_name = (
        current_brand.name
        if current_brand is not None
        else None
    )

    if (
        not is_unknown_brand(incoming_brand.name)
        and (
            current_brand is None
            or is_unknown_brand(current_brand_name)
        )
        and product.brand_id != incoming_brand.id
    ):
        product.brand_id = incoming_brand.id
        current_brand = incoming_brand
        updated_fields.append("brand_id")

    elif (
        current_brand is not None
        and not is_unknown_brand(current_brand.name)
        and not is_unknown_brand(incoming_brand.name)
        and normalized(current_brand.name)
        != normalized(incoming_brand.name)
    ):
        conflicts.append(
            "brand_conflict:"
            f"{current_brand.name}"
            "!="
            f"{incoming_brand.name}"
        )

    if incoming.category_id is not None:
        incoming_category = await get_category_by_id(
            session=session,
            category_id=incoming.category_id,
        )

        if incoming_category is not None:
            current_category_name = (
                current_category.name
                if current_category is not None
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
                product.category_id = incoming_category.id
                current_category = incoming_category
                updated_fields.append("category_id")

            elif (
                current_category is not None
                and not is_generic_category(
                    current_category.name
                )
                and not is_generic_category(
                    incoming_category.name
                )
                and product.category_id
                != incoming_category.id
            ):
                conflicts.append(
                    "category_conflict:"
                    f"{current_category.name}"
                    "!="
                    f"{incoming_category.name}"
                )

    actual_brand = current_brand or incoming_brand

    new_search_text = build_search_text(
        product=product,
        brand=actual_brand,
        category=current_category,
    )

    if (
        clean_text(product.search_text)
        != clean_text(new_search_text)
    ):
        product.search_text = new_search_text
        updated_fields.append("search_text")

    return (
        updated_fields,
        conflicts,
        actual_brand,
        current_category,
    )


async def create_product( *, session: AsyncSession, incoming: ExternalProductData, brand: Brand, ) -> Product:
    if incoming.category_id is None:
        raise ValueError(
            "Невозможно создать новый товар "
            "без category_id."
        )

    product_name = clean_text(incoming.name)

    if not product_name:
        raise ValueError(
            "Невозможно создать товар "
            "без названия."
        )

    product = Product(
        name=product_name,
        normalized_name=normalized(product_name),
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
            clean_text(incoming.subtype)
            or None
        ),
        description=(
            clean_text(incoming.description)
            or None
        ),
        image_url=(
            clean_text(incoming.image_url)
            or None
        ),
        keywords=(
            clean_text(incoming.keywords)
            or None
        ),
        is_active=True,
    )

    session.add(product)
    await session.flush()

    category = await get_category_by_id(
        session=session,
        category_id=product.category_id,
    )

    product.search_text = build_search_text(
        product=product,
        brand=brand,
        category=category,
    )

    await session.flush()

    return product


async def merge_external_product( *, session: AsyncSession, incoming: ExternalProductData, commit: bool = False, ) -> ProductMergeResult:
    source = clean_text(incoming.source)

    if not source:
        raise ValueError(
            "Не указан источник товара."
        )

    if not clean_text(incoming.name):
        raise ValueError(
            "Не указано название товара."
        )

    incoming.source_priority = max(
        0,
        min(int(incoming.source_priority), 100),
    )
    incoming.confidence = max(
        0.0,
        min(float(incoming.confidence), 100.0),
    )

    incoming_brand = await get_or_create_brand(
        session=session,
        brand_name=incoming.brand_name,
    )

    product, match_type = await find_matching_product(
        session=session,
        incoming=incoming,
        brand=incoming_brand,
    )

    created = False
    result_brand = incoming_brand
    conflicts: tuple[str, ...] = ()

    if product is None:
        product = await create_product(
            session=session,
            incoming=incoming,
            brand=incoming_brand,
        )

        created = True
        match_type = ProductMatchType.CREATED

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
            conflict_list,
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
        conflicts = tuple(
            conflict_list
        )

    await session.flush()

    if conflicts:
        logger.warning(
            "Product merge conflicts: "
            "product_id=%s source=%s "
            "match=%s conflicts=%s",
            product.id,
            source,
            match_type,
            conflicts,
        )

    logger.info(
        "Product merge complete: "
        "product_id=%s source=%s "
        "created=%s match=%s "
        "updated=%s conflicts=%s",
        product.id,
        source,
        created,
        match_type,
        updated_fields,
        conflicts,
    )

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
        source=source,
        conflicts=conflicts,
    )
