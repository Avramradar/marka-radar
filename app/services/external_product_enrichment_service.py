import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.integrations.openfoodfacts_adapter import (
    import_openfoodfacts_product,
)
from app.services.product_merge_service import (
    ProductMergeResult,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExternalEnrichmentResult:
    """
    Результат внешнего обогащения товара.

    enriched:
        удалось ли получить хоть какие-то
        полезные изменения.

    provider:
        источник, который дал результат.

    merge_result:
        результат Product Merge Engine.
    """

    enriched: bool
    provider: str | None
    merge_result: ProductMergeResult | None


@dataclass(slots=True)
class ProductCompleteness:
    """
    Оценка полноты товарной карточки.

    Чем больше заполнено полезных полей,
    тем меньше смысла обращаться
    к дополнительным внешним источникам.
    """

    score: int

    has_name: bool
    has_specific_name: bool
    has_brand: bool
    has_category: bool
    has_package: bool
    has_image: bool
    has_description: bool
    has_subtype: bool

    @property
    def is_good_enough(self) -> bool:
        """
        Карточку считаем достаточно полной,
        если есть базовые идентифицирующие данные.
        """

        return (
            self.score >= 65
            and self.has_specific_name
            and self.has_brand
        )


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


GENERIC_PRODUCT_NAMES = {
    "",
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
    "продукт",
}


GENERIC_CATEGORY_NAMES = {
    "",
    "продукты",
    "продукт",
    "еда",
    "food",
    "foods",
    "product",
    "products",
    "прочее",
    "другое",
    "other",
}


def normalize_simple(
    value: object,
) -> str:
    """
    Простая нормализация
    для внутренних проверок.
    """

    return " ".join(
        str(
            value or ""
        )
        .strip()
        .lower()
        .replace(
            "ё",
            "е",
        )
        .split()
    )


def is_real_brand(
    brand_name: str | None,
) -> bool:
    """
    Проверяет, является ли бренд настоящим,
    а не служебным значением.
    """

    return (
        normalize_simple(
            brand_name
        )
        not in UNKNOWN_BRAND_NAMES
    )


def is_specific_name(
    product_name: str | None,
) -> bool:
    """
    Проверяет, что название конкретнее,
    чем просто:

        Кофе
        Молоко
        Пицца
    """

    return (
        normalize_simple(
            product_name
        )
        not in GENERIC_PRODUCT_NAMES
    )


def is_specific_category(
    category_name: str | None,
) -> bool:
    """
    Проверяет, что категория
    не является слишком общей.
    """

    return (
        normalize_simple(
            category_name
        )
        not in GENERIC_CATEGORY_NAMES
    )


def calculate_product_completeness(
    *,
    product: Product,
    brand: Brand,
    category: Category,
) -> ProductCompleteness:
    """
    Оценивает полноту карточки.

    Это НЕ рейтинг качества продукта.
    Только качество метаданных.
    """

    product_name = str(
        product.name or ""
    ).strip()

    brand_name = str(
        brand.name or ""
    ).strip()

    category_name = str(
        category.name or ""
    ).strip()

    has_name = bool(
        product_name
    )

    has_specific_name = (
        is_specific_name(
            product_name
        )
    )

    has_brand = (
        is_real_brand(
            brand_name
        )
    )

    has_category = (
        is_specific_category(
            category_name
        )
    )

    has_package = (
        product.package_value is not None
        and bool(
            product.package_unit
        )
    )

    has_image = bool(
        product.image_url
    )

    has_description = bool(
        product.description
    )

    has_subtype = bool(
        product.subtype
    )

    score = 0

    if has_name:
        score += 10

    if has_specific_name:
        score += 20

    if has_brand:
        score += 20

    if has_category:
        score += 15

    if has_package:
        score += 15

    if has_image:
        score += 10

    if has_description:
        score += 5

    if has_subtype:
        score += 5

    return ProductCompleteness(
        score=min(
            score,
            100,
        ),
        has_name=has_name,
        has_specific_name=(
            has_specific_name
        ),
        has_brand=has_brand,
        has_category=has_category,
        has_package=has_package,
        has_image=has_image,
        has_description=(
            has_description
        ),
        has_subtype=has_subtype,
    )


def merge_result_has_useful_changes(
    merge_result: ProductMergeResult,
) -> bool:
    """
    Проверяет, были ли полезные изменения.

    search_text сам по себе не считается
    реальным обогащением карточки.
    """

    useful_fields = {
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
    }

    return any(
        field
        in useful_fields
        for field
        in merge_result.updated_fields
    )


async def run_provider_safely(
    *,
    provider_name: str,
    provider: Callable[
        [],
        Awaitable[
            ProductMergeResult | None
        ],
    ],
) -> ProductMergeResult | None:
    """
    Безопасно запускает внешний источник.

    Ошибка одного провайдера не должна
    ломать поиск MarkaRadar.
    """

    try:
        result = await provider()

    except Exception:
        logger.exception(
            "Ошибка внешнего источника %s",
            provider_name,
        )

        return None

    if result is None:
        logger.info(
            "Внешний источник %s "
            "не нашёл товар",
            provider_name,
        )

        return None

    logger.info(
        "External enrichment provider=%s "
        "product_id=%s created=%s "
        "match=%s fields=%s",
        provider_name,
        result.product.id,
        result.created,
        result.match_type,
        result.updated_fields,
    )

    return result


async def enrich_from_openfoodfacts(
    *,
    session: AsyncSession,
    barcode: str,
) -> ProductMergeResult | None:
    """
    Первый внешний источник:
    Open Food Facts.
    """

    return await import_openfoodfacts_product(
        session=session,
        barcode=barcode,
        commit=False,
    )


async def enrich_product_by_barcode(
    *,
    session: AsyncSession,
    barcode: str,
    product: Product | None = None,
    brand: Brand | None = None,
    category: Category | None = None,
) -> ExternalEnrichmentResult:
    """
    Центральная точка внешнего обогащения.

    Сейчас подключён:

        1. OpenFoodFacts

    Позже сюда добавим:

        2. второй товарный источник;
        3. каталог производителя;
        4. разрешённые merchant feeds;
        5. другие API.

    Важно:

    Search Pipeline ничего не знает
    о конкретных внешних сервисах.
    """

    cleaned_barcode = "".join(
        character
        for character in str(
            barcode
        )
        if character.isdigit()
    )

    if not (
        8
        <= len(
            cleaned_barcode
        )
        <= 14
    ):
        return ExternalEnrichmentResult(
            enriched=False,
            provider=None,
            merge_result=None,
        )

    #
    # Если карточка уже хорошая,
    # вообще не обращаемся наружу.
    #

    if (
        product is not None
        and brand is not None
        and category is not None
    ):
        completeness = (
            calculate_product_completeness(
                product=product,
                brand=brand,
                category=category,
            )
        )

        logger.info(
            "Product completeness "
            "barcode=%s score=%s",
            cleaned_barcode,
            completeness.score,
        )

        if completeness.is_good_enough:
            return ExternalEnrichmentResult(
                enriched=False,
                provider=None,
                merge_result=None,
            )

    #
    # PROVIDER 1
    # OpenFoodFacts
    #

    off_result = await run_provider_safely(
        provider_name="openfoodfacts",
        provider=lambda: (
            enrich_from_openfoodfacts(
                session=session,
                barcode=cleaned_barcode,
            )
        ),
    )

    if off_result is not None:
        await session.flush()

        if (
            off_result.created
            or merge_result_has_useful_changes(
                off_result
            )
        ):
            return ExternalEnrichmentResult(
                enriched=True,
                provider="openfoodfacts",
                merge_result=off_result,
            )

    #
    # ВАЖНО
    #
    # Здесь специально оставляем место
    # для второго источника.
    #
    # Пример будущей логики:
    #
    # second_result = await run_provider_safely(
    #     provider_name="second_catalog",
    #     provider=lambda: (
    #         enrich_from_second_catalog(
    #             session=session,
    #             barcode=cleaned_barcode,
    #         )
    #     ),
    # )
    #
    # if second_result is not None:
    #     await session.flush()
    #
    #     if (
    #         second_result.created
    #         or merge_result_has_useful_changes(
    #             second_result
    #         )
    #     ):
    #         return ExternalEnrichmentResult(
    #             enriched=True,
    #             provider="second_catalog",
    #             merge_result=second_result,
    #         )

    return ExternalEnrichmentResult(
        enriched=False,
        provider=None,
        merge_result=off_result,
    )
