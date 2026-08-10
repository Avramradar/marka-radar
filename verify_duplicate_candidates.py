from __future__ import annotations

import asyncio
import html
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func
from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_family import ProductFamily
from app.database.models.product_source import ProductSource
from app.database.session import async_session_maker
from app.services.product_merge_service import (
    identity_name_similarity,
    normalize_barcode,
    normalized,
    package_values_compatible,
)


#
# MarkaRadar Duplicate Candidate Verifier v1
#
# Назначение:
# - взять только уже отобранные потенциальные дубли;
# - глубже проверить их по независимым доказательствам;
# - определить финальный класс;
# - предложить каноническую карточку;
# - НИЧЕГО не менять в БД.
#
# Финальные классы:
#
# CONFIRMED_SAME_SKU
# Доказательств достаточно, что это один SKU.
#
# NEEDS_MANUAL_REVIEW
# Пара очень похожа, но данных недостаточно
# или есть противоречие, которое нельзя безопасно
# разрешить автоматически.
#
# CONFIRMED_DIFFERENT_SKU
# Есть сильное доказательство, что это разные SKU.
#
# ВАЖНО:
# AUTO MERGE EXECUTED: NO
# DATABASE CHANGES: NONE
#


class VerificationClass(StrEnum):
    CONFIRMED_SAME_SKU = "CONFIRMED_SAME_SKU"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    CONFIRMED_DIFFERENT_SKU = "CONFIRMED_DIFFERENT_SKU"


@dataclass(slots=True, frozen=True)
class SourceInfo:
    provider: str
    source_id: str
    source_url: str | None


@dataclass(slots=True, frozen=True)
class ProductBundle:
    product: Product
    brand: Brand
    category: Category
    family: ProductFamily | None
    sources: tuple[SourceInfo, ...]


@dataclass(slots=True, frozen=True)
class VerificationResult:
    classification: VerificationClass
    reason: str
    confidence: float

    left_id: int
    right_id: int

    canonical_product_id: int | None

    left_name: str
    right_name: str

    left_brand: str
    right_brand: str

    left_category: str
    right_category: str

    left_family: str | None
    right_family: str | None

    left_barcode: str | None
    right_barcode: str | None

    left_package: str
    right_package: str

    left_sources: tuple[SourceInfo, ...]
    right_sources: tuple[SourceInfo, ...]

    positive_evidence: tuple[str, ...]
    negative_evidence: tuple[str, ...]


#
# Здесь перечисляем пары, которые хотим глубоко проверить.
#
# На первом запуске берём известные REVIEW-кандидаты
# из текущего сита.
#
# После запуска можно расширить этот список ID-парами
# из FINAL SUMMARY, не меняя остальной код.
#
CANDIDATE_PAIRS: tuple[
    tuple[int, int],
    ...
] = (
    (9066, 31627),
    (31654, 31657),
    (31645, 31650),
)


MAX_RESULTS_OUTPUT = 100
FINAL_TOP_IDS = 50


