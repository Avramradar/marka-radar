from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable

from sqlalchemy import func
from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_source import ProductSource
from app.database.session import async_session_maker
from app.services.product_merge_service import (
    identity_name_similarity,
    is_generic_category,
    is_unknown_brand,
    normalize_barcode,
    normalize_package_unit,
    normalized,
    package_values_compatible,
)


#
# Scanner v4
#
# Цель:
# - ничего не менять в базе;
# - разделять пары на AUTO_SAFE / REVIEW / REJECT;
# - не считать разную жирность, вкус, вариант,
# количество или упаковку дублем;
# - не терять случаи, где у одной карточки есть
# barcode, а у другой — более полные данные.
#


class CandidateClass(StrEnum):
    AUTO_SAFE = "AUTO_SAFE"
    REVIEW = "REVIEW"
    REJECT = "REJECT"


@dataclass(slots=True, frozen=True)
class ProductMeta:
    product: Product
    brand: Brand
    category: Category
    source_count: int


@dataclass(slots=True, frozen=True)
class PairEvidence:
    left_barcode: str | None
    right_barcode: str | None

    package_compatible: bool | None

    coverage: float
    jaccard: float
    common_count: int

    left_percentages: tuple[str, ...]
    right_percentages: tuple[str, ...]

    left_counts: tuple[str, ...]
    right_counts: tuple[str, ...]

    left_name_packages: tuple[str, ...]
    right_name_packages: tuple[str, ...]

    left_variant_tokens: tuple[str, ...]
    right_variant_tokens: tuple[str, ...]

    hard_conflicts: tuple[str, ...]
    soft_differences: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class DuplicateCandidate:
    classification: CandidateClass
    reason: str
    score: float

    left_id: int
    right_id: int

    brand_name: str
    category_name: str

    left_name: str
    right_name: str

    left_barcode: str | None
    right_barcode: str | None

    left_package: str
    right_package: str

    left_sources: int
    right_sources: int

    package_compatible: bool | None

    coverage: float
    jaccard: float
    common_count: int

    left_percentages: tuple[str, ...]
    right_percentages: tuple[str, ...]

    left_counts: tuple[str, ...]
    right_counts: tuple[str, ...]

    left_name_packages: tuple[str, ...]
    right_name_packages: tuple[str, ...]

    left_variant_tokens: tuple[str, ...]
    right_variant_tokens: tuple[str, ...]

    hard_conflicts: tuple[str, ...]
    soft_differences: tuple[str, ...]


PERCENT_PATTERN = re.compile(
    r"(?<![\d.,])"
    r"(\d+(?:[.,]\d+)?)"
    r"\s*%",
    flags=re.IGNORECASE,
)

COUNT_PATTERN = re.compile(
    r"(?<![\d.,])"
    r"(\d+)"
    r"\s*"
    r"(шт|штук|капсул|пакетик(?:а|ов)?|"
    r"саше|порц(?:ия|ии|ий))\b",
    flags=re.IGNORECASE,
)

PACKAGE_IN_NAME_PATTERN = re.compile(
    r"(?<![\d.,])"
    r"(\d+(?:[.,]\d+)?)"
    r"\s*"
    r"(кг|г|гр|мл|л)\b",
    flags=re.IGNORECASE,
)

WORD_PATTERN = re.compile(
    r"[a-zа-я0-9]+",
    flags=re.IGNORECASE,
)


#
# Эти слова сами по себе не должны считаться
# различающим SKU-признаком.
#
GENERIC_VARIANT_WORDS = {
    "колбаса",
    "сервелат",
    "сметана",
    "молоко",
    "кофе",
    "пельмени",
    "майонез",
    "сыр",
    "масло",
    "йогурт",
    "кефир",
    "творог",
    "сливки",
    "напиток",
    "напитки",
    "капсулах",
    "капсулы",
    "кофемашин",
    "nespresso",
    "вареная",
    "вареный",
    "вареное",
    "варено",
    "копченая",
    "копченый",
    "копченое",
    "копченые",
    "варенокопченая",
    "варенокопченый",
    "ультрапастеризованное",
    "пастеризованное",
    "сливочная",
    "классическая",
    "классический",
    "традиции",
    "традиционный",
    "традиционная",
    "традиционное",
    "гост",
    "бзмж",
    "для",
    "из",
    "на",
    "с",
    "со",
    "без",
}


