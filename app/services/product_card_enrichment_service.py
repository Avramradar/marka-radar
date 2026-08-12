from __future__ import annotations

# MARKARADAR TARGETED ENRICHMENT V4 VERIFIED — 2026-08-12
ENRICHMENT_BUILD_ID = "V4_TARGETED_DISPLAY_READY_2026_08_12"

import logging
from dataclasses import dataclass
from decimal import Decimal
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


@dataclass(
    slots=True,
    frozen=True,
)
class ProductCardState:
    """
    Полное текущее состояние карточки MarkaRadar.

    product:
        Канонический Product из БД.

    brand:
        Текущий Brand.

    category:
        Текущая Category.

    completeness:
        Оценка полноты карточки.

    image_validation:
        Результат проверки внешнего изображения.
        None, если изображения нет или проверка
        не требовалась.

    should_continue:
        Нужно ли продолжать обогащение
        следующими источниками.

    stop_reason:
        Почему процесс можно остановить
        или почему его нужно продолжать.
    """

    product: Product
    brand: Brand
    category: Category

    completeness: ProductCompletenessResult
    image_validation: ImageValidationResult | None

    display_ready: bool
    display_reason: str
    should_continue: bool
    stop_reason: str


async def load_product_card(
    *,
    session: AsyncSession,
    product_id: int,
) -> tuple[
    Product,
    Brand,
    Category,
]:
    """
    Загружает каноническую карточку одним запросом.

    Не используем lazy loading отношений,
    чтобы поведение было предсказуемым
    в AsyncSession.
    """

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
        .execution_options(
            populate_existing=True
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



def evaluate_display_readiness(
    *,
    product: Product,
    brand: Brand,
    completeness: ProductCompletenessResult,
    image_validation: ImageValidationResult | None,
) -> tuple[bool, str]:
    """
    Отдельный gate для пользовательского показа.

    Полная карточка (is_complete) и карточка, уже безопасная для показа,
    — разные состояния. Для display_ready обязательны:
      * уверенная идентификация SKU;
      * конкретное название и реальный бренд;
      * подтверждённое изображение;
      * barcode либо корректная упаковка как дополнительный identity-anchor.

    category / description / subtype / keywords могут продолжать
    дообогащаться позже и сами по себе не блокируют показ.
    """

    from app.services.product_completeness_service import (
        has_valid_package,
        is_real_brand,
        is_specific_product_name,
        normalize_barcode,
    )

    if not is_specific_product_name(getattr(product, "name", None)):
        return False, "name_not_specific"

    if not is_real_brand(getattr(brand, "name", None)):
        return False, "brand_not_confirmed"

    image_url = " ".join(
        str(getattr(product, "image_url", "") or "").strip().split()
    )
    if not image_url:
        return False, "image_missing"

    if is_external_image_url(image_url):
        if image_validation is None:
            return False, "image_not_validated"
        if not image_validation.valid:
            return False, "image_invalid"

    barcode = normalize_barcode(getattr(product, "barcode", None))
    package_ok = has_valid_package(
        value=getattr(product, "package_value", None),
        unit=getattr(product, "package_unit", None),
    )

    if not barcode and not package_ok:
        return False, "sku_anchor_missing"

    if float(completeness.identity_score) < 55.0:
        return False, "identity_score_low"

    return True, "identity_and_image_confirmed"


async def evaluate_product_card_state(
    *,
    session: AsyncSession,
    product_id: int,
    validate_image: bool = True,
) -> ProductCardState:
    """
    Оценивает текущее состояние карточки.

    Последовательность:

        Product + Brand + Category
        ↓
        первичная оценка полноты
        ↓
        если есть внешний image_url —
        Image Validator
        ↓
        повторная оценка с image_valid
        ↓
        решение continue / stop

    Функция ничего не изменяет в БД.
    """

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

    display_ready, display_reason = evaluate_display_readiness(
        product=product,
        brand=brand,
        completeness=completeness,
        image_validation=image_validation,
    )

    # Пользовательский enrichment прекращаем, когда карточка уже безопасна
    # для показа. Внутренний is_complete остаётся строгим и продолжает
    # отражать реальную полноту данных в базе.
    should_continue = (
        not completeness.is_complete
        and not display_ready
    )

    if completeness.is_complete:
        stop_reason = "card_complete"
    elif display_ready:
        stop_reason = "display_ready"
    elif completeness.critical_missing_fields:
        stop_reason = (
            "critical_fields_missing:"
            + ",".join(completeness.critical_missing_fields)
        )
    elif completeness.next_priority_fields:
        stop_reason = (
            "fields_need_improvement:"
            + ",".join(completeness.next_priority_fields)
        )
    else:
        stop_reason = "completeness_threshold_not_reached"

    logger.info(
        "Product card completeness: "
        "product_id=%s score=%.1f "
        "identity=%.1f presentation=%.1f "
        "complete=%s display_ready=%s display_reason=%s continue=%s "
        "missing=%s weak=%s critical=%s next=%s",
        product.id,
        completeness.score,
        completeness.identity_score,
        completeness.presentation_score,
        completeness.is_complete,
        display_ready,
        display_reason,
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
        image_validation=image_validation,
        display_ready=display_ready,
        display_reason=display_reason,
        should_continue=should_continue,
        stop_reason=stop_reason,
    )


def should_continue_enrichment(
    state: ProductCardState,
) -> bool:
    """
    Единая функция принятия решения.

    В дальнейшем именно её будет вызывать
    External Catalog Orchestrator после
    каждого успешно обработанного источника.
    """

    return bool(
        state.should_continue
    )


def fields_needed_from_next_source(
    state: ProductCardState,
) -> tuple[
    str,
    ...
]:
    """
    Возвращает поля, которые следующий источник
    должен попытаться найти в первую очередь.
    """

    return (
        state
        .completeness
        .next_priority_fields
    )


def completeness_log_payload(
    state: ProductCardState,
) -> dict[
    str,
    Any,
]:
    """
    Удобный структурированный payload
    для логов и будущей диагностики.
    """

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
        "display_ready": state.display_ready,
        "display_reason": state.display_reason,
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

def _format_query_number(value: Any) -> str:
    """Форматирует число упаковки без технических хвостов .000."""

    if value is None:
        return ""

    try:
        decimal_value = Decimal(str(value))
    except Exception:
        return " ".join(str(value).strip().split())

    if decimal_value == decimal_value.to_integral():
        return str(int(decimal_value))

    return format(decimal_value.normalize(), "f")


def build_targeted_enrichment_queries(
    *,
    product: Product,
    brand: Brand,
) -> tuple[str, ...]:
    """
    Формирует поисковые запросы для ОБОГАЩЕНИЯ УЖЕ ВЫБРАННОГО SKU.

    Первый запрос максимально точный: бренд + название + упаковка.
    Второй — fallback без упаковки, если каталог плохо индексирует вес.

    Важное отличие от общего каталожного поиска:
    результаты этих запросов НИКОГДА не создают новый Product.
    """

    brand_name = " ".join(
        str(getattr(brand, "name", "") or "").strip().split()
    )
    product_name = " ".join(
        str(getattr(product, "name", "") or "").strip().split()
    )

    base_parts = [
        value
        for value in (brand_name, product_name)
        if value
    ]

    if not base_parts:
        return ()

    base_query = " ".join(base_parts)
    queries: list[str] = []

    package_value = getattr(product, "package_value", None)
    package_unit = " ".join(
        str(getattr(product, "package_unit", "") or "").strip().split()
    )

    formatted_value = _format_query_number(package_value)

    if formatted_value and package_unit:
        queries.append(
            " ".join(
                f"{base_query} {formatted_value} {package_unit}".split()
            )
        )

    queries.append(base_query)

    # Сохраняем порядок и удаляем дубли.
    return tuple(dict.fromkeys(queries))


def _external_keywords_to_text(values: Any) -> str | None:
    if not values:
        return None

    if isinstance(values, str):
        cleaned = " ".join(values.strip().split())
        return cleaned or None

    cleaned_values = [
        " ".join(str(value or "").strip().split())
        for value in values
        if str(value or "").strip()
    ]

    if not cleaned_values:
        return None

    return ", ".join(dict.fromkeys(cleaned_values))


def _target_package_match(
    *,
    product: Product,
    external_product: Any,
) -> bool | None:
    """
    Более строгая проверка упаковки, чем обычный Merge Engine.

    Для targeted enrichment нельзя брать фото 240 г для карточки 245 г.
    Допуск только 0.5%, чтобы пережить техническое округление, но не
    объединять соседние SKU.
    """

    from app.services.product_merge_service import package_to_base

    current_family, current_value = package_to_base(
        getattr(product, "package_value", None),
        getattr(product, "package_unit", None),
    )
    incoming_family, incoming_value = package_to_base(
        getattr(external_product, "package_value", None),
        getattr(external_product, "package_unit", None),
    )

    if current_family is None or current_value is None:
        return None

    if incoming_family is None or incoming_value is None:
        return None

    if current_family != incoming_family:
        return False

    larger = max(current_value, incoming_value)
    if larger <= 0:
        return None

    difference_percent = (
        abs(current_value - incoming_value)
        / larger
        * Decimal("100")
    )

    return difference_percent <= Decimal("0.5")


def _target_candidate_identity(
    *,
    product: Product,
    brand: Brand,
    external_product: Any,
    needed_fields: tuple[str, ...],
) -> tuple[bool, float, str]:
    """
    Проверяет, можно ли использовать внешнюю карточку ТОЛЬКО для
    обогащения выбранного product_id.

    Здесь намеренно правила строже, чем у общего каталогового импорта:
    лучше пропустить фото, чем присвоить изображение соседнего SKU.
    """

    from app.services.product_merge_service import (
        identity_name_similarity,
        is_unknown_brand,
        normalize_barcode,
        normalized,
    )

    current_barcode = normalize_barcode(
        getattr(product, "barcode", None)
    )
    incoming_barcode = normalize_barcode(
        getattr(external_product, "barcode", None)
    )

    if (
        current_barcode
        and incoming_barcode
        and current_barcode != incoming_barcode
    ):
        return False, 0.0, "barcode_conflict"

    current_brand = normalized(
        getattr(brand, "name", None)
    )
    incoming_brand = normalized(
        getattr(external_product, "brand_name", None)
    )

    current_brand_is_real = not is_unknown_brand(
        getattr(brand, "name", None)
    )
    incoming_brand_is_real = not is_unknown_brand(
        getattr(external_product, "brand_name", None)
    )

    if (
        current_brand_is_real
        and incoming_brand_is_real
        and current_brand != incoming_brand
    ):
        return False, 0.0, "brand_conflict"

    package_match = _target_package_match(
        product=product,
        external_product=external_product,
    )

    if package_match is False:
        return False, 0.0, "package_conflict"

    coverage, jaccard, common_count = identity_name_similarity(
        getattr(product, "name", None),
        getattr(external_product, "name", None),
    )

    exact_barcode = bool(
        current_barcode
        and incoming_barcode
        and current_barcode == incoming_barcode
    )

    # Даже одинаковый штрихкод не должен автоматически протащить
    # совершенно чужое название: это защита от ошибочного barcode в БД.
    if exact_barcode:
        if common_count <= 0 and coverage < 0.5:
            return False, 0.0, "barcode_name_mismatch"

        score = 100.0
        if getattr(external_product, "image_url", None):
            score += 3.0
        if getattr(external_product, "description", None):
            score += 1.0
        return True, score, "exact_barcode"

    # Без точного barcode требуем реальный совпадающий бренд.
    if current_brand_is_real:
        if not incoming_brand_is_real:
            return False, 0.0, "missing_external_brand"
        if current_brand != incoming_brand:
            return False, 0.0, "brand_not_exact"
    elif not incoming_brand_is_real:
        return False, 0.0, "no_brand_evidence"

    # Если у канонической карточки упаковка известна, внешний кандидат
    # обязан содержать ту же упаковку. Отсутствие веса недостаточно.
    current_has_package = (
        getattr(product, "package_value", None) is not None
        and bool(getattr(product, "package_unit", None))
    )

    if current_has_package and package_match is not True:
        return False, 0.0, "package_not_confirmed"

    # Для текстового targeted-match нужны минимум два общих значимых
    # токена и высокая доля совпадения. Jaccard защищает от вариантов
    # вроде «с маслом / без масла / атлантическая / дальневосточная».
    if common_count < 2:
        return False, 0.0, "weak_name_common_tokens"

    if coverage < 0.80:
        return False, 0.0, "weak_name_coverage"

    if jaccard < 0.55:
        return False, 0.0, "weak_name_jaccard"

    score = (
        55.0
        + coverage * 20.0
        + jaccard * 15.0
        + (10.0 if package_match is True else 0.0)
    )

    if (
        "image_url" in needed_fields
        and getattr(external_product, "image_url", None)
    ):
        score += 5.0

    if (
        "description" in needed_fields
        and getattr(external_product, "description", None)
    ):
        score += 2.0

    return True, score, "brand_name_package"


async def _merge_external_into_target(
    *,
    session: AsyncSession,
    state: ProductCardState,
    external_product: Any,
    identity_reason: str,
) -> tuple[str, ...]:
    """
    Обогащает только state.product.

    НИКОГДА не вызывает общий merge_external_product(), поэтому в режиме
    открытия карточки не может создать соседний Product.
    """

    from app.services.product_merge_service import (
        ExternalProductData,
        ensure_product_source,
        get_or_create_brand,
        get_product_source,
        is_unknown_brand,
        merge_product_fields,
    )

    provider_name = " ".join(
        str(getattr(external_product, "provider", "") or "").strip().split()
    )
    source_id = " ".join(
        str(getattr(external_product, "source_id", "") or "").strip().split()
    )

    # Если эта внешняя карточка уже навсегда связана с другим Product,
    # targeted enrichment не имеет права перепривязывать её сам.
    if provider_name and source_id:
        existing_source = await get_product_source(
            session=session,
            source=provider_name,
            source_id=source_id,
        )
        if (
            existing_source is not None
            and int(existing_source.product_id) != int(state.product.id)
        ):
            logger.warning(
                "Targeted enrichment source rejected: "
                "provider=%s source_id=%s target_product_id=%s "
                "linked_product_id=%s",
                provider_name,
                source_id,
                state.product.id,
                existing_source.product_id,
            )
            return ()

    # Сначала проверяем фото самого кандидата. Битую ссылку в каноническую
    # карточку не записываем.
    incoming_image = " ".join(
        str(getattr(external_product, "image_url", "") or "").strip().split()
    ) or None

    if incoming_image and is_external_image_url(incoming_image):
        try:
            validation = await validate_external_image(
                image_url=incoming_image
            )
        except Exception:
            logger.exception(
                "Targeted candidate image validation failed: "
                "provider=%s source_id=%s",
                provider_name,
                source_id,
            )
            incoming_image = None
        else:
            if not validation.valid:
                logger.info(
                    "Targeted candidate image rejected: "
                    "provider=%s source_id=%s reason=%s status=%s",
                    provider_name,
                    source_id,
                    validation.reason,
                    validation.status_code,
                )
                incoming_image = None

    target_brand_is_unknown = is_unknown_brand(
        getattr(state.brand, "name", None)
    )
    external_brand_name = getattr(
        external_product,
        "brand_name",
        None,
    )

    incoming_brand = state.brand
    safe_brand_name = getattr(state.brand, "name", None)

    if (
        target_brand_is_unknown
        and external_brand_name
        and not is_unknown_brand(external_brand_name)
    ):
        incoming_brand = await get_or_create_brand(
            session=session,
            brand_name=external_brand_name,
        )
        safe_brand_name = external_brand_name

    # Уже известные identity-поля сохраняем как канонические. Внешний
    # источник в targeted mode в первую очередь дополняет presentation.
    safe_name = (
        getattr(state.product, "name", None)
        or getattr(external_product, "name", None)
    )
    safe_barcode = (
        getattr(state.product, "barcode", None)
        or getattr(external_product, "barcode", None)
    )
    safe_package_value = (
        getattr(state.product, "package_value", None)
        if getattr(state.product, "package_value", None) is not None
        else getattr(external_product, "package_value", None)
    )
    safe_package_unit = (
        getattr(state.product, "package_unit", None)
        or getattr(external_product, "package_unit", None)
    )

    incoming = ExternalProductData(
        source=provider_name,
        source_id=source_id or None,
        source_url=(
            " ".join(
                str(getattr(external_product, "source_url", "") or "")
                .strip()
                .split()
            )
            or None
        ),
        name=str(safe_name or ""),
        brand_name=safe_brand_name,
        barcode=safe_barcode,
        category_id=None,
        family_id=None,
        package_value=safe_package_value,
        package_unit=safe_package_unit,
        subtype=getattr(external_product, "subtype", None),
        description=getattr(external_product, "description", None),
        image_url=incoming_image,
        keywords=_external_keywords_to_text(
            getattr(external_product, "keywords", None)
        ),
        confidence=100.0,
    )

    updated_fields, conflicts, _, _ = await merge_product_fields(
        session=session,
        product=state.product,
        incoming_brand=incoming_brand,
        incoming=incoming,
    )

    if conflicts:
        logger.info(
            "Targeted enrichment merge conflicts: "
            "product_id=%s provider=%s source_id=%s "
            "identity=%s conflicts=%s",
            state.product.id,
            provider_name,
            source_id,
            identity_reason,
            tuple(conflicts),
        )

    if provider_name and source_id:
        await ensure_product_source(
            session=session,
            product=state.product,
            incoming=incoming,
        )

    await session.flush()

    logger.info(
        "Targeted enrichment merged: "
        "product_id=%s provider=%s source_id=%s "
        "identity=%s updated=%s",
        state.product.id,
        provider_name,
        source_id,
        identity_reason,
        tuple(updated_fields),
    )

    return tuple(updated_fields)


async def _clear_invalid_target_image(
    *,
    session: AsyncSession,
    state: ProductCardState,
) -> ProductCardState:
    """Удаляет из карточки подтверждённо битый внешний image_url."""

    validation = state.image_validation

    if (
        validation is None
        or validation.valid
        or not getattr(state.product, "image_url", None)
    ):
        return state

    logger.info(
        "Invalid canonical image cleared: product_id=%s reason=%s status=%s",
        state.product.id,
        validation.reason,
        validation.status_code,
    )

    state.product.image_url = None
    await session.commit()

    return await evaluate_product_card_state(
        session=session,
        product_id=int(state.product.id),
        validate_image=True,
    )


async def _try_targeted_external_product(
    *,
    session: AsyncSession,
    state: ProductCardState,
    external_product: Any,
    needed_fields: tuple[str, ...],
) -> tuple[ProductCardState, bool]:
    accepted, score, reason = _target_candidate_identity(
        product=state.product,
        brand=state.brand,
        external_product=external_product,
        needed_fields=needed_fields,
    )

    provider_name = getattr(external_product, "provider", None)
    source_id = getattr(external_product, "source_id", None)

    if not accepted:
        logger.info(
            "Targeted candidate rejected: "
            "product_id=%s provider=%s source_id=%s "
            "name=%r barcode=%r package=%r%s reason=%s",
            state.product.id,
            provider_name,
            source_id,
            getattr(external_product, "name", None),
            getattr(external_product, "barcode", None),
            getattr(external_product, "package_value", None),
            getattr(external_product, "package_unit", None) or "",
            reason,
        )
        return state, False

    logger.info(
        "Targeted candidate accepted: "
        "product_id=%s provider=%s source_id=%s score=%.1f "
        "reason=%s name=%r",
        state.product.id,
        provider_name,
        source_id,
        score,
        reason,
        getattr(external_product, "name", None),
    )

    updated_fields = await _merge_external_into_target(
        session=session,
        state=state,
        external_product=external_product,
        identity_reason=reason,
    )

    if updated_fields:
        await session.commit()
    else:
        # ensure_product_source() мог создать provenance даже когда новых
        # полей нет; сохраняем такую подтверждённую связь тоже.
        await session.commit()

    refreshed = await evaluate_product_card_state(
        session=session,
        product_id=int(state.product.id),
        validate_image=True,
    )

    return refreshed, bool(updated_fields)


async def ensure_product_card_enriched(
    *,
    session: AsyncSession,
    product_id: int,
    limit_per_provider: int = 8,
) -> ProductCardState:
    """
    Targeted enrichment выбранного товара.

    Главный инвариант:
        этот процесс может улучшать ТОЛЬКО переданный product_id и не
        имеет права создавать новые товары из поисковой выдачи провайдера.

    Порядок:
        локальная карточка -> проверка полноты -> barcode lookup каждого
        провайдера -> строгий текстовый поиск -> merge только в target ->
        повторная проверка -> остановка после полной карточки.
    """

    safe_limit = max(1, min(int(limit_per_provider), 20))

    state = await evaluate_product_card_state(
        session=session,
        product_id=int(product_id),
        validate_image=True,
    )
    state = await _clear_invalid_target_image(
        session=session,
        state=state,
    )

    logger.info(
        "Card enrichment start: product_id=%s "
        "score=%.1f complete=%s display_ready=%s display_reason=%s "
        "missing=%s weak=%s next=%s",
        product_id,
        state.completeness.score,
        state.completeness.is_complete,
        state.display_ready,
        state.display_reason,
        state.completeness.missing_fields,
        state.completeness.weak_fields,
        state.completeness.next_priority_fields,
    )

    if not should_continue_enrichment(state):
        logger.info(
            "Card enrichment skipped: product_id=%s reason=%s",
            product_id,
            state.stop_reason,
        )
        return state

    from app.services.external_catalog_service import (
        get_external_catalog_service,
    )

    catalog_service = get_external_catalog_service()
    barcode = "".join(
        char
        for char in str(getattr(state.product, "barcode", "") or "")
        if char.isdigit()
    )

    # ----------------------------------------------------------
    # 1. BARCODE LOOKUP: источник возвращает карточку, но мы сами
    #    проверяем её и применяем только к выбранному product_id.
    # ----------------------------------------------------------
    if barcode:
        for provider in catalog_service.providers:
            if not should_continue_enrichment(state):
                return state

            provider_name = str(
                getattr(provider, "provider_name", "") or ""
            ).strip()

            logger.info(
                "Card enrichment barcode start: product_id=%s "
                "provider=%s barcode=%s needed=%s",
                product_id,
                provider_name,
                barcode,
                fields_needed_from_next_source(state),
            )

            try:
                external_product = await provider.get_by_barcode(barcode)
            except Exception:
                logger.exception(
                    "Targeted barcode provider failed: "
                    "product_id=%s provider=%s barcode=%s",
                    product_id,
                    provider_name,
                    barcode,
                )
                continue

            if external_product is None:
                logger.info(
                    "Targeted barcode provider empty: "
                    "product_id=%s provider=%s barcode=%s",
                    product_id,
                    provider_name,
                    barcode,
                )
                continue

            state, _ = await _try_targeted_external_product(
                session=session,
                state=state,
                external_product=external_product,
                needed_fields=fields_needed_from_next_source(state),
            )

            logger.info(
                "Card enrichment after barcode: "
                "product_id=%s provider=%s score=%.1f complete=%s "
                "display_ready=%s display_reason=%s missing=%s next=%s",
                product_id,
                provider_name,
                state.completeness.score,
                state.completeness.is_complete,
                state.display_ready,
                state.display_reason,
                state.completeness.missing_fields,
                state.completeness.next_priority_fields,
            )

            if state.display_ready and not state.completeness.is_complete:
                logger.info(
                    "Card enrichment display-ready after barcode: "
                    "product_id=%s provider=%s reason=%s",
                    product_id,
                    provider_name,
                    state.display_reason,
                )

    if not should_continue_enrichment(state):
        return state

    # ----------------------------------------------------------
    # 2. STRICT TEXT LOOKUP. В выдаче выбираем только кандидата,
    #    доказанно относящегося к target SKU. Остальные игнорируются
    #    и НЕ импортируются в каталог.
    # ----------------------------------------------------------
    queries = build_targeted_enrichment_queries(
        product=state.product,
        brand=state.brand,
    )

    if not queries:
        logger.info(
            "Card enrichment stopped: product_id=%s reason=no_query",
            product_id,
        )
        return state

    for provider in catalog_service.providers:
        if not should_continue_enrichment(state):
            break

        provider_name = str(
            getattr(provider, "provider_name", "") or ""
        ).strip()

        provider_merged = False

        for query in queries:
            if not should_continue_enrichment(state):
                break

            needed_fields = fields_needed_from_next_source(state)

            logger.info(
                "Card enrichment provider start: "
                "product_id=%s provider=%s query=%r needed=%s",
                product_id,
                provider_name,
                query,
                needed_fields,
            )

            try:
                result = await provider.search(
                    query,
                    limit=safe_limit,
                )
            except Exception:
                logger.exception(
                    "Targeted provider search failed: "
                    "product_id=%s provider=%s query=%r",
                    product_id,
                    provider_name,
                    query,
                )
                continue

            if result.unavailable:
                logger.info(
                    "Targeted provider unavailable: "
                    "product_id=%s provider=%s query=%r error=%r",
                    product_id,
                    provider_name,
                    query,
                    result.error,
                )
                continue

            accepted_candidates: list[tuple[float, Any, str]] = []

            for external_product in result.products:
                accepted, score, reason = _target_candidate_identity(
                    product=state.product,
                    brand=state.brand,
                    external_product=external_product,
                    needed_fields=needed_fields,
                )

                if accepted:
                    accepted_candidates.append(
                        (score, external_product, reason)
                    )
                else:
                    logger.info(
                        "Targeted candidate rejected: "
                        "product_id=%s provider=%s source_id=%s "
                        "name=%r barcode=%r package=%r%s reason=%s",
                        product_id,
                        provider_name,
                        getattr(external_product, "source_id", None),
                        getattr(external_product, "name", None),
                        getattr(external_product, "barcode", None),
                        getattr(external_product, "package_value", None),
                        getattr(external_product, "package_unit", None) or "",
                        reason,
                    )

            if not accepted_candidates:
                logger.info(
                    "Targeted provider no safe candidate: "
                    "product_id=%s provider=%s query=%r found=%s",
                    product_id,
                    provider_name,
                    query,
                    len(result.products),
                )
                continue

            accepted_candidates.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            score, best_candidate, reason = accepted_candidates[0]

            logger.info(
                "Targeted provider best candidate: "
                "product_id=%s provider=%s source_id=%s "
                "score=%.1f reason=%s name=%r",
                product_id,
                provider_name,
                getattr(best_candidate, "source_id", None),
                score,
                reason,
                getattr(best_candidate, "name", None),
            )

            state, changed = await _try_targeted_external_product(
                session=session,
                state=state,
                external_product=best_candidate,
                needed_fields=needed_fields,
            )

            logger.info(
                "Card enrichment after provider: "
                "product_id=%s provider=%s score=%.1f "
                "identity=%.1f presentation=%.1f complete=%s "
                "display_ready=%s display_reason=%s "
                "missing=%s weak=%s critical=%s next=%s changed=%s",
                product_id,
                provider_name,
                state.completeness.score,
                state.completeness.identity_score,
                state.completeness.presentation_score,
                state.completeness.is_complete,
                state.display_ready,
                state.display_reason,
                state.completeness.missing_fields,
                state.completeness.weak_fields,
                state.completeness.critical_missing_fields,
                state.completeness.next_priority_fields,
                changed,
            )

            provider_merged = True
            break

        if provider_merged and not should_continue_enrichment(state):
            logger.info(
                "Card enrichment stop after provider: "
                "product_id=%s provider=%s score=%.1f "
                "complete=%s display_ready=%s reason=%s",
                product_id,
                provider_name,
                state.completeness.score,
                state.completeness.is_complete,
                state.display_ready,
                state.stop_reason,
            )
            break

    if should_continue_enrichment(state):
        logger.info(
            "Card enrichment sources exhausted: "
            "product_id=%s score=%.1f missing=%s weak=%s "
            "critical=%s next=%s",
            product_id,
            state.completeness.score,
            state.completeness.missing_fields,
            state.completeness.weak_fields,
            state.completeness.critical_missing_fields,
            state.completeness.next_priority_fields,
        )

    return state