def clean_text( value: object, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def clean_html_text( value: object, ) -> str:
    text = clean_text(
        value
    )

    if not text:
        return ""

    return " ".join(
        html.unescape(
            text
        )
        .split()
    )


def normalized_clean( value: object, ) -> str:
    return normalized(
        clean_html_text(
            value
        )
    )


def package_text( product: Product, ) -> str:
    if product.package_value is None:
        return ""

    return (
        f"{product.package_value}"
        f"{product.package_unit or ''}"
    )


def normalize_optional_text( value: str | None, ) -> str:
    return normalized_clean(
        value
    )


def same_optional_text( left: str | None, right: str | None, ) -> bool | None:
    left_value = normalize_optional_text(
        left
    )

    right_value = normalize_optional_text(
        right
    )

    if (
        not left_value
        or not right_value
    ):
        return None

    return (
        left_value
        == right_value
    )


def source_provider_set( bundle: ProductBundle, ) -> set[str]:
    return {
        source.provider
        for source
        in bundle.sources
    }


def source_identity_set( bundle: ProductBundle, ) -> set[
    tuple[str, str]
]:
    return {
        (
            source.provider,
            source.source_id,
        )
        for source
        in bundle.sources
    }


def text_quality_score( value: str | None, ) -> float:
    text = clean_html_text(
        value
    )

    if not text:
        return 0.0

    score = min(
        len(text) / 5.0,
        60.0,
    )

    if len(text) >= 80:
        score += 10.0

    if len(text) >= 180:
        score += 10.0

    return min(
        score,
        100.0,
    )


def product_completeness_score( bundle: ProductBundle, ) -> float:
    product = bundle.product

    score = 0.0

    if normalize_barcode(
        product.barcode
    ):
        score += 25.0

    if (
        product.package_value is not None
        and product.package_unit
    ):
        score += 15.0

    if clean_text(
        product.subtype
    ):
        score += 8.0

    if clean_text(
        product.image_url
    ):
        score += 15.0

    score += (
        min(
            text_quality_score(
                product.description
            ),
            20.0,
        )
    )

    if clean_text(
        product.keywords
    ):
        score += 5.0

    if bundle.family is not None:
        score += 4.0

    score += min(
        len(
            bundle.sources
        )
        * 4.0,
        8.0,
    )

    return round(
        min(
            score,
            100.0,
        ),
        1,
    )


def choose_canonical_product( *, left: ProductBundle, right: ProductBundle, ) -> int:
    left_score = (
        product_completeness_score(
            left
        )
    )

    right_score = (
        product_completeness_score(
            right
        )
    )

    if left_score > right_score:
        return left.product.id

    if right_score > left_score:
        return right.product.id

    #
    # При равенстве предпочитаем карточку:
    # 1. с barcode;
    # 2. с большим числом источников;
    # 3. с меньшим ID как более старую.
    #
    left_barcode = normalize_barcode(
        left.product.barcode
    )

    right_barcode = normalize_barcode(
        right.product.barcode
    )

    if (
        left_barcode
        and not right_barcode
    ):
        return left.product.id

    if (
        right_barcode
        and not left_barcode
    ):
        return right.product.id

    if len(left.sources) > len(right.sources):
        return left.product.id

    if len(right.sources) > len(left.sources):
        return right.product.id

    return min(
        left.product.id,
        right.product.id,
    )


async def load_bundle( *, session, product_id: int, ) -> ProductBundle | None:
    result = await session.execute(
        select(
            Product,
            Brand,
            Category,
            ProductFamily,
        )
        .join(
            Brand,
            Brand.id
            == Product.brand_id,
        )
        .join(
            Category,
            Category.id
            == Product.category_id,
        )
        .outerjoin(
            ProductFamily,
            ProductFamily.id
            == Product.family_id,
        )
        .where(
            Product.id
            == int(
                product_id
            )
        )
        .limit(
            1
        )
    )

    row = result.first()

    if row is None:
        return None

    (
        product,
        brand,
        category,
        family,
    ) = row

    sources_result = await session.execute(
        select(
            ProductSource
        )
        .where(
            ProductSource.product_id
            == product.id
        )
        .order_by(
            ProductSource.provider.asc(),
            ProductSource.source_id.asc(),
        )
    )

    source_rows = list(
        sources_result.scalars().all()
    )

    sources = tuple(
        SourceInfo(
            provider=source.provider,
            source_id=source.source_id,
            source_url=source.source_url,
        )
        for source
        in source_rows
    )

    return ProductBundle(
        product=product,
        brand=brand,
        category=category,
        family=family,
        sources=sources,
    )


def verify_pair( *, left: ProductBundle, right: ProductBundle, ) -> VerificationResult:
    positive: list[str] = []
    negative: list[str] = []

    left_product = left.product
    right_product = right.product

    left_barcode = normalize_barcode(
        left_product.barcode
    )

    right_barcode = normalize_barcode(
        right_product.barcode
    )

    #
    # 1. BRAND
    #
    same_brand = (
        normalized_clean(
            left.brand.name
        )
        == normalized_clean(
            right.brand.name
        )
    )

    if same_brand:
        positive.append(
            "same_brand"
        )
    else:
        negative.append(
            "different_brand"
        )

    #
    # 2. NAME
    #
    same_name = (
        normalized_clean(
            left_product.name
        )
        == normalized_clean(
            right_product.name
        )
    )

    (
        coverage,
        jaccard,
        common_count,
    ) = identity_name_similarity(
        clean_html_text(
            left_product.name
        ),
        clean_html_text(
            right_product.name
        ),
    )

    if same_name:
        positive.append(
            "exact_normalized_name"
        )
    elif (
        common_count >= 3
        and coverage >= 0.95
        and jaccard >= 0.80
    ):
        positive.append(
            "very_strong_name_similarity"
        )
    elif (
        common_count >= 2
        and coverage >= 0.80
        and jaccard >= 0.55
    ):
        positive.append(
            "strong_name_similarity"
        )
    else:
        negative.append(
            "weak_name_similarity"
        )

    #
    # 3. BARCODE
    #
    if (
        left_barcode
        and right_barcode
    ):
        if (
            left_barcode
            == right_barcode
        ):
            positive.append(
                "same_barcode"
            )
        else:
            negative.append(
                "different_barcode"
            )
    elif bool(left_barcode) != bool(
        right_barcode
    ):
        positive.append(
            "barcode_known_on_one_side"
        )

    #
    # 4. PACKAGE
    #
    package_compatibility = (
        package_values_compatible(
            current_value=(
                left_product.package_value
            ),
            current_unit=(
                left_product.package_unit
            ),
            incoming_value=(
                right_product.package_value
            ),
            incoming_unit=(
                right_product.package_unit
            ),
        )
    )

    if package_compatibility is True:
        positive.append(
            "same_package"
        )

    elif package_compatibility is False:
        negative.append(
            "different_package"
        )

    #
    # 5. SUBTYPE
    #
    subtype_equal = same_optional_text(
        left_product.subtype,
        right_product.subtype,
    )

    if subtype_equal is True:
        positive.append(
            "same_subtype"
        )

    elif subtype_equal is False:
        negative.append(
            "different_subtype"
        )

    #
    # 6. FAMILY
    #
    if (
        left.family is not None
        and right.family is not None
    ):
        if (
            left.family.id
            == right.family.id
        ):
            positive.append(
                "same_family"
            )
        else:
            negative.append(
                "different_family"
            )

    #
    # 7. SOURCES
    #
    left_source_ids = (
        source_identity_set(
            left
        )
    )

    right_source_ids = (
        source_identity_set(
            right
        )
    )

    left_providers = source_provider_set(
        left
    )

    right_providers = source_provider_set(
        right
    )

    shared_source_identity = (
        left_source_ids
        & right_source_ids
    )

    independent_providers = (
        left_providers
        | right_providers
    )

    if shared_source_identity:
        positive.append(
            "shared_provider_source_identity"
        )

    if (
        left.sources
        and right.sources
        and not shared_source_identity
        and len(
            independent_providers
        ) >= 2
    ):
        positive.append(
            "independent_external_sources"
        )

    if (
        left.sources
        and right.sources
        and left_providers
        == right_providers
        and not shared_source_identity
    ):
        negative.append(
            "same_provider_different_source_ids"
        )

    #
    # 8. DESCRIPTION
    #
    description_equal = (
        same_optional_text(
            left_product.description,
            right_product.description,
        )
    )

    if description_equal is True:
        positive.append(
            "same_description"
        )

    #
    # 9. CATEGORY
    #
    same_category = (
        left.category.id
        == right.category.id
        or normalized_clean(
            left.category.name
        )
        == normalized_clean(
            right.category.name
        )
    )

    if same_category:
        positive.append(
            "same_category"
        )
    else:
        negative.append(
            "different_category"
        )

    #
    # РЕШЕНИЕ.
    #
    hard_negative = {
        "different_brand",
        "different_package",
        "different_subtype",
    }

    if (
        hard_negative
        & set(
            negative
        )
    ):
        return VerificationResult(
            classification=(
                VerificationClass.CONFIRMED_DIFFERENT_SKU
            ),
            reason=(
                "hard_identity_conflict"
            ),
            confidence=99.0,
            left_id=left_product.id,
            right_id=right_product.id,
            canonical_product_id=None,
            left_name=left_product.name,
            right_name=right_product.name,
            left_brand=left.brand.name,
            right_brand=right.brand.name,
            left_category=(
                left.category.name
            ),
            right_category=(
                right.category.name
            ),
            left_family=(
                left.family.name
                if left.family is not None
                else None
            ),
            right_family=(
                right.family.name
                if right.family is not None
                else None
            ),
            left_barcode=left_barcode,
            right_barcode=right_barcode,
            left_package=package_text(
                left_product
            ),
            right_package=package_text(
                right_product
            ),
            left_sources=left.sources,
            right_sources=right.sources,
            positive_evidence=tuple(
                positive
            ),
            negative_evidence=tuple(
                negative
            ),
        )

    #
    # Разные известные barcode не позволяют
    # автоматически подтвердить один SKU.
    #
    if "different_barcode" in negative:
        return VerificationResult(
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "barcode_conflict_requires_external_confirmation"
            ),
            confidence=70.0,
            left_id=left_product.id,
            right_id=right_product.id,
            canonical_product_id=None,
            left_name=left_product.name,
            right_name=right_product.name,
            left_brand=left.brand.name,
            right_brand=right.brand.name,
            left_category=(
                left.category.name
            ),
            right_category=(
                right.category.name
            ),
            left_family=(
                left.family.name
                if left.family is not None
                else None
            ),
            right_family=(
                right.family.name
                if right.family is not None
                else None
            ),
            left_barcode=left_barcode,
            right_barcode=right_barcode,
            left_package=package_text(
                left_product
            ),
            right_package=package_text(
                right_product
            ),
            left_sources=left.sources,
            right_sources=right.sources,
            positive_evidence=tuple(
                positive
            ),
            negative_evidence=tuple(
                negative
            ),
        )

    strong_identity = bool(
        "same_barcode" in positive
        or (
            "exact_normalized_name"
            in positive
            and "same_brand"
            in positive
            and "same_package"
            in positive
        )
        or (
            "very_strong_name_similarity"
            in positive
            and "same_brand"
            in positive
            and "same_package"
            in positive
            and (
                "independent_external_sources"
                in positive
                or "same_family"
                in positive
                or "same_subtype"
                in positive
            )
        )
    )

    if strong_identity:
        canonical_id = (
            choose_canonical_product(
                left=left,
                right=right,
            )
        )

        confidence = 96.0

        if (
            "same_barcode"
            in positive
        ):
            confidence = 100.0

        elif (
            "independent_external_sources"
            in positive
        ):
            confidence = 98.0

        return VerificationResult(
            classification=(
                VerificationClass.CONFIRMED_SAME_SKU
            ),
            reason=(
                "multiple_independent_identity_signals"
            ),
            confidence=confidence,
            left_id=left_product.id,
            right_id=right_product.id,
            canonical_product_id=(
                canonical_id
            ),
            left_name=left_product.name,
            right_name=right_product.name,
            left_brand=left.brand.name,
            right_brand=right.brand.name,
            left_category=(
                left.category.name
            ),
            right_category=(
                right.category.name
            ),
            left_family=(
                left.family.name
                if left.family is not None
                else None
            ),
            right_family=(
                right.family.name
                if right.family is not None
                else None
            ),
            left_barcode=left_barcode,
            right_barcode=right_barcode,
            left_package=package_text(
                left_product
            ),
            right_package=package_text(
                right_product
            ),
            left_sources=left.sources,
            right_sources=right.sources,
            positive_evidence=tuple(
                positive
            ),
            negative_evidence=tuple(
                negative
            ),
        )

    #
    # Если есть сильное сходство, но не хватает
    # независимого подтверждения — ручная проверка.
    #
    if (
        "exact_normalized_name"
        in positive
        or "very_strong_name_similarity"
        in positive
        or "strong_name_similarity"
        in positive
    ):
        return VerificationResult(
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "strong_similarity_but_not_enough_independent_evidence"
            ),
            confidence=80.0,
            left_id=left_product.id,
            right_id=right_product.id,
            canonical_product_id=None,
            left_name=left_product.name,
            right_name=right_product.name,
            left_brand=left.brand.name,
            right_brand=right.brand.name,
            left_category=(
                left.category.name
            ),
            right_category=(
                right.category.name
            ),
            left_family=(
                left.family.name
                if left.family is not None
                else None
            ),
            right_family=(
                right.family.name
                if right.family is not None
                else None
            ),
            left_barcode=left_barcode,
            right_barcode=right_barcode,
            left_package=package_text(
                left_product
            ),
            right_package=package_text(
                right_product
            ),
            left_sources=left.sources,
            right_sources=right.sources,
            positive_evidence=tuple(
                positive
            ),
            negative_evidence=tuple(
                negative
            ),
        )

    return VerificationResult(
        classification=(
            VerificationClass.CONFIRMED_DIFFERENT_SKU
        ),
        reason=(
            "insufficient_identity_overlap"
        ),
        confidence=95.0,
        left_id=left_product.id,
        right_id=right_product.id,
        canonical_product_id=None,
        left_name=left_product.name,
        right_name=right_product.name,
        left_brand=left.brand.name,
        right_brand=right.brand.name,
        left_category=(
            left.category.name
        ),
        right_category=(
            right.category.name
        ),
        left_family=(
            left.family.name
            if left.family is not None
            else None
        ),
        right_family=(
            right.family.name
            if right.family is not None
            else None
        ),
        left_barcode=left_barcode,
        right_barcode=right_barcode,
        left_package=package_text(
            left_product
        ),
        right_package=package_text(
            right_product
        ),
        left_sources=left.sources,
        right_sources=right.sources,
        positive_evidence=tuple(
            positive
        ),
        negative_evidence=tuple(
            negative
        ),
    )


