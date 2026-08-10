from __future__ import annotations

import asyncio
import html
import re
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations
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
    identity_name_tokens,
    is_generic_category,
    is_unknown_brand,
    normalize_barcode,
    normalize_package_unit,
    normalized,
    package_values_compatible,
)


#
# MarkaRadar Duplicate Scanner v7
#
# Главное отличие от v6:
# - сито сужает выборку РАНЬШЕ;
# - category compatibility участвует уже в coarse blocking;
# - один общий слабый токен больше не создаёт тысячи пар;
# - разные известные barcode почти всегда сразу REJECT;
# - BARCODE_CONFLICT_REVIEW остаётся только для почти
# идентичных карточек;
# - HTML-мусор и package-токены удаляются из variant tokens.
#
# Никаких изменений в БД.
#


class CandidateClass(StrEnum):
    AUTO_SAFE = "AUTO_SAFE"
    REVIEW = "REVIEW"
    BARCODE_CONFLICT_REVIEW = "BARCODE_CONFLICT_REVIEW"
    REJECT = "REJECT"


@dataclass(slots=True, frozen=True)
class ProductMeta:
    product: Product
    brand: Brand
    category: Category
    source_count: int

    normalized_brand: str
    category_bucket: str

    identity_tokens: frozenset[str]


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

    left_brand: str
    right_brand: str

    left_category: str
    right_category: str

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


@dataclass(slots=True)
class SieveStats:
    products_loaded: int = 0
    eligible_products: int = 0

    brand_category_buckets: int = 0
    raw_pairs_inside_buckets: int = 0

    coarse_pairs_generated: int = 0

    rejected_different_barcode: int = 0
    rejected_package: int = 0
    rejected_percentage: int = 0
    rejected_count: int = 0
    rejected_name_package: int = 0
    rejected_variant: int = 0
    rejected_weak_name: int = 0

    passed_structure: int = 0
    passed_name: int = 0

    auto_safe: int = 0
    review: int = 0
    barcode_conflict_review: int = 0
    reject: int = 0


PERCENT_PATTERN = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*%",
    flags=re.IGNORECASE,
)

COUNT_PATTERN = re.compile(
    r"(?<![\d.,])(\d+)\s*"
    r"(шт|штук|капсул|пакетик(?:а|ов)?|"
    r"саше|порц(?:ия|ии|ий))\b",
    flags=re.IGNORECASE,
)

PACKAGE_IN_NAME_PATTERN = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*"
    r"(кг|г|гр|мл|л)\b",
    flags=re.IGNORECASE,
)

WORD_PATTERN = re.compile(
    r"[a-zа-я0-9]+",
    flags=re.IGNORECASE,
)

PACKAGE_TOKEN_PATTERN = re.compile(
    r"^\d+(?:[.,]\d+)?(?:г|гр|кг|мл|л)$",
    flags=re.IGNORECASE,
)


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
    "вода",
    "чай",
    "кетчуп",

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

    "quot",
    "amp",
    "lt",
    "gt",
    "nbsp",
}


CATEGORY_EQUIVALENCE_GROUPS = (
    {
        "молочная продукция",
        "dairy",
        "dairy products",
        "milk products",
        "smetana",
    },
    {
        "напитки",
        "beverages",
        "drinks",
    },
    {
        "чай",
        "teas",
        "tea",
    },
    {
        "батончики",
        "bars",
        "chocolate bars",
    },
    {
        "вода",
        "drinking water",
        "water",
    },
    {
        "кетчуп",
        "ketchup",
    },
    {
        "колбаса",
        "sausages",
        "sausage",
    },
)


MAX_TOKEN_BLOCK_SIZE = 50

MAX_AUTO_SAFE_OUTPUT = 100
MAX_REVIEW_OUTPUT = 100
MAX_BARCODE_CONFLICT_OUTPUT = 50
MAX_REJECT_OUTPUT = 20


