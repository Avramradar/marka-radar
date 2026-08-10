from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.services.image_validation_service import (
    ImageValidationResult,
    validate_external_image,
)
from app.services.product_completeness_service import (
    ProductCompletenessResult,
    evaluate_product_completeness,
    is_external_image_url,
)


logger = logging.getLogger(__name__)


@dataclass( slots=True, frozen=True, )
class ProductCardState:
    """ Полное текущее состояние карточки MarkaRadar. product: Канонический Product из БД. brand: Текущий Brand. category: Текущая Category. completeness: Оценка полноты карточки. image_validation: Результат проверки внешнего изображения. None, если изображения нет или проверка не требовалась. should_continue: Нужно ли продолжать обогащение следующими источниками. stop_reason: Почему процесс можно остановить или почему его нужно продолжать. """

    product: Product
    brand: Brand
    category: Category

    completeness: ProductCompletenessResult
    image_validation: ImageValidationResult | None

    should_continue: bool
    stop_reason: str


async def load_product_card( *, session: AsyncSession, product_id: int, ) -> tuple[
    Product,
    Brand,
    Category,
]:
    """ Загружает каноническую карточку одним запросом. Не используем lazy loading отношений, чтобы поведение было предсказуемым в AsyncSession. """

    result = await session.execute(
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Brand.id == Product.brand_id,
        )
        .join(
            Category,
            Category.id == Product.category_id,
        )
        .where(
            Product.id == product_id
        )
        .limit(
            1
        )
    )

    row = result.first()

    if row is None:
        raise ValueError(
            f"Товар product_id={product_id} "
            "не найден."
        )

    product, brand, category = row

    return (
        product,
        brand,
        category,
    )


def build_enrichment_query( *, product: Product, brand: Brand, ) -> str:
    """ Формирует безопасный запрос для следующего внешнего источника. Приоритет: реальный бренд + название + упаковка. Не добавляем description/keywords: они могут содержать мусор и ухудшить поиск. """

    values: list[str] = []

    brand_name = str(
        getattr(
            brand,
            "name",
            "",
        )
        or ""
    ).strip()

    product_name = str(
        getattr(
            product,
            "name",
            "",
        )
        or ""
    ).strip()

    if brand_name:
        values.append(
            brand_name
        )

    if product_name:
        values.append(
            product_name
        )

    package_value = getattr(
        product,
        "package_value",
        None,
    )

    package_unit = str(
        getattr(
            product,
            "package_unit",
            "",
        )
        or ""
    ).strip()

    if (
        package_value is not None
        and package_unit
    ):
        values.append(
            f"{package_value} {package_unit}"
        )

    query = " ".join(
        value
        for value in values
        if value
    )

    return " ".join(
        query.split()
    )


async def evaluate_product_card_state( *, session: AsyncSession, product_id: int, validate_image: bool = True, ) -> ProductCardState:
    """ Оценивает текущее состояние карточки. Последовательность: Product + Brand + Category ↓ первичная оценка полноты ↓ если есть внешний image_url — Image Validator ↓ повторная оценка с image_valid ↓ решение continue / stop Функция ничего не изменяет в БД. """

    (
        product,
        brand,
        category,
    ) = await load_product_card(
        session=session,
        product_id=product_id,
    )

    image_url = getattr(
        product,
        "image_url",
        None,
    )

    image_validation: (
        ImageValidationResult
        | None
    ) = None

    image_valid: bool | None = None

    if (
        validate_image
        and image_url
        and is_external_image_url(
            image_url
        )
    ):
        image_validation = (
            await validate_external_image(
                image_url=str(
                    image_url
                ),
            )
        )

        image_valid = (
            image_validation.valid
        )

        logger.info(
            "Product image validation: "
            "product_id=%s valid=%s "
            "reason=%s status=%s "
            "content_type=%s",
            product.id,
            image_validation.valid,
            image_validation.reason,
            image_validation.status_code,
            image_validation.content_type,
        )

    completeness = (
        evaluate_product_completeness(
            product=product,
            brand=brand,
            category=category,
            image_valid=image_valid,
        )
    )

    should_continue = (
        not completeness.is_complete
    )

    if completeness.is_complete:
        stop_reason = (
            "card_complete"
        )

    elif (
        completeness
        .critical_missing_fields
    ):
        stop_reason = (
            "critical_fields_missing:"
            + ",".join(
                completeness
                .critical_missing_fields
            )
        )

    elif completeness.next_priority_fields:
        stop_reason = (
            "fields_need_improvement:"
            + ",".join(
                completeness
                .next_priority_fields
            )
        )

    else:
        stop_reason = (
            "completeness_threshold_not_reached"
        )

    logger.info(
        "Product card completeness: "
        "product_id=%s score=%.1f "
        "identity=%.1f presentation=%.1f "
        "complete=%s continue=%s "
        "missing=%s weak=%s critical=%s "
        "next=%s",
        product.id,
        completeness.score,
        completeness.identity_score,
        completeness.presentation_score,
        completeness.is_complete,
        should_continue,
        completeness.missing_fields,
        completeness.weak_fields,
        completeness.critical_missing_fields,
        completeness.next_priority_fields,
    )

    return ProductCardState(
        product=product,
        brand=brand,
        category=category,
        completeness=completeness,
        image_validation=(
            image_validation
        ),
        should_continue=(
            should_continue
        ),
        stop_reason=stop_reason,
    )


def should_continue_enrichment( state: ProductCardState, ) -> bool:
    """ Единая функция принятия решения. В дальнейшем именно её будет вызывать External Catalog Orchestrator после каждого успешно обработанного источника. """

    return bool(
        state.should_continue
    )


def fields_needed_from_next_source( state: ProductCardState, ) -> tuple[
    str,
    ...
]:
    """ Возвращает поля, которые следующий источник должен попытаться найти в первую очередь. """

    return (
        state
        .completeness
        .next_priority_fields
    )


def completeness_log_payload( state: ProductCardState, ) -> dict[
    str,
    Any,
]:
    """ Удобный структурированный payload для логов и будущей диагностики. """

    return {
        "product_id": (
            state.product.id
        ),
        "score": (
            state.completeness.score
        ),
        "identity_score": (
            state
            .completeness
            .identity_score
        ),
        "presentation_score": (
            state
            .completeness
            .presentation_score
        ),
        "is_complete": (
            state
            .completeness
            .is_complete
        ),
        "should_continue": (
            state.should_continue
        ),
        "stop_reason": (
            state.stop_reason
        ),
        "missing_fields": (
            state
            .completeness
            .missing_fields
        ),
        "weak_fields": (
            state
            .completeness
            .weak_fields
        ),
        "critical_missing_fields": (
            state
            .completeness
            .critical_missing_fields
        ),
        "next_priority_fields": (
            state
            .completeness
            .next_priority_fields
        ),
        "image_validation": (
            (
                {
                    "valid": (
                        state
                        .image_validation
                        .valid
                    ),
                    "reason": (
                        state
                        .image_validation
                        .reason
                    ),
                    "status_code": (
                        state
                        .image_validation
                        .status_code
                    ),
                    "content_type": (
                        state
                        .image_validation
                        .content_type
                    ),
                }
            )
            if state.image_validation
            is not None
            else None
        ),
    }