def print_sources( title: str, sources: tuple[ SourceInfo, ... ], ) -> None:
    print(
        title
    )

    if not sources:
        print(
            " none"
        )
        return

    for source in sources:
        print(
            " ",
            source.provider,
            "|",
            source.source_id,
            "|",
            source.source_url,
        )


def print_result( *, index: int, item: VerificationResult, ) -> None:
    print(
        "-" * 80
    )

    print(
        f"#{index} "
        f"class={item.classification.value} "
        f"confidence={item.confidence} "
        f"reason={item.reason}"
    )

    print(
        "ids:",
        item.left_id,
        "<->",
        item.right_id,
    )

    print(
        "canonical_product_id:",
        item.canonical_product_id,
    )

    print(
        "names:",
        repr(
            item.left_name
        ),
        "|",
        repr(
            item.right_name
        ),
    )

    print(
        "brands:",
        item.left_brand,
        "|",
        item.right_brand,
    )

    print(
        "categories:",
        item.left_category,
        "|",
        item.right_category,
    )

    print(
        "families:",
        item.left_family,
        "|",
        item.right_family,
    )

    print(
        "barcodes:",
        item.left_barcode,
        "|",
        item.right_barcode,
    )

    print(
        "packages:",
        item.left_package,
        "|",
        item.right_package,
    )

    print(
        "positive_evidence:",
        item.positive_evidence,
    )

    print(
        "negative_evidence:",
        item.negative_evidence,
    )

    print_sources(
        "LEFT SOURCES:",
        item.left_sources,
    )

    print_sources(
        "RIGHT SOURCES:",
        item.right_sources,
    )