def clean_text( value: object, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def html_clean_text( value: object, ) -> str:
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


def comparable_text( value: object, ) -> str:
    return (
        html_clean_text(
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

    return tuple(
        sorted(
            {
                decimal_text(
                    match.group(1)
                )
                for match
                in PERCENT_PATTERN.finditer(
                    text
                )
            }
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
        values.add(
            f"{match.group(1)}:"
            f"{match.group(2).lower().replace('ё', 'е')}"
        )

    return tuple(
        sorted(
            values
        )
    )


def extract_name_packages( value: str | None, ) -> tuple[str, ...]:
    text = comparable_text(
        value
    )

    values: set[str] = set()

    for match in PACKAGE_IN_NAME_PATTERN.finditer(
        text
    ):
        unit = normalize_package_unit(
            match.group(2)
        )

        if not unit:
            continue

        values.add(
            f"{decimal_text(match.group(1))}:"
            f"{unit}"
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
                html_clean_text(
                    brand_name
                )
            )
        )
        if len(token) >= 3
    }


def variant_tokens( value: str | None, *, brand_name: str | None, ) -> set[str]:
    text = normalized(
        html_clean_text(
            value
        )
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

        if PACKAGE_TOKEN_PATTERN.fullmatch(
            token
        ):
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


def category_bucket( category_name: str | None, ) -> str:
    key = normalized(
        html_clean_text(
            category_name
        )
    )

    if not key:
        return ""

    for index, group in enumerate(
        CATEGORY_EQUIVALENCE_GROUPS,
        start=1,
    ):
        normalized_group = {
            normalized(
                item
            )
            for item in group
        }

        if key in normalized_group:
            return (
                f"equiv:{index}"
            )

    return key


def pair_key( left_id: int, right_id: int, ) -> tuple[int, int]:
    return (
        min(
            left_id,
            right_id,
        ),
        max(
            left_id,
            right_id,
        ),
    )


def build_evidence( *, left: ProductMeta, right: ProductMeta, ) -> PairEvidence:
    left_product = left.product
    right_product = right.product

    left_barcode = normalize_barcode(
        left_product.barcode
    )

    right_barcode = normalize_barcode(
        right_product.barcode
    )

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

    (
        coverage,
        jaccard,
        common_count,
    ) = identity_name_similarity(
        html_clean_text(
            left_product.name
        ),
        html_clean_text(
            right_product.name
        ),
    )

    left_percentages = extract_percentages(
        left_product.name
    )

    right_percentages = extract_percentages(
        right_product.name
    )

    left_counts = extract_counts(
        left_product.name
    )

    right_counts = extract_counts(
        right_product.name
    )

    left_name_packages = (
        extract_name_packages(
            left_product.name
        )
    )

    right_name_packages = (
        extract_name_packages(
            right_product.name
        )
    )

    left_variants = variant_tokens(
        left_product.name,
        brand_name=left.brand.name,
    )

    right_variants = variant_tokens(
        right_product.name,
        brand_name=right.brand.name,
    )

    hard_conflicts: list[str] = []
    soft_differences: list[str] = []

    if package_compatibility is False:
        hard_conflicts.append(
            "different_package:"
            f"{package_text(left_product)}"
            "!="
            f"{package_text(right_product)}"
        )

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

    left_only = (
        left_variants
        - common_variants
    )

    right_only = (
        right_variants
        - common_variants
    )

    if (
        left_only
        and right_only
    ):
        hard_conflicts.append(
            "different_variant_tokens:"
            f"{tuple(sorted(left_only))}"
            "!="
            f"{tuple(sorted(right_only))}"
        )

    elif (
        left_only
        or right_only
    ):
        soft_differences.append(
            "one_sided_variant_tokens:"
            f"{tuple(sorted(left_only))}"
            "|"
            f"{tuple(sorted(right_only))}"
        )

    if bool(left_barcode) != bool(right_barcode):
        soft_differences.append(
            "barcode_present_only_on_one_side"
        )

    if package_compatibility is None:
        left_has_package = bool(
            left_product.package_value is not None
            and left_product.package_unit
        )

        right_has_package = bool(
            right_product.package_value is not None
            and right_product.package_unit
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


def classify_pair( *, left: ProductMeta, right: ProductMeta, evidence: PairEvidence, ) -> tuple[
    CandidateClass,
    str,
    float,
]:
    same_name = (
        normalized(
            html_clean_text(
                left.product.name
            )
        )
        == normalized(
            html_clean_text(
                right.product.name
            )
        )
    )

    same_barcode = bool(
        evidence.left_barcode
        and evidence.right_barcode
        and evidence.left_barcode
        == evidence.right_barcode
    )

    different_known_barcodes = bool(
        evidence.left_barcode
        and evidence.right_barcode
        and evidence.left_barcode
        != evidence.right_barcode
    )

    #
    # Жёсткий structural conflict.
    #
    if evidence.hard_conflicts:
        return (
            CandidateClass.REJECT,
            "hard_identity_conflict",
            0.0,
        )

    #
    # Разные известные barcode.
    #
    # В отдельный review попадают только почти
    # идентичные карточки.
    #
    if different_known_barcodes:
        very_strong_name = bool(
            same_name
            or (
                evidence.common_count >= 3
                and evidence.coverage >= 0.95
                and evidence.jaccard >= 0.80
            )
        )

        package_ok = (
            evidence.package_compatible is True
        )

        percentages_equal = (
            evidence.left_percentages
            == evidence.right_percentages
        )

        counts_equal = (
            evidence.left_counts
            == evidence.right_counts
        )

        if (
            very_strong_name
            and package_ok
            and percentages_equal
            and counts_equal
        ):
            score = (
                80.0
                + evidence.coverage * 6.0
                + evidence.jaccard * 6.0
            )

            return (
                CandidateClass.BARCODE_CONFLICT_REVIEW,
                "near_identical_but_different_barcode",
                round(
                    min(
                        score,
                        92.0,
                    ),
                    1,
                ),
            )

        return (
            CandidateClass.REJECT,
            "different_barcode",
            0.0,
        )

    if same_barcode:
        return (
            CandidateClass.AUTO_SAFE,
            "same_barcode",
            100.0,
        )

    if (
        same_name
        and evidence.package_compatible is True
    ):
        return (
            CandidateClass.AUTO_SAFE,
            "exact_name_and_package",
            97.0,
        )

    if (
        evidence.package_compatible is True
        and evidence.common_count >= 3
        and evidence.coverage >= 0.95
        and evidence.jaccard >= 0.85
        and not evidence.soft_differences
    ):
        score = (
            88.0
            + evidence.coverage * 4.0
            + evidence.jaccard * 4.0
        )

        return (
            CandidateClass.AUTO_SAFE,
            "very_strong_identity",
            round(
                min(
                    score,
                    96.0,
                ),
                1,
            ),
        )

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
            62.0
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
                    90.0,
                ),
                1,
            ),
        )

    if (
        evidence.common_count >= 2
        and evidence.coverage >= 0.80
        and evidence.jaccard >= 0.55
    ):
        score = (
            58.0
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
                    89.0,
                ),
                1,
            ),
        )

    if same_name:
        return (
            CandidateClass.REVIEW,
            "exact_name_but_identity_incomplete",
            78.0,
        )

    return (
        CandidateClass.REJECT,
        "insufficient_identity_evidence",
        0.0,
    )


