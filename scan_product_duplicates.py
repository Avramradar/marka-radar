from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
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


@dataclass(slots=True, frozen=True)
class DuplicateCandidate:
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

    barcode_equal: bool
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

    discriminator_conflicts: tuple[str, ...]

    reason: str
    score: float


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
# Общие слова, которые сами по себе
# не отличают SKU.
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


def clean_text( value: object, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def comparable_text( value: object, ) -> str:
    """ Нормализация, которая НЕ уничтожает %, точки и запятые. Это критично для: 2.5% 3.2% 1.35кг """

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
    """ Извлекает массу/объём из названия. В отличие от старой версии: 1.35кг -> 1.35:кг а НЕ: 35:кг """

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
    """ Выделяет SKU-различающие слова. Примеры различий, которые должны остановить автоматическое объединение: финский индейкой armonioso intenso cremoso forte Если такое слово есть только у одной карточки, пара остаётся для ручной проверки и НЕ считается безопасным auto-merge кандидатом. """

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


def find_discriminator_conflicts( *, left: Product, right: Product, brand_name: str, ) -> tuple[
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """ Возвращает структурированные признаки и список конфликтов. Главное изменение v3: односторонний variant token тоже считается небезопасным для автоматического merge. Это специально консервативно. """

    conflicts: list[str] = []

    left_percentages = extract_percentages(
        left.name
    )

    right_percentages = extract_percentages(
        right.name
    )

    if values_conflict(
        left_percentages,
        right_percentages,
    ):
        conflicts.append(
            "different_percentage:"
            f"{left_percentages}"
            "!="
            f"{right_percentages}"
        )

    left_counts = extract_counts(
        left.name
    )

    right_counts = extract_counts(
        right.name
    )

    if values_conflict(
        left_counts,
        right_counts,
    ):
        conflicts.append(
            "different_count:"
            f"{left_counts}"
            "!="
            f"{right_counts}"
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

    if values_conflict(
        left_name_packages,
        right_name_packages,
    ):
        conflicts.append(
            "different_name_package:"
            f"{left_name_packages}"
            "!="
            f"{right_name_packages}"
        )

    left_variants = variant_tokens(
        left.name,
        brand_name=brand_name,
    )

    right_variants = variant_tokens(
        right.name,
        brand_name=brand_name,
    )

    if (
        left_variants
        != right_variants
    ):
        conflicts.append(
            "different_variant_tokens:"
            f"{tuple(sorted(left_variants))}"
            "!="
            f"{tuple(sorted(right_variants))}"
        )

    left_discriminators = tuple(
        sorted(
            {
                *left_percentages,
                *left_counts,
                *left_name_packages,
                *left_variants,
            }
        )
    )

    right_discriminators = tuple(
        sorted(
            {
                *right_percentages,
                *right_counts,
                *right_name_packages,
                *right_variants,
            }
        )
    )

    return (
        left_percentages,
        right_percentages,
        left_counts,
        right_counts,
        left_name_packages,
        right_name_packages,
        tuple(
            sorted(
                left_variants
            )
        ),
        tuple(
            sorted(
                right_variants
            )
        ),
        tuple(
            conflicts
        ),
    )


def candidate_score( *, barcode_equal: bool, package_compatible: bool | None, coverage: float, jaccard: float, common_count: int, discriminator_conflicts: tuple[ str, ... ], ) -> float:
    score = 0.0

    if barcode_equal:
        score += 100.0

    #
    # Same brand + same category.
    #
    score += 20.0
    score += 15.0

    if package_compatible is True:
        score += 20.0

    score += (
        coverage
        * 25.0
    )

    score += (
        jaccard
        * 15.0
    )

    if common_count >= 3:
        score += 10.0

    elif common_count >= 2:
        score += 5.0

    score -= (
        len(
            discriminator_conflicts
        )
        * 100.0
    )

    return round(
        score,
        1,
    )


def should_keep_candidate( *, barcode_equal: bool, package_compatible: bool | None, coverage: float, jaccard: float, common_count: int, discriminator_conflicts: tuple[ str, ... ], ) -> tuple[
    bool,
    str,
]:
    """ Scanner v3 делит пары на два класса: 1. same_barcode Очень сильный кандидат. Даже при конфликте выводим для диагностики. 2. strong_brand_category_name_match Без barcode допускается только если НЕТ ни одного SKU-discriminator конфликта. """

    if barcode_equal:
        if package_compatible is False:
            return (
                True,
                "same_barcode_but_package_conflict",
            )

        if discriminator_conflicts:
            return (
                True,
                "same_barcode_but_discriminator_conflict",
            )

        return (
            True,
            "same_barcode",
        )

    if package_compatible is False:
        return (
            False,
            "package_conflict",
        )

    if discriminator_conflicts:
        return (
            False,
            "discriminator_conflict",
        )

    if common_count < 2:
        return (
            False,
            "weak_name",
        )

    if coverage < 0.80:
        return (
            False,
            "weak_coverage",
        )

    if jaccard < 0.55:
        return (
            False,
            "weak_jaccard",
        )

    return (
        True,
        "strong_brand_category_name_match",
    )


async def main() -> None:
    print(
        "=" * 80
    )

    print(
        "MarkaRadar Duplicate Scanner v3"
    )

    print(
        "DATABASE CHANGES: NONE"
    )

    print(
        "=" * 80
    )

    async with (
        async_session_maker()
        as session
    ):
        result = await session.execute(
            select(
                Product,
                Brand,
                Category,
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
            .where(
                Product.is_active.is_(
                    True
                )
            )
            .order_by(
                Product.id.asc()
            )
        )

        rows = list(
            result.all()
        )

    print(
        "Active products:",
        len(rows),
    )

    by_barcode: dict[
        str,
        list[
            tuple[
                Product,
                Brand,
                Category,
            ]
        ],
    ] = defaultdict(
        list
    )

    by_brand_category: dict[
        tuple[int, int],
        list[
            tuple[
                Product,
                Brand,
                Category,
            ]
        ],
    ] = defaultdict(
        list
    )

    for (
        product,
        brand,
        category,
    ) in rows:
        barcode = normalize_barcode(
            product.barcode
        )

        if barcode:
            by_barcode[
                barcode
            ].append(
                (
                    product,
                    brand,
                    category,
                )
            )

        if (
            not is_unknown_brand(
                brand.name
            )
            and not is_generic_category(
                category.name
            )
        ):
            by_brand_category[
                (
                    brand.id,
                    category.id,
                )
            ].append(
                (
                    product,
                    brand,
                    category,
                )
            )

    candidates: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    rejected_by_discriminator = 0
    rejected_by_package = 0
    rejected_by_name = 0

    async def evaluate_pair( *, left: Product, right: Product, brand: Brand, category: Category, force_barcode_equal: bool = False, ) -> None:
        nonlocal rejected_by_discriminator
        nonlocal rejected_by_package
        nonlocal rejected_by_name

        left_barcode = normalize_barcode(
            left.barcode
        )

        right_barcode = normalize_barcode(
            right.barcode
        )

        barcode_equal = (
            force_barcode_equal
            or bool(
                left_barcode
                and right_barcode
                and left_barcode
                == right_barcode
            )
        )

        #
        # Разные известные barcode —
        # жёсткий запрет.
        #
        if (
            left_barcode
            and right_barcode
            and left_barcode
            != right_barcode
        ):
            return

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

        (
            left_percentages,
            right_percentages,
            left_counts,
            right_counts,
            left_name_packages,
            right_name_packages,
            left_variant_tokens,
            right_variant_tokens,
            discriminator_conflicts,
        ) = find_discriminator_conflicts(
            left=left,
            right=right,
            brand_name=brand.name,
        )

        (
            keep,
            reason,
        ) = should_keep_candidate(
            barcode_equal=barcode_equal,
            package_compatible=(
                package_compatibility
            ),
            coverage=coverage,
            jaccard=jaccard,
            common_count=common_count,
            discriminator_conflicts=(
                discriminator_conflicts
            ),
        )

        if not keep:
            if (
                reason
                == "discriminator_conflict"
            ):
                rejected_by_discriminator += 1

            elif (
                reason
                == "package_conflict"
            ):
                rejected_by_package += 1

            else:
                rejected_by_name += 1

            return

        score = candidate_score(
            barcode_equal=barcode_equal,
            package_compatible=(
                package_compatibility
            ),
            coverage=coverage,
            jaccard=jaccard,
            common_count=common_count,
            discriminator_conflicts=(
                discriminator_conflicts
            ),
        )

        key = (
            min(
                left.id,
                right.id,
            ),
            max(
                left.id,
                right.id,
            ),
        )

        existing = candidates.get(
            key
        )

        if (
            existing is not None
            and existing.score >= score
        ):
            return

        candidates[
            key
        ] = DuplicateCandidate(
            left_id=left.id,
            right_id=right.id,
            brand_name=brand.name,
            category_name=(
                category.name
            ),
            left_name=left.name,
            right_name=right.name,
            left_barcode=left_barcode,
            right_barcode=right_barcode,
            left_package=package_text(
                left
            ),
            right_package=package_text(
                right
            ),
            barcode_equal=barcode_equal,
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
            left_variant_tokens=(
                left_variant_tokens
            ),
            right_variant_tokens=(
                right_variant_tokens
            ),
            discriminator_conflicts=(
                discriminator_conflicts
            ),
            reason=reason,
            score=score,
        )

    #
    # 1. Одинаковый barcode.
    #
    for group in by_barcode.values():
        if len(group) < 2:
            continue

        for left_index in range(
            len(group)
        ):
            for right_index in range(
                left_index + 1,
                len(group),
            ):
                (
                    left,
                    brand,
                    category,
                ) = group[
                    left_index
                ]

                (
                    right,
                    _right_brand,
                    _right_category,
                ) = group[
                    right_index
                ]

                await evaluate_pair(
                    left=left,
                    right=right,
                    brand=brand,
                    category=category,
                    force_barcode_equal=True,
                )

    #
    # 2. Один реальный бренд + одна категория.
    #
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
                (
                    left,
                    brand,
                    category,
                ) = group[
                    left_index
                ]

                (
                    right,
                    _right_brand,
                    _right_category,
                ) = group[
                    right_index
                ]

                await evaluate_pair(
                    left=left,
                    right=right,
                    brand=brand,
                    category=category,
                )

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item.barcode_equal,
            item.score,
            item.common_count,
        ),
        reverse=True,
    )

    print()

    print(
        "Duplicate candidates:",
        len(ordered),
    )

    print(
        "Rejected by discriminator conflict:",
        rejected_by_discriminator,
    )

    print(
        "Rejected by package conflict:",
        rejected_by_package,
    )

    print(
        "Rejected by weak name:",
        rejected_by_name,
    )

    print()

    for index, item in enumerate(
        ordered[:100],
        start=1,
    ):
        print(
            "-" * 80
        )

        print(
            f"#{index} "
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
            "barcode_equal:",
            item.barcode_equal,
        )

        print(
            "package_compatible:",
            item.package_compatible,
        )

        print(
            "left_percentages:",
            item.left_percentages,
        )

        print(
            "right_percentages:",
            item.right_percentages,
        )

        print(
            "left_counts:",
            item.left_counts,
        )

        print(
            "right_counts:",
            item.right_counts,
        )

        print(
            "left_name_packages:",
            item.left_name_packages,
        )

        print(
            "right_name_packages:",
            item.right_name_packages,
        )

        print(
            "left_variant_tokens:",
            item.left_variant_tokens,
        )

        print(
            "right_variant_tokens:",
            item.right_variant_tokens,
        )

        print(
            "discriminator_conflicts:",
            item.discriminator_conflicts,
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

    print()

    print(
        "=" * 80
    )

    print(
        "SCAN COMPLETE"
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