def format_top_ids( items: list[ VerificationResult ], ) -> str:
    if not items:
        return "none"

    return ", ".join(
        (
            f"{item.left_id}<->{item.right_id}"
            f"({item.confidence})"
        )
        for item
        in items[
            :FINAL_TOP_IDS
        ]
    )


def print_final_summary( *, results: list[ VerificationResult ], ) -> None:
    counts = Counter(
        item.classification.value
        for item in results
    )

    same_items = [
        item
        for item in results
        if (
            item.classification
            == VerificationClass.CONFIRMED_SAME_SKU
        )
    ]

    review_items = [
        item
        for item in results
        if (
            item.classification
            == VerificationClass.NEEDS_MANUAL_REVIEW
        )
    ]

    different_items = [
        item
        for item in results
        if (
            item.classification
            == VerificationClass.CONFIRMED_DIFFERENT_SKU
        )
    ]

    same_items.sort(
        key=lambda item: (
            item.confidence,
            item.left_id,
            item.right_id,
        ),
        reverse=True,
    )

    review_items.sort(
        key=lambda item: (
            item.confidence,
            item.left_id,
            item.right_id,
        ),
        reverse=True,
    )

    different_items.sort(
        key=lambda item: (
            item.confidence,
            item.left_id,
            item.right_id,
        ),
        reverse=True,
    )

    print()
    print(
        "=" * 80
    )
    print(
        "FINAL VERIFICATION SUMMARY"
    )
    print(
        "=" * 80
    )

    print(
        "Pairs requested:",
        len(
            CANDIDATE_PAIRS
        ),
    )

    print(
        "Pairs verified:",
        len(
            results
        ),
    )

    print()

    print(
        "CONFIRMED_SAME_SKU:",
        counts[
            VerificationClass.CONFIRMED_SAME_SKU.value
        ],
    )

    print(
        "NEEDS_MANUAL_REVIEW:",
        counts[
            VerificationClass.NEEDS_MANUAL_REVIEW.value
        ],
    )

    print(
        "CONFIRMED_DIFFERENT_SKU:",
        counts[
            VerificationClass.CONFIRMED_DIFFERENT_SKU.value
        ],
    )

    print()

    print(
        "TOP SAME SKU IDS:",
        format_top_ids(
            same_items
        ),
    )

    print(
        "TOP MANUAL REVIEW IDS:",
        format_top_ids(
            review_items
        ),
    )

    print(
        "TOP DIFFERENT SKU IDS:",
        format_top_ids(
            different_items
        ),
    )

    print()

    print(
        "AUTO MERGE EXECUTED: NO"
    )

    print(
        "DATABASE CHANGES: NONE"
    )

    print(
        "=" * 80
    )


