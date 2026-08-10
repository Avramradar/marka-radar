from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.price import PriceObservation
from app.database.models.product import Product
from app.database.models.product_alias import ProductAlias
from app.database.models.product_source import ProductSource
from app.database.models.rating import Rating
from app.database.models.review import Review
from app.database.models.search_history import SearchHistory
from app.services.product_merge_service import (
    build_search_text,
    clean_text,
    combine_keywords,
    get_brand_by_id,
    get_category_by_id,
    identity_name_similarity,
    is_better_name,
    is_generic_category,
    is_unknown_brand,
    normalize_barcode,
    normalize_package_unit,
    normalize_package_value,
    normalized,
    package_values_compatible,
    should_replace_description,
    should_replace_image,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class ProductConsolidationResult:
    canonical_product_id: int
    duplicate_product_id: int

    applied: bool
    dry_run: bool

    updated_fields: tuple[str, ...]

    moved_sources: int
    moved_prices: int
    moved_ratings: int
    moved_reviews: int
    moved_search_history: int
    moved_aliases: int

    removed_rating_conflicts: int
    removed_review_conflicts: int

    aliases_added: int

    conflicts: tuple[str, ...]
    blocked_reasons: tuple[str, ...]


def _dedupe_strings(
    values: list[str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            value
            for value in values
            if value
        )
    )


async def _load_product(
    *,
    session: AsyncSession,
    product_id: int,
) -> Product | None:
    result = await session.execute(
        select(Product)
        .where(
            Product.id == int(product_id)
        )
        .limit(1)
    )

    return result.scalar_one_or_none()


async def _identity_safety_check(
    *,
    session: AsyncSession,
    canonical: Product,
    duplicate: Product,
) -> tuple[list[str], list[str]]:
    """
    Проверяет безопасность объединения.

    Жёсткие блокировки:
    - разные известные barcode;
    - конфликтующая известная упаковка;
    - разные реальные бренды;
    - разные конкретные категории;
    - слишком слабое совпадение названий.

    Последняя блокировка может быть снята только
    через confirmed_identity=True.
    """

    blocked: list[str] = []
    conflicts: list[str] = []

    canonical_barcode = normalize_barcode(
        canonical.barcode
    )
    duplicate_barcode = normalize_barcode(
        duplicate.barcode
    )

    if (
        canonical_barcode
        and duplicate_barcode
        and canonical_barcode != duplicate_barcode
    ):
        blocked.append(
            "barcode_conflict:"
            f"{canonical_barcode}"
            "!="
            f"{duplicate_barcode}"
        )

    package_compatibility = package_values_compatible(
        current_value=canonical.package_value,
        current_unit=canonical.package_unit,
        incoming_value=duplicate.package_value,
        incoming_unit=duplicate.package_unit,
    )

    if package_compatibility is False:
        blocked.append(
            "package_conflict:"
            f"{canonical.package_value}"
            f"{canonical.package_unit or ''}"
            "!="
            f"{duplicate.package_value}"
            f"{duplicate.package_unit or ''}"
        )

    canonical_brand = await get_brand_by_id(
        session=session,
        brand_id=canonical.brand_id,
    )

    duplicate_brand = await get_brand_by_id(
        session=session,
        brand_id=duplicate.brand_id,
    )

    canonical_brand_name = (
        canonical_brand.name
        if canonical_brand is not None
        else None
    )

    duplicate_brand_name = (
        duplicate_brand.name
        if duplicate_brand is not None
        else None
    )

    if (
        not is_unknown_brand(canonical_brand_name)
        and not is_unknown_brand(duplicate_brand_name)
        and normalized(canonical_brand_name)
        != normalized(duplicate_brand_name)
    ):
        blocked.append(
            "brand_conflict:"
            f"{canonical_brand_name}"
            "!="
            f"{duplicate_brand_name}"
        )

    canonical_category = await get_category_by_id(
        session=session,
        category_id=canonical.category_id,
    )

    duplicate_category = await get_category_by_id(
        session=session,
        category_id=duplicate.category_id,
    )

    if (
        canonical_category is not None
        and duplicate_category is not None
        and not is_generic_category(
            canonical_category.name
        )
        and not is_generic_category(
            duplicate_category.name
        )
        and canonical_category.id
        != duplicate_category.id
    ):
        blocked.append(
            "category_conflict:"
            f"{canonical_category.name}"
            "!="
            f"{duplicate_category.name}"
        )

    (
        coverage,
        jaccard,
        common_count,
    ) = identity_name_similarity(
        canonical.name,
        duplicate.name,
    )

    if common_count < 2:
        blocked.append(
            "name_similarity_too_weak:"
            f"common={common_count}"
        )

    elif (
        coverage < 0.75
        or jaccard < 0.45
    ):
        blocked.append(
            "name_similarity_too_weak:"
            f"coverage={coverage:.3f},"
            f"jaccard={jaccard:.3f}"
        )

    if package_compatibility is None:
        conflicts.append(
            "package_not_confirmed"
        )

    return blocked, conflicts


async def _merge_card_fields(
    *,
    session: AsyncSession,
    canonical: Product,
    duplicate: Product,
) -> tuple[list[str], list[str]]:
    updated_fields: list[str] = []
    conflicts: list[str] = []

    canonical_brand = await get_brand_by_id(
        session=session,
        brand_id=canonical.brand_id,
    )

    duplicate_brand = await get_brand_by_id(
        session=session,
        brand_id=duplicate.brand_id,
    )

    canonical_category = await get_category_by_id(
        session=session,
        category_id=canonical.category_id,
    )

    duplicate_category = await get_category_by_id(
        session=session,
        category_id=duplicate.category_id,
    )

    # NAME
    if is_better_name(
        current_name=canonical.name,
        incoming_name=duplicate.name,
    ):
        canonical.name = clean_text(
            duplicate.name
        )
        canonical.normalized_name = normalized(
            duplicate.name
        )
        updated_fields.append("name")

    # BRAND
    if (
        canonical_brand is not None
        and is_unknown_brand(
            canonical_brand.name
        )
        and duplicate_brand is not None
        and not is_unknown_brand(
            duplicate_brand.name
        )
    ):
        canonical.brand_id = duplicate_brand.id
        canonical_brand = duplicate_brand
        updated_fields.append("brand_id")

    # CATEGORY
    if (
        canonical_category is not None
        and is_generic_category(
            canonical_category.name
        )
        and duplicate_category is not None
        and not is_generic_category(
            duplicate_category.name
        )
    ):
        canonical.category_id = (
            duplicate_category.id
        )
        canonical_category = (
            duplicate_category
        )
        updated_fields.append("category_id")

    # FAMILY
    if (
        canonical.family_id is None
        and duplicate.family_id is not None
    ):
        canonical.family_id = (
            duplicate.family_id
        )
        updated_fields.append("family_id")

    # BARCODE
    canonical_barcode = normalize_barcode(
        canonical.barcode
    )
    duplicate_barcode = normalize_barcode(
        duplicate.barcode
    )

    if (
        canonical_barcode is None
        and duplicate_barcode
    ):
        canonical.barcode = duplicate_barcode
        updated_fields.append("barcode")

    # PACKAGE
    package_compatibility = package_values_compatible(
        current_value=canonical.package_value,
        current_unit=canonical.package_unit,
        incoming_value=duplicate.package_value,
        incoming_unit=duplicate.package_unit,
    )

    if package_compatibility is not False:
        duplicate_package_value = (
            normalize_package_value(
                duplicate.package_value
            )
        )

        duplicate_package_unit = (
            normalize_package_unit(
                duplicate.package_unit
            )
        )

        if (
            canonical.package_value is None
            and duplicate_package_value is not None
        ):
            canonical.package_value = (
                duplicate_package_value
            )
            updated_fields.append(
                "package_value"
            )

        if (
            not canonical.package_unit
            and duplicate_package_unit
        ):
            canonical.package_unit = (
                duplicate_package_unit
            )
            updated_fields.append(
                "package_unit"
            )

    # SUBTYPE
    if (
        not clean_text(canonical.subtype)
        and clean_text(duplicate.subtype)
    ):
        canonical.subtype = clean_text(
            duplicate.subtype
        )
        updated_fields.append("subtype")

    elif (
        clean_text(canonical.subtype)
        and clean_text(duplicate.subtype)
        and normalized(canonical.subtype)
        != normalized(duplicate.subtype)
    ):
        conflicts.append(
            "subtype_conflict:"
            f"{clean_text(canonical.subtype)}"
            "!="
            f"{clean_text(duplicate.subtype)}"
        )

    # DESCRIPTION
    if should_replace_description(
        current_value=canonical.description,
        incoming_value=duplicate.description,
    ):
        canonical.description = clean_text(
            duplicate.description
        )
        updated_fields.append("description")

    # IMAGE
    if should_replace_image(
        current_value=canonical.image_url,
        incoming_value=duplicate.image_url,
    ):
        canonical.image_url = clean_text(
            duplicate.image_url
        )
        updated_fields.append("image_url")

    # KEYWORDS
    merged_keywords = combine_keywords(
        canonical.keywords,
        duplicate.keywords,
    )

    if (
        merged_keywords
        and merged_keywords
        != canonical.keywords
    ):
        canonical.keywords = merged_keywords
        updated_fields.append("keywords")

    actual_brand = (
        canonical_brand
        or duplicate_brand
    )

    if actual_brand is None:
        raise ValueError(
            "Не удалось определить Brand "
            "канонического товара."
        )

    new_search_text = build_search_text(
        product=canonical,
        brand=actual_brand,
        category=canonical_category,
    )

    if (
        clean_text(canonical.search_text)
        != clean_text(new_search_text)
    ):
        canonical.search_text = new_search_text
        updated_fields.append("search_text")

    return updated_fields, conflicts


async def _ensure_alias(
    *,
    session: AsyncSession,
    product_id: int,
    alias: str | None,
) -> bool:
    cleaned_alias = clean_text(alias)

    if not cleaned_alias:
        return False

    normalized_alias = normalized(
        cleaned_alias
    )

    if not normalized_alias:
        return False

    existing = await session.execute(
        select(ProductAlias)
        .where(
            ProductAlias.product_id
            == product_id,
            ProductAlias.normalized_alias
            == normalized_alias,
        )
        .limit(1)
    )

    if (
        existing.scalar_one_or_none()
        is not None
    ):
        return False

    session.add(
        ProductAlias(
            product_id=product_id,
            alias=cleaned_alias,
            normalized_alias=normalized_alias,
        )
    )

    await session.flush()

    return True


async def _move_aliases(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> tuple[int, int]:
    result = await session.execute(
        select(ProductAlias)
        .where(
            ProductAlias.product_id
            == duplicate_product_id
        )
        .order_by(ProductAlias.id.asc())
    )

    duplicate_aliases = list(
        result.scalars().all()
    )

    moved_count = 0
    created_count = 0

    for alias_row in duplicate_aliases:
        exists = await session.execute(
            select(ProductAlias)
            .where(
                ProductAlias.product_id
                == canonical_product_id,
                ProductAlias.normalized_alias
                == alias_row.normalized_alias,
            )
            .limit(1)
        )

        if (
            exists.scalar_one_or_none()
            is not None
        ):
            await session.delete(alias_row)
            continue

        alias_row.product_id = (
            canonical_product_id
        )
        moved_count += 1

    await session.flush()

    return moved_count, created_count


async def _move_sources(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> int:
    result = await session.execute(
        update(ProductSource)
        .where(
            ProductSource.product_id
            == duplicate_product_id
        )
        .values(
            product_id=canonical_product_id
        )
    )

    return int(result.rowcount or 0)


async def _move_prices(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> int:
    result = await session.execute(
        update(PriceObservation)
        .where(
            PriceObservation.product_id
            == duplicate_product_id
        )
        .values(
            product_id=canonical_product_id
        )
    )

    return int(result.rowcount or 0)


async def _move_search_history(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> int:
    result = await session.execute(
        update(SearchHistory)
        .where(
            SearchHistory.selected_product_id
            == duplicate_product_id
        )
        .values(
            selected_product_id=(
                canonical_product_id
            )
        )
    )

    return int(result.rowcount or 0)


async def _move_ratings(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> tuple[int, int]:
    canonical_result = await session.execute(
        select(Rating.user_id)
        .where(
            Rating.product_id
            == canonical_product_id
        )
    )

    canonical_users = set(
        canonical_result.scalars().all()
    )

    duplicate_result = await session.execute(
        select(Rating)
        .where(
            Rating.product_id
            == duplicate_product_id
        )
        .order_by(Rating.id.asc())
    )

    duplicate_ratings = list(
        duplicate_result.scalars().all()
    )

    moved = 0
    removed_conflicts = 0

    for rating in duplicate_ratings:
        if rating.user_id in canonical_users:
            await session.delete(rating)
            removed_conflicts += 1
            continue

        rating.product_id = (
            canonical_product_id
        )

        canonical_users.add(
            rating.user_id
        )

        moved += 1

    await session.flush()

    return moved, removed_conflicts


async def _move_reviews(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
) -> tuple[int, int]:
    canonical_result = await session.execute(
        select(Review.user_id)
        .where(
            Review.product_id
            == canonical_product_id
        )
    )

    canonical_users = set(
        canonical_result.scalars().all()
    )

    duplicate_result = await session.execute(
        select(Review)
        .where(
            Review.product_id
            == duplicate_product_id
        )
        .order_by(Review.id.asc())
    )

    duplicate_reviews = list(
        duplicate_result.scalars().all()
    )

    moved = 0
    removed_conflicts = 0

    for review in duplicate_reviews:
        if review.user_id in canonical_users:
            await session.delete(review)
            removed_conflicts += 1
            continue

        review.product_id = (
            canonical_product_id
        )

        canonical_users.add(
            review.user_id
        )

        moved += 1

    await session.flush()

    return moved, removed_conflicts


async def consolidate_products(
    *,
    session: AsyncSession,
    canonical_product_id: int,
    duplicate_product_id: int,
    dry_run: bool = True,
    commit: bool = False,
    confirmed_identity: bool = False,
) -> ProductConsolidationResult:
    """
    Безопасно объединяет две существующие карточки.

    confirmed_identity=True означает, что оператор
    вручную подтвердил: это один SKU.

    В этом режиме разрешается снять ТОЛЬКО блокировку
    name_similarity_too_weak.

    Barcode/package/brand/category конфликты
    остаются жёсткими блокировками.
    """

    canonical_product_id = int(
        canonical_product_id
    )
    duplicate_product_id = int(
        duplicate_product_id
    )

    if (
        canonical_product_id
        == duplicate_product_id
    ):
        raise ValueError(
            "Нельзя консолидировать Product "
            "сам с собой."
        )

    canonical = await _load_product(
        session=session,
        product_id=canonical_product_id,
    )

    duplicate = await _load_product(
        session=session,
        product_id=duplicate_product_id,
    )

    if canonical is None:
        raise ValueError(
            "Канонический Product "
            f"{canonical_product_id} не найден."
        )

    if duplicate is None:
        raise ValueError(
            "Product-дубль "
            f"{duplicate_product_id} не найден."
        )

    if not canonical.is_active:
        raise ValueError(
            "Канонический Product должен быть active."
        )

    (
        blocked_reasons,
        safety_conflicts,
    ) = await _identity_safety_check(
        session=session,
        canonical=canonical,
        duplicate=duplicate,
    )

    #
    # РУЧНОЕ ПОДТВЕРЖДЕНИЕ.
    #
    # Снимаем исключительно блокировку,
    # связанную со слабой похожестью имени.
    #
    # Например:
    # "Пельмени Иркутские"
    # и
    # "Пельмени Сибирская коллекция
    #  Иркутские традиции, 700г"
    #
    # могут быть подтверждены оператором после
    # отдельной проверки источников.
    #
    if confirmed_identity:
        original_blocked = list(
            blocked_reasons
        )

        blocked_reasons = [
            reason
            for reason in blocked_reasons
            if not reason.startswith(
                "name_similarity_too_weak:"
            )
        ]

        name_block_removed = (
            len(original_blocked)
            != len(blocked_reasons)
        )

        if name_block_removed:
            safety_conflicts.append(
                "identity_manually_confirmed"
            )

    logger.info(
        "Product consolidation check: "
        "canonical=%s duplicate=%s "
        "blocked=%s conflicts=%s "
        "dry_run=%s "
        "confirmed_identity=%s",
        canonical_product_id,
        duplicate_product_id,
        blocked_reasons,
        safety_conflicts,
        dry_run,
        confirmed_identity,
    )

    if blocked_reasons:
        return ProductConsolidationResult(
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
            applied=False,
            dry_run=dry_run,
            updated_fields=(),
            moved_sources=0,
            moved_prices=0,
            moved_ratings=0,
            moved_reviews=0,
            moved_search_history=0,
            moved_aliases=0,
            removed_rating_conflicts=0,
            removed_review_conflicts=0,
            aliases_added=0,
            conflicts=_dedupe_strings(
                safety_conflicts
            ),
            blocked_reasons=_dedupe_strings(
                blocked_reasons
            ),
        )

    #
    # DRY RUN.
    #
    # Даже с confirmed_identity=True
    # база здесь НЕ меняется.
    #
    if dry_run:
        return ProductConsolidationResult(
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
            applied=False,
            dry_run=True,
            updated_fields=(),
            moved_sources=0,
            moved_prices=0,
            moved_ratings=0,
            moved_reviews=0,
            moved_search_history=0,
            moved_aliases=0,
            removed_rating_conflicts=0,
            removed_review_conflicts=0,
            aliases_added=0,
            conflicts=_dedupe_strings(
                safety_conflicts
            ),
            blocked_reasons=(),
        )

    updated_fields: list[str] = []
    conflicts: list[str] = list(
        safety_conflicts
    )

    moved_sources = 0
    moved_prices = 0
    moved_ratings = 0
    moved_reviews = 0
    moved_search_history = 0
    moved_aliases = 0

    removed_rating_conflicts = 0
    removed_review_conflicts = 0

    aliases_added = 0

    #
    # SAVEPOINT.
    #
    # Если любой перенос падает,
    # консолидация откатывается целиком.
    #
    async with session.begin_nested():
        (
            card_updated_fields,
            card_conflicts,
        ) = await _merge_card_fields(
            session=session,
            canonical=canonical,
            duplicate=duplicate,
        )

        updated_fields.extend(
            card_updated_fields
        )

        conflicts.extend(
            card_conflicts
        )

        #
        # Имя дубля сохраняем как alias.
        #
        if (
            normalized(duplicate.name)
            != normalized(canonical.name)
        ):
            if await _ensure_alias(
                session=session,
                product_id=(
                    canonical_product_id
                ),
                alias=duplicate.name,
            ):
                aliases_added += 1

        (
            moved_aliases,
            _created_aliases,
        ) = await _move_aliases(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
        )

        moved_sources = await _move_sources(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
        )

        moved_prices = await _move_prices(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
        )

        (
            moved_ratings,
            removed_rating_conflicts,
        ) = await _move_ratings(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
        )

        (
            moved_reviews,
            removed_review_conflicts,
        ) = await _move_reviews(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
        )

        moved_search_history = (
            await _move_search_history(
                session=session,
                canonical_product_id=(
                    canonical_product_id
                ),
                duplicate_product_id=(
                    duplicate_product_id
                ),
            )
        )

        #
        # Дубль не удаляем физически.
        # Просто выключаем.
        #
        duplicate.is_active = False

        await session.flush()

    logger.warning(
        "Product consolidation applied: "
        "canonical=%s duplicate=%s "
        "updated_fields=%s "
        "sources=%s prices=%s "
        "ratings=%s reviews=%s "
        "search_history=%s aliases=%s "
        "removed_rating_conflicts=%s "
        "removed_review_conflicts=%s "
        "conflicts=%s "
        "confirmed_identity=%s",
        canonical_product_id,
        duplicate_product_id,
        tuple(updated_fields),
        moved_sources,
        moved_prices,
        moved_ratings,
        moved_reviews,
        moved_search_history,
        moved_aliases,
        removed_rating_conflicts,
        removed_review_conflicts,
        conflicts,
        confirmed_identity,
    )

    if commit:
        await session.commit()

    return ProductConsolidationResult(
        canonical_product_id=(
            canonical_product_id
        ),
        duplicate_product_id=(
            duplicate_product_id
        ),
        applied=True,
        dry_run=False,
        updated_fields=_dedupe_strings(
            updated_fields
        ),
        moved_sources=moved_sources,
        moved_prices=moved_prices,
        moved_ratings=moved_ratings,
        moved_reviews=moved_reviews,
        moved_search_history=(
            moved_search_history
        ),
        moved_aliases=moved_aliases,
        removed_rating_conflicts=(
            removed_rating_conflicts
        ),
        removed_review_conflicts=(
            removed_review_conflicts
        ),
        aliases_added=aliases_added,
        conflicts=_dedupe_strings(
            conflicts
        ),
        blocked_reasons=(),
    )