def build_coarse_pairs( rows: list[ProductMeta], stats: SieveStats, ) -> set[
    tuple[int, int]
]:
    """ SIEVE 0. Сначала: normalized brand + category bucket Потом внутри этого блока: exact normalized name ИЛИ минимум 2 общих identity token ИЛИ один редкий identity token + barcode только у одной стороны. Таким образом мы не создаём сотни тысяч сравнений по одному слову типа "кетчуп". """

    pairs: set[
        tuple[int, int]
    ] = set()

    buckets: dict[
        tuple[str, str],
        list[ProductMeta],
    ] = defaultdict(
        list
    )

    for item in rows:
        if not item.normalized_brand:
            continue

        if is_unknown_brand(
            item.brand.name
        ):
            continue

        if not item.category_bucket:
            continue

        buckets[
            (
                item.normalized_brand,
                item.category_bucket,
            )
        ].append(
            item
        )

    stats.brand_category_buckets = len(
        buckets
    )

    for group in buckets.values():
        if len(group) < 2:
            continue

        stats.raw_pairs_inside_buckets += (
            len(group)
            * (
                len(group) - 1
            )
            // 2
        )

        exact_name_index: dict[
            str,
            list[int],
        ] = defaultdict(
            list
        )

        token_index: dict[
            str,
            list[int],
        ] = defaultdict(
            list
        )

        by_id = {
            item.product.id: item
            for item in group
        }

        for item in group:
            exact_name = normalized(
                html_clean_text(
                    item.product.name
                )
            )

            if exact_name:
                exact_name_index[
                    exact_name
                ].append(
                    item.product.id
                )

            for token in item.identity_tokens:
                token_index[
                    token
                ].append(
                    item.product.id
                )

        #
        # Exact name.
        #
        for ids in exact_name_index.values():
            if len(ids) < 2:
                continue

            for (
                left_id,
                right_id,
            ) in combinations(
                ids,
                2,
            ):
                pairs.add(
                    pair_key(
                        left_id,
                        right_id,
                    )
                )

        #
        # Build counts of how many identity tokens
        # each pair shares.
        #
        pair_common_tokens: Counter[
            tuple[int, int]
        ] = Counter()

        for ids in token_index.values():
            if (
                len(ids) < 2
                or len(ids)
                > MAX_TOKEN_BLOCK_SIZE
            ):
                continue

            for (
                left_id,
                right_id,
            ) in combinations(
                ids,
                2,
            ):
                pair_common_tokens[
                    pair_key(
                        left_id,
                        right_id,
                    )
                ] += 1

        for key, common_count in (
            pair_common_tokens.items()
        ):
            left = by_id[
                key[0]
            ]

            right = by_id[
                key[1]
            ]

            if common_count >= 2:
                pairs.add(
                    key
                )
                continue

            #
            # Один общий identity token допустим
            # только для сценария "barcode есть
            # у одной карточки, у второй нет".
            #
            left_barcode = normalize_barcode(
                left.product.barcode
            )

            right_barcode = normalize_barcode(
                right.product.barcode
            )

            one_barcode_only = (
                bool(left_barcode)
                != bool(right_barcode)
            )

            if one_barcode_only:
                pairs.add(
                    key
                )

    stats.coarse_pairs_generated = len(
        pairs
    )

    return pairs