#
# Максимальное число примеров REJECT в логе.
# Все REJECT считаются в статистике, но не превращают
# GitHub Actions в огромную простыню.
#
MAX_AUTO_SAFE_OUTPUT = 100
MAX_REVIEW_OUTPUT = 100
MAX_REJECT_OUTPUT = 30


def clean_text( value: object, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def comparable_text( value: object, ) -> str:
    """ Нормализация, которая НЕ уничтожает: - % - точки; - запятые. Иначе 2.5% / 3.2% или 1.35кг невозможно корректно различить. """

    return (
        clean_text(
            value
        )
        .lower()
        .replace(
            "ё",
            "е",
        )
    )


def decimal_text( value: str, ) -> str:
    try:
        number = Decimal(
            value.replace(
                ",",
                ".",
            )
        )
    except Exception:
        return value.replace(
            ",",
            ".",
        )

    if (
        number
        == number.to_integral()
    ):
        return str(
            int(number)
        )

    return format(
        number.normalize(),
        "f",
    )


def package_text( product: Product, ) -> str:
    if product.package_value is None:
        return ""

    return (
        f"{product.package_value}"
        f"{product.package_unit or ''}"
    )


def extract_percentages( value: str | None, ) -> tuple[str, ...]:
    text = comparable_text(
        value
    )

    values = {
        decimal_text(
            match.group(1)
        )
        for match
        in PERCENT_PATTERN.finditer(
            text
        )
    }

    return tuple(
        sorted(
            values
        )
    )


def extract_counts( value: str | None, ) -> tuple[str, ...]:
    text = comparable_text(
        value
    )

    values: set[str] = set()

    for match in COUNT_PATTERN.finditer(
        text
    ):
        number = match.group(1)

        unit = (
            match.group(2)
            .lower()
            .replace(
                "ё",
                "е",
            )
        )

        values.add(
            f"{number}:{unit}"
        )

    return tuple(
        sorted(
            values
        )
    )


def extract_name_packages( value: str | None, ) -> tuple[str, ...]:
    """ Извлекает массу/объём прямо из названия. Пример: 1.35кг -> 1.35:кг 300г -> 300:г """

    text = comparable_text(
        value
    )

    values: set[str] = set()

    for match in PACKAGE_IN_NAME_PATTERN.finditer(
        text
    ):
        raw_value = decimal_text(
            match.group(1)
        )

        unit = normalize_package_unit(
            match.group(2)
        )

        if not unit:
            continue

        values.add(
            f"{raw_value}:{unit}"
        )

    return tuple(
        sorted(
            values
        )
    )


def tokenize_brand( brand_name: str | None, ) -> set[str]:
    return {
        token
        for token
        in WORD_PATTERN.findall(
            normalized(
                brand_name
            )
        )
        if len(token) >= 3
    }


def variant_tokens( value: str | None, *, brand_name: str | None, ) -> set[str]:
    """ Выделяет слова, которые могут отличать SKU. Например: финский индейкой armonioso intenso cremoso forte Эти слова не являются автоматическим REJECT сами по себе, если встречаются только у одной карточки. Односторонняя разница отправляет пару в REVIEW. Если с обеих сторон присутствуют разные вариантные признаки — это уже сильный REJECT. """

    text = normalized(
        value
    )

    brand_tokens = tokenize_brand(
        brand_name
    )

    result: set[str] = set()

    for token in WORD_PATTERN.findall(
        text
    ):
        if len(token) < 3:
            continue

        if token.isdigit():
            continue

        if token in brand_tokens:
            continue

        if token in GENERIC_VARIANT_WORDS:
            continue

        result.add(
            token
        )

    return result


def values_conflict( left_values: Iterable[str], right_values: Iterable[str], ) -> bool:
    left = set(
        left_values
    )

    right = set(
        right_values
    )

    return bool(
        left
        and right
        and left != right
    )


def build_evidence( *, left: Product, right: Product, brand_name: str, ) -> PairEvidence:
    left_barcode = normalize_barcode(
        left.barcode
    )

    right_barcode = normalize_barcode(
        right.barcode
    )

    package_compatibility = (
        package_values_compatible(
            current_value=(
                left.package_value
            ),
            current_unit=(
                left.package_unit
            ),
            incoming_value=(
                right.package_value
            ),
            incoming_unit=(
                right.package_unit
            ),
        )
    )

    (
        coverage,
        jaccard,
        common_count,
    ) = identity_name_similarity(
        left.name,
        right.name,
    )

    left_percentages = extract_percentages(
        left.name
    )

    right_percentages = extract_percentages(
        right.name
    )

    left_counts = extract_counts(
        left.name
    )

    right_counts = extract_counts(
        right.name
    )

    left_name_packages = (
        extract_name_packages(
            left.name
        )
    )

    right_name_packages = (
        extract_name_packages(
            right.name
        )
    )

    left_variants = variant_tokens(
        left.name,
        brand_name=brand_name,
    )

    right_variants = variant_tokens(
        right.name,
        brand_name=brand_name,
    )

    hard_conflicts: list[str] = []
    soft_differences: list[str] = []

    #
    # Разные известные barcode — жёсткий запрет.
    #
    if (
        left_barcode
        and right_barcode
        and left_barcode
        != right_barcode
    ):
        hard_conflicts.append(
            "different_barcode:"
            f"{left_barcode}"
            "!="
            f"{right_barcode}"
        )

    #
    # Разные известные package_value/unit —
    # жёсткий запрет.
    #
    if package_compatibility is False:
        hard_conflicts.append(
            "different_package:"
            f"{package_text(left)}"
            "!="
            f"{package_text(right)}"
        )

    #
    # Жирность / концентрация / процент —
    # сильный SKU-признак.
    #
    if values_conflict(
        left_percentages,
        right_percentages,
    ):
        hard_conflicts.append(
            "different_percentage:"
            f"{left_percentages}"
            "!="
            f"{right_percentages}"
        )

    #
    # Количество штук/капсул/порций.
    #
    if values_conflict(
        left_counts,
        right_counts,
    ):
        hard_conflicts.append(
            "different_count:"
            f"{left_counts}"
            "!="
            f"{right_counts}"
        )

    #
    # Иногда упаковка есть только в названии.
    #
    if values_conflict(
        left_name_packages,
        right_name_packages,
    ):
        hard_conflicts.append(
            "different_name_package:"
            f"{left_name_packages}"
            "!="
            f"{right_name_packages}"
        )

    common_variants = (
        left_variants
        & right_variants
    )

    left_only_variants = (
        left_variants
        - common_variants
    )

    right_only_variants = (
        right_variants
        - common_variants
    )

    #
    # Разные конкретные варианты с обеих сторон:
    # Armonioso vs Intenso
    # Cremoso vs Forte
    #
    if (
        left_only_variants
        and right_only_variants
    ):
        hard_conflicts.append(
            "different_variant_tokens:"
            f"{tuple(sorted(left_only_variants))}"
            "!="
            f"{tuple(sorted(right_only_variants))}"
        )

    #
    # Признак только на одной стороне:
    # "финский" vs отсутствие слова,
    # "с индейкой" vs отсутствие слова.
    #
    # Этого недостаточно для REJECT:
    # вторая карточка может быть просто урезана.
    # Поэтому REVIEW.
    #
    elif (
        left_only_variants
        or right_only_variants
    ):
        soft_differences.append(
            "one_sided_variant_tokens:"
            f"{tuple(sorted(left_only_variants))}"
            "|"
            f"{tuple(sorted(right_only_variants))}"
        )

    #
    # Barcode только у одной карточки —
    # важный сигнал для REVIEW.
    #
    if bool(left_barcode) != bool(right_barcode):
        soft_differences.append(
            "barcode_present_only_on_one_side"
        )

    #
    # Упаковка известна только у одной стороны.
    #
    if package_compatibility is None:
        left_has_package = bool(
            left.package_value is not None
            and left.package_unit
        )

        right_has_package = bool(
            right.package_value is not None
            and right.package_unit
        )

        if (
            left_has_package
            != right_has_package
        ):
            soft_differences.append(
                "package_present_only_on_one_side"
            )

    return PairEvidence(
        left_barcode=left_barcode,
        right_barcode=right_barcode,
        package_compatible=(
            package_compatibility
        ),
        coverage=coverage,
        jaccard=jaccard,
        common_count=common_count,
        left_percentages=(
            left_percentages
        ),
        right_percentages=(
            right_percentages
        ),
        left_counts=left_counts,
        right_counts=right_counts,
        left_name_packages=(
            left_name_packages
        ),
        right_name_packages=(
            right_name_packages
        ),
        left_variant_tokens=tuple(
            sorted(
                left_variants
            )
        ),
        right_variant_tokens=tuple(
            sorted(
                right_variants
            )
        ),
        hard_conflicts=tuple(
            hard_conflicts
        ),
        soft_differences=tuple(
            soft_differences
        ),
    )


def classify_pair( *, left: Product, right: Product, evidence: PairEvidence, ) -> tuple[
    CandidateClass,
    str,
    float,
]:
    """ Три класса: AUTO_SAFE Очень сильные доказательства. В будущем только этот класс можно рассматривать для автоматической консолидации. REVIEW Пара похожа, но данных недостаточно или одна карточка явно более полная. REJECT Есть конфликт SKU или слишком мало доказательств. """

    if evidence.hard_conflicts:
        return (
            CandidateClass.REJECT,
            "hard_identity_conflict",
            0.0,
        )

    same_normalized_name = (
        normalized(
            left.name
        )
        == normalized(
            right.name
        )
    )

    both_barcodes = bool(
        evidence.left_barcode
        and evidence.right_barcode
    )

    same_barcode = bool(
        both_barcodes
        and evidence.left_barcode
        == evidence.right_barcode
    )

    #
    # В нормальной схеме products.barcode UNIQUE,
    # но оставляем правило как дополнительную
    # защиту для исторических/нестандартных данных.
    #
    if same_barcode:
        if evidence.soft_differences:
            return (
                CandidateClass.REVIEW,
                "same_barcode_with_incomplete_or_variant_data",
                98.0,
            )

        return (
            CandidateClass.AUTO_SAFE,
            "same_barcode",
            100.0,
        )

    #
    # Точное имя + совпадающая известная упаковка.
    #
    if (
        same_normalized_name
        and evidence.package_compatible is True
        and not evidence.soft_differences
    ):
        return (
            CandidateClass.AUTO_SAFE,
            "exact_name_and_package",
            96.0,
        )

    #
    # Очень сильное fuzzy-сходство +
    # подтверждённая одинаковая упаковка +
    # отсутствие различающих признаков.
    #
    if (
        evidence.package_compatible is True
        and evidence.common_count >= 3
        and evidence.coverage >= 0.95
        and evidence.jaccard >= 0.80
        and not evidence.soft_differences
    ):
        score = (
            85.0
            + evidence.coverage * 5.0
            + evidence.jaccard * 5.0
        )

        return (
            CandidateClass.AUTO_SAFE,
            "very_strong_name_and_package",
            round(
                min(
                    score,
                    95.0,
                ),
                1,
            ),
        )

    #
    # REVIEW №1:
    # у одной карточки есть barcode, у второй нет,
    # бренд/категория уже совпали по группе,
    # нет жёстких конфликтов.
    #
    # Даже один сильный общий токен может быть
    # полезен для ручной проверки — именно так
    # выглядел исторический случай с "Иркутскими".
    #
    one_barcode_only = (
        bool(
            evidence.left_barcode
        )
        != bool(
            evidence.right_barcode
        )
    )

    if (
        one_barcode_only
        and evidence.common_count >= 1
        and evidence.coverage >= 0.70
    ):
        score = (
            60.0
            + evidence.coverage * 15.0
            + evidence.jaccard * 10.0
        )

        if evidence.package_compatible is True:
            score += 8.0

        return (
            CandidateClass.REVIEW,
            "one_barcode_plus_related_identity",
            round(
                min(
                    score,
                    89.0,
                ),
                1,
            ),
        )

    #
    # REVIEW №2:
    # сильное сходство названия, но одна сторона
    # содержит дополнительный вариантный признак
    # или данные по упаковке неполны.
    #
    if (
        evidence.common_count >= 2
        and evidence.coverage >= 0.80
        and evidence.jaccard >= 0.55
    ):
        score = (
            55.0
            + evidence.coverage * 15.0
            + evidence.jaccard * 10.0
        )

        if evidence.package_compatible is True:
            score += 8.0

        return (
            CandidateClass.REVIEW,
            "strong_similarity_needs_review",
            round(
                min(
                    score,
                    88.0,
                ),
                1,
            ),
        )

    #
    # REVIEW №3:
    # точное нормализованное имя, но упаковки
    # недостаточно для уверенного AUTO_SAFE.
    #
    if same_normalized_name:
        return (
            CandidateClass.REVIEW,
            "exact_name_but_identity_incomplete",
            75.0,
        )

    return (
        CandidateClass.REJECT,
        "insufficient_identity_evidence",
        0.0,
    )


def make_candidate( *, classification: CandidateClass, reason: str, score: float, left: ProductMeta, right: ProductMeta, evidence: PairEvidence, ) -> DuplicateCandidate:
    return DuplicateCandidate(
        classification=classification,
        reason=reason,
        score=score,
        left_id=left.product.id,
        right_id=right.product.id,
        brand_name=left.brand.name,
        category_name=(
            left.category.name
        ),
        left_name=left.product.name,
        right_name=right.product.name,
        left_barcode=(
            evidence.left_barcode
        ),
        right_barcode=(
            evidence.right_barcode
        ),
        left_package=package_text(
            left.product
        ),
        right_package=package_text(
            right.product
        ),
        left_sources=left.source_count,
        right_sources=right.source_count,
        package_compatible=(
            evidence.package_compatible
        ),
        coverage=evidence.coverage,
        jaccard=evidence.jaccard,
        common_count=evidence.common_count,
        left_percentages=(
            evidence.left_percentages
        ),
        right_percentages=(
            evidence.right_percentages
        ),
        left_counts=evidence.left_counts,
        right_counts=evidence.right_counts,
        left_name_packages=(
            evidence.left_name_packages
        ),
        right_name_packages=(
            evidence.right_name_packages
        ),
        left_variant_tokens=(
            evidence.left_variant_tokens
        ),
        right_variant_tokens=(
            evidence.right_variant_tokens
        ),
        hard_conflicts=(
            evidence.hard_conflicts
        ),
        soft_differences=(
            evidence.soft_differences
        ),
    )


def print_candidate( *, index: int, item: DuplicateCandidate, ) -> None:
    print(
        "-" * 80
    )

    print(
        f"#{index} "
        f"class={item.classification.value} "
        f"score={item.score} "
        f"reason={item.reason}"
    )

    print(
        "ids:",
        item.left_id,
        "<->",
        item.right_id,
    )

    print(
        "brand:",
        item.brand_name,
    )

    print(
        "category:",
        item.category_name,
    )

    print(
        "left:",
        repr(
            item.left_name
        ),
    )

    print(
        "right:",
        repr(
            item.right_name
        ),
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
        "source_counts:",
        item.left_sources,
        "|",
        item.right_sources,
    )

    print(
        "package_compatible:",
        item.package_compatible,
    )

    print(
        "percentages:",
        item.left_percentages,
        "|",
        item.right_percentages,
    )

    print(
        "counts:",
        item.left_counts,
        "|",
        item.right_counts,
    )

    print(
        "name_packages:",
        item.left_name_packages,
        "|",
        item.right_name_packages,
    )

    print(
        "variant_tokens:",
        item.left_variant_tokens,
        "|",
        item.right_variant_tokens,
    )

    print(
        "hard_conflicts:",
        item.hard_conflicts,
    )

    print(
        "soft_differences:",
        item.soft_differences,
    )

    print(
        "common_count:",
        item.common_count,
    )

    print(
        "coverage:",
        round(
            item.coverage,
            3,
        ),
    )

    print(
        "jaccard:",
        round(
            item.jaccard,
            3,
        ),
    )


async def load_product_meta() -> list[
    ProductMeta
]:
    async with (
        async_session_maker()
        as session
    ):
        source_counts_subquery = (
            select(
                ProductSource.product_id.label(
                    "product_id"
                ),
                func.count(
                    ProductSource.id
                ).label(
                    "source_count"
                ),
            )
            .group_by(
                ProductSource.product_id
            )
            .subquery()
        )

        result = await session.execute(
            select(
                Product,
                Brand,
                Category,
                func.coalesce(
                    source_counts_subquery.c.source_count,
                    0,
                ),
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
                source_counts_subquery,
                source_counts_subquery.c.product_id
                == Product.id,
            )
            .where(
                Product.is_active.is_(
                    True
                )
            )
            .order_by(
                Product.id.asc()
            )
        )

        return [
            ProductMeta(
                product=product,
                brand=brand,
                category=category,
                source_count=int(
                    source_count
                    or 0
                ),
            )
            for (
                product,
                brand,
                category,
                source_count,
            )
            in result.all()
        ]


async def main() -> None:
    print(
        "=" * 80
    )

    print(
        "MarkaRadar Duplicate Scanner v4"
    )

    print(
        "MODE: CLASSIFICATION ONLY"
    )

    print(
        "DATABASE CHANGES: NONE"
    )

    print(
        "=" * 80
    )

    rows = await load_product_meta()

    print(
        "Active products:",
        len(rows),
    )

    by_brand_category: dict[
        tuple[int, int],
        list[ProductMeta],
    ] = defaultdict(
        list
    )

    for item in rows:
        if is_unknown_brand(
            item.brand.name
        ):
            continue

        if is_generic_category(
            item.category.name
        ):
            continue

        by_brand_category[
            (
                item.brand.id,
                item.category.id,
            )
        ].append(
            item
        )

    auto_safe: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    review: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject_reason_counts: Counter[str] = Counter()

    evaluated_pairs = 0

    for group in by_brand_category.values():
        if len(group) < 2:
            continue

        for left_index in range(
            len(group)
        ):
            for right_index in range(
                left_index + 1,
                len(group),
            ):
                left = group[
                    left_index
                ]

                right = group[
                    right_index
                ]

                evaluated_pairs += 1

                evidence = build_evidence(
                    left=left.product,
                    right=right.product,
                    brand_name=left.brand.name,
                )

                (
                    classification,
                    reason,
                    score,
                ) = classify_pair(
                    left=left.product,
                    right=right.product,
                    evidence=evidence,
                )

                candidate = make_candidate(
                    classification=classification,
                    reason=reason,
                    score=score,
                    left=left,
                    right=right,
                    evidence=evidence,
                )

                key = (
                    min(
                        left.product.id,
                        right.product.id,
                    ),
                    max(
                        left.product.id,
                        right.product.id,
                    ),
                )

                if (
                    classification
                    == CandidateClass.AUTO_SAFE
                ):
                    auto_safe[
                        key
                    ] = candidate

                elif (
                    classification
                    == CandidateClass.REVIEW
                ):
                    review[
                        key
                    ] = candidate

                else:
                    reject_reason_counts[
                        reason
                    ] += 1

                    #
                    # Для лога сохраняем только
                    # небольшое число REJECT-примеров.
                    #
                    if (
                        len(reject)
                        < MAX_REJECT_OUTPUT
                    ):
                        reject[
                            key
                        ] = candidate

    ordered_auto_safe = sorted(
        auto_safe.values(),
        key=lambda item: (
            item.score,
            item.common_count,
        ),
        reverse=True,
    )

    ordered_review = sorted(
        review.values(),
        key=lambda item: (
            item.score,
            item.common_count,
        ),
        reverse=True,
    )

    ordered_reject = list(
        reject.values()
    )

    print()

    print(
        "Evaluated pairs:",
        evaluated_pairs,
    )

    print(
        "AUTO_SAFE:",
        len(
            ordered_auto_safe
        ),
    )

    print(
        "REVIEW:",
        len(
            ordered_review
        ),
    )

    print(
        "REJECT:",
        sum(
            reject_reason_counts.values()
        ),
    )

    print()

    print(
        "REJECT reasons:"
    )

    if not reject_reason_counts:
        print(
            " none"
        )
    else:
        for (
            reason,
            count,
        ) in reject_reason_counts.most_common():
            print(
                f" {reason}: {count}"
            )

    print()

    print(
        "=" * 80
    )

    print(
        "AUTO_SAFE CANDIDATES"
    )

    print(
        "=" * 80
    )

    if not ordered_auto_safe:
        print(
            "none"
        )

    for index, item in enumerate(
        ordered_auto_safe[
            :MAX_AUTO_SAFE_OUTPUT
        ],
        start=1,
    ):
        print_candidate(
            index=index,
            item=item,
        )

    print()

    print(
        "=" * 80
    )

    print(
        "REVIEW CANDIDATES"
    )

    print(
        "=" * 80
    )

    if not ordered_review:
        print(
            "none"
        )

    for index, item in enumerate(
        ordered_review[
            :MAX_REVIEW_OUTPUT
        ],
        start=1,
    ):
        print_candidate(
            index=index,
            item=item,
        )

    print()

    print(
        "=" * 80
    )

    print(
        "REJECT EXAMPLES"
    )

    print(
        "=" * 80
    )

    if not ordered_reject:
        print(
            "none"
        )

    for index, item in enumerate(
        ordered_reject[
            :MAX_REJECT_OUTPUT
        ],
        start=1,
    ):
        print_candidate(
            index=index,
            item=item,
        )

    print()

    print(
        "=" * 80
    )

    print(
        "SCAN COMPLETE"
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


if __name__ == "__main__":
    asyncio.run(
        main()
    )