async def main() -> None:
    print(
        "=" * 80
    )

    print(
        "MarkaRadar Duplicate Candidate Verifier v1"
    )

    print(
        "MODE: DEEP VERIFICATION ONLY"
    )

    print(
        "AUTO MERGE EXECUTED: NO"
    )

    print(
        "DATABASE CHANGES: NONE"
    )

    print(
        "=" * 80
    )

    results: list[
        VerificationResult
    ] = []

    async with (
        async_session_maker()
        as session
    ):
        for (
            left_id,
            right_id,
        ) in CANDIDATE_PAIRS:
            left = await load_bundle(
                session=session,
                product_id=left_id,
            )

            right = await load_bundle(
                session=session,
                product_id=right_id,
            )

            if (
                left is None
                or right is None
            ):
                print(
                    "-" * 80
                )

                print(
                    "PAIR NOT FOUND:",
                    left_id,
                    "<->",
                    right_id,
                )

                continue

            result = verify_pair(
                left=left,
                right=right,
            )

            results.append(
                result
            )

    ordered = sorted(
        results,
        key=lambda item: (
            item.classification.value,
            item.confidence,
        ),
        reverse=True,
    )

    print()

    print(
        "=" * 80
    )

    print(
        "VERIFICATION RESULTS"
    )

    print(
        "=" * 80
    )

    if not ordered:
        print(
            "none"
        )

    for index, item in enumerate(
        ordered[
            :MAX_RESULTS_OUTPUT
        ],
        start=1,
    ):
        print_result(
            index=index,
            item=item,
        )

    print_final_summary(
        results=results,
    )


if __name__ == "__main__":
    asyncio.run(
        main()
  )