def make_candidate( *, classification: CandidateClass, reason: str, score: float, left: ProductMeta, right: ProductMeta, evidence: PairEvidence, ) -> DuplicateCandidate:
    return DuplicateCandidate(
        classification=classification,
        reason=reason,
        score=score,
        left_id=left.product.id,
        right_id=right.product.id,
        left_brand=left.brand.name,
        right_brand=right.brand.name,
        left_category=(
            left.category.name
        ),
        right_category=(
            right.category.name
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
        left_counts=(
            evidence.left_counts
        ),
        right_counts=(
            evidence.right_counts
        ),
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

        items: list[
            ProductMeta
        ] = []

        for (
            product,
            brand,
            category,
            source_count,
        ) in result.all():
            cleaned_name = html_clean_text(
                product.name
            )

            items.append(
                ProductMeta(
                    product=product,
                    brand=brand,
                    category=category,
                    source_count=int(
                        source_count
                        or 0
                    ),
                    normalized_brand=(
                        normalized(
                            html_clean_text(
                                brand.name
                            )
                        )
                    ),
                    category_bucket=(
                        category_bucket(
                            category.name
                        )
                    ),
                    identity_tokens=frozenset(
                        identity_name_tokens(
                            cleaned_name
                        )
                    ),
                )
            )

        return items


def count_reject_reason( *, reason: str, evidence: PairEvidence, stats: SieveStats, reject_reason_counts: Counter[str], ) -> None:
    stats.reject += 1

    reject_reason_counts[
        reason
    ] += 1

    if reason == "different_barcode":
        stats.rejected_different_barcode += 1
        return

    for conflict in evidence.hard_conflicts:
        if conflict.startswith(
            "different_package:"
        ):
            stats.rejected_package += 1

        elif conflict.startswith(
            "different_percentage:"
        ):
            stats.rejected_percentage += 1

        elif conflict.startswith(
            "different_count:"
        ):
            stats.rejected_count += 1

        elif conflict.startswith(
            "different_name_package:"
        ):
            stats.rejected_name_package += 1

        elif conflict.startswith(
            "different_variant_tokens:"
        ):
            stats.rejected_variant += 1

    if reason == "insufficient_identity_evidence":
        stats.rejected_weak_name += 1


def print_sieve_stats( stats: SieveStats, ) -> None:
    print()
    print(
        "=" * 80
    )
    print(
        "SIEVE STATISTICS"
    )
    print(
        "=" * 80
    )

    print(
        "Products loaded:",
        stats.products_loaded,
    )

    print(
        "Eligible products:",
        stats.eligible_products,
    )

    print(
        "Brand+category buckets:",
        stats.brand_category_buckets,
    )

    print(
        "Raw possible pairs inside buckets:",
        stats.raw_pairs_inside_buckets,
    )

    print(
        "SIEVE 0 - coarse pairs generated:",
        stats.coarse_pairs_generated,
    )

    print(
        "Rejected different barcode:",
        stats.rejected_different_barcode,
    )

    print(
        "Rejected package:",
        stats.rejected_package,
    )

    print(
        "Rejected percentage:",
        stats.rejected_percentage,
    )

    print(
        "Rejected count:",
        stats.rejected_count,
    )

    print(
        "Rejected name-package:",
        stats.rejected_name_package,
    )

    print(
        "Rejected variant:",
        stats.rejected_variant,
    )

    print(
        "Rejected weak name:",
        stats.rejected_weak_name,
    )

    print(
        "Passed structure sieve:",
        stats.passed_structure,
    )

    print(
        "Passed name sieve:",
        stats.passed_name,
    )

    print(
        "AUTO_SAFE:",
        stats.auto_safe,
    )

    print(
        "REVIEW:",
        stats.review,
    )

    print(
        "BARCODE_CONFLICT_REVIEW:",
        stats.barcode_conflict_review,
    )

    print(
        "REJECT:",
        stats.reject,
    )


async def main() -> None:
    print(
        "=" * 80
    )

    print(
        "MarkaRadar Duplicate Scanner v7"
    )

    print(
        "MODE: NARROWING MULTI-STAGE SIEVE"
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

    rows = await load_product_meta()

    stats = SieveStats(
        products_loaded=len(
            rows
        )
    )

    stats.eligible_products = sum(
        1
        for item in rows
        if (
            item.normalized_brand
            and not is_unknown_brand(
                item.brand.name
            )
            and item.category_bucket
        )
    )

    by_id = {
        item.product.id: item
        for item in rows
    }

    coarse_pairs = build_coarse_pairs(
        rows,
        stats,
    )

    auto_safe: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    review: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    barcode_conflict_review: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject_examples: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject_reason_counts: Counter[str] = Counter()

    for (
        left_id,
        right_id,
    ) in coarse_pairs:
        left = by_id[
            left_id
        ]

        right = by_id[
            right_id
        ]

        evidence = build_evidence(
            left=left,
            right=right,
        )

        #
        # Fast structural rejection.
        #
        if evidence.hard_conflicts:
            (
                classification,
                reason,
                score,
            ) = classify_pair(
                left=left,
                right=right,
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

            count_reject_reason(
                reason=reason,
                evidence=evidence,
                stats=stats,
                reject_reason_counts=(
                    reject_reason_counts
                ),
            )

            if (
                len(
                    reject_examples
                )
                < MAX_REJECT_OUTPUT
            ):
                reject_examples[
                    pair_key(
                        left_id,
                        right_id,
                    )
                ] = candidate

            continue

        stats.passed_structure += 1

        same_name = (
            normalized(
                html_clean_text(
                    left.product.name
                )
            )
            == normalized(
                html_clean_text(
                    right.product.name
                )
            )
        )

        one_barcode_only = (
            bool(
                evidence.left_barcode
            )
            != bool(
                evidence.right_barcode
            )
        )

        name_related = bool(
            same_name
            or evidence.common_count >= 2
            or (
                one_barcode_only
                and evidence.common_count >= 1
                and evidence.coverage >= 0.70
            )
        )

        if not name_related:
            classification = (
                CandidateClass.REJECT
            )

            reason = (
                "insufficient_identity_evidence"
            )

            score = 0.0

            candidate = make_candidate(
                classification=classification,
                reason=reason,
                score=score,
                left=left,
                right=right,
                evidence=evidence,
            )

            count_reject_reason(
                reason=reason,
                evidence=evidence,
                stats=stats,
                reject_reason_counts=(
                    reject_reason_counts
                ),
            )

            if (
                len(
                    reject_examples
                )
                < MAX_REJECT_OUTPUT
            ):
                reject_examples[
                    pair_key(
                        left_id,
                        right_id,
                    )
                ] = candidate

            continue

        stats.passed_name += 1

        (
            classification,
            reason,
            score,
        ) = classify_pair(
            left=left,
            right=right,
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

        key = pair_key(
            left_id,
            right_id,
        )

        if (
            classification
            == CandidateClass.AUTO_SAFE
        ):
            stats.auto_safe += 1

            auto_safe[
                key
            ] = candidate

        elif (
            classification
            == CandidateClass.REVIEW
        ):
            stats.review += 1

            review[
                key
            ] = candidate

        elif (
            classification
            == CandidateClass.BARCODE_CONFLICT_REVIEW
        ):
            stats.barcode_conflict_review += 1

            barcode_conflict_review[
                key
            ] = candidate

        else:
            count_reject_reason(
                reason=reason,
                evidence=evidence,
                stats=stats,
                reject_reason_counts=(
                    reject_reason_counts
                ),
            )

            if (
                len(
                    reject_examples
                )
                < MAX_REJECT_OUTPUT
            ):
                reject_examples[
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

    ordered_barcode_conflict = sorted(
        barcode_conflict_review.values(),
        key=lambda item: (
            item.score,
            item.common_count,
        ),
        reverse=True,
    )

    ordered_reject = list(
        reject_examples.values()
    )

    print_sieve_stats(
        stats
    )

    print()
    print(
        "=" * 80
    )
    print(
        "REJECT REASONS"
    )
    print(
        "=" * 80
    )

    if not reject_reason_counts:
        print(
            "none"
        )
    else:
        for (
            reason,
            count,
        ) in reject_reason_counts.most_common():
            print(
                f"{reason}: {count}"
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
        "BARCODE CONFLICT REVIEW"
    )
    print(
        "=" * 80
    )

    if not ordered_barcode_conflict:
        print(
            "none"
        )

    for index, item in enumerate(
        ordered_barcode_conflict[
            :MAX_BARCODE_CONFLICT_OUTPUT
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
