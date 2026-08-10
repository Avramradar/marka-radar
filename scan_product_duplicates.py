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
# MarkaRadar Duplicate Scanner v6
#
# Принцип работы: многоступенчатое сито.
#
# Сначала мы строим широкие группы кандидатов,
# затем последовательно применяем всё более точные
# фильтры и НЕ делаем никаких изменений в БД.
#
# Итоговые классы:
#
# AUTO_SAFE
# Очень сильные доказательства одного SKU.
#
# REVIEW
# Сильное сходство, но данных недостаточно
# для безопасного автоматического merge.
#
# BARCODE_CONFLICT_REVIEW
# Название/бренд/упаковка могут совпадать,
# но barcode разные. Автоматически объединять
# такую пару НЕЛЬЗЯ, но её нельзя просто
# выбрасывать из анализа.
#
# REJECT
# Доказано разные SKU или слишком мало
# доказательств.
#
# ВАЖНО:
# Этот файл только анализирует данные.
# AUTO MERGE EXECUTED: NO
# DATABASE CHANGES: NONE
#


class CandidateClass(StrEnum):
    AUTO_SAFE = "AUTO_SAFE"
    REVIEW = "REVIEW"
    BARCODE_CONFLICT_REVIEW = (
        "BARCODE_CONFLICT_REVIEW"
    )
    REJECT = "REJECT"


@dataclass(slots=True, frozen=True)
class ProductMeta:
    product: Product
    brand: Brand
    category: Category
    source_count: int

    normalized_brand: str
    normalized_category: str

    identity_tokens: frozenset[str]
    searchable_tokens: frozenset[str]


@dataclass(slots=True, frozen=True)
class PairEvidence:
    same_brand: bool
    same_category: bool

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

    exact_barcode_groups: int = 0
    normalized_brand_groups: int = 0
    token_blocks: int = 0

    possible_pairs_raw: int = 0
    coarse_pairs_generated: int = 0

    passed_brand_sieve: int = 0
    passed_category_sieve: int = 0
    passed_structure_sieve: int = 0
    passed_name_sieve: int = 0

    auto_safe: int = 0
    review: int = 0
    barcode_conflict_review: int = 0
    reject: int = 0


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

HTML_ENTITY_PATTERN = re.compile(
    r"&[a-z0-9#]+;",
    flags=re.IGNORECASE,
)


#
# Слова, которые не должны считаться
# различающими SKU-признаками.
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

    #
    # HTML-мусор после старых импортов.
    #
    "quot",
    "amp",
    "lt",
    "gt",
    "nbsp",
}


#
# Категории могут отличаться между провайдерами.
# Здесь только самые очевидные родственные группы.
#
CATEGORY_EQUIVALENCE_GROUPS = (
    {
        "молочная продукция",
        "dairy",
        "dairy products",
        "milk products",
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
)


MAX_AUTO_SAFE_OUTPUT = 100
MAX_REVIEW_OUTPUT = 100
MAX_BARCODE_CONFLICT_OUTPUT = 100
MAX_REJECT_OUTPUT = 30


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

    text = html.unescape(
        text
    )

    text = HTML_ENTITY_PATTERN.sub(
        " ",
        text,
    )

    return " ".join(
        text.split()
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

        #
        # Масса вроде 300г/135кг уже отдельно
        # анализируется как package и не должна
        # считаться вариантом SKU.
        #
        if re.fullmatch(
            r"\d+(?:\d+)?"
            r"(?:г|гр|кг|мл|л)",
            token,
            flags=re.IGNORECASE,
        ):
            continue

        if re.fullmatch(
            r"\d+(?:[.,]\d+)?"
            r"(?:г|гр|кг|мл|л)",
            token,
            flags=re.IGNORECASE,
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


def category_group( value: str | None, ) -> str:
    key = normalized(
        html_clean_text(
            value
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


def categories_compatible( left: ProductMeta, right: ProductMeta, ) -> bool:
    if (
        left.category.id
        == right.category.id
    ):
        return True

    left_group = category_group(
        left.category.name
    )

    right_group = category_group(
        right.category.name
    )

    if (
        left_group
        and right_group
        and left_group
        == right_group
    ):
        return True

    #
    # Если категории различаются, но одна из них
    # слишком общая — не режем пару слишком рано.
    #
    if is_generic_category(
        left.category.name
    ):
        return True

    if is_generic_category(
        right.category.name
    ):
        return True

    return False


def brands_compatible( left: ProductMeta, right: ProductMeta, ) -> bool:
    if (
        left.brand.id
        == right.brand.id
    ):
        return True

    if (
        left.normalized_brand
        and right.normalized_brand
        and left.normalized_brand
        == right.normalized_brand
    ):
        return True

    #
    # Неизвестный бренд не является конфликтом.
    #
    if is_unknown_brand(
        left.brand.name
    ):
        return True

    if is_unknown_brand(
        right.brand.name
    ):
        return True

    return False


def common_token_count( left: ProductMeta, right: ProductMeta, ) -> int:
    return len(
        left.searchable_tokens
        & right.searchable_tokens
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
        left_product.name,
        right_product.name,
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

    if not brands_compatible(
        left,
        right,
    ):
        hard_conflicts.append(
            "different_brand:"
            f"{left.brand.name}"
            "!="
            f"{right.brand.name}"
        )

    if not categories_compatible(
        left,
        right,
    ):
        soft_differences.append(
            "different_category:"
            f"{left.category.name}"
            "|"
            f"{right.category.name}"
        )

    if (
        left_barcode
        and right_barcode
        and left_barcode
        != right_barcode
    ):
        soft_differences.append(
            "different_barcode:"
            f"{left_barcode}"
            "!="
            f"{right_barcode}"
        )

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
            left_product.package_value
            is not None
            and left_product.package_unit
        )

        right_has_package = bool(
            right_product.package_value
            is not None
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
        same_brand=brands_compatible(
            left,
            right,
        ),
        same_category=categories_compatible(
            left,
            right,
        ),
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
    left_product = left.product
    right_product = right.product

    if evidence.hard_conflicts:
        return (
            CandidateClass.REJECT,
            "hard_identity_conflict",
            0.0,
        )

    same_name = (
        normalized(
            html_clean_text(
                left_product.name
            )
        )
        == normalized(
            html_clean_text(
                right_product.name
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
    # Отдельный класс:
    # всё похоже, но barcode конфликтуют.
    #
    if different_known_barcodes:
        strong_identity = bool(
            same_name
            or (
                evidence.common_count >= 2
                and evidence.coverage >= 0.80
                and evidence.jaccard >= 0.55
            )
        )

        same_or_unknown_package = (
            evidence.package_compatible
            is not False
        )

        if (
            strong_identity
            and same_or_unknown_package
        ):
            score = (
                70.0
                + evidence.coverage * 10.0
                + evidence.jaccard * 10.0
            )

            return (
                CandidateClass.BARCODE_CONFLICT_REVIEW,
                "strong_identity_but_different_barcode",
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
            "different_barcode_and_weak_identity",
            0.0,
        )

    if same_barcode:
        if (
            evidence.package_compatible is False
        ):
            return (
                CandidateClass.REVIEW,
                "same_barcode_but_package_conflict",
                95.0,
            )

        return (
            CandidateClass.AUTO_SAFE,
            "same_barcode",
            100.0,
        )

    if (
        same_name
        and evidence.package_compatible is True
        and evidence.same_brand
    ):
        return (
            CandidateClass.AUTO_SAFE,
            "exact_name_brand_package",
            97.0,
        )

    if (
        evidence.same_brand
        and evidence.package_compatible is True
        and evidence.common_count >= 3
        and evidence.coverage >= 0.95
        and evidence.jaccard >= 0.80
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
        and evidence.same_brand
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
        evidence.same_brand
        and evidence.common_count >= 2
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

    if (
        same_name
        and evidence.same_brand
    ):
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


def build_coarse_pairs( rows: list[ProductMeta], stats: SieveStats, ) -> set[
    tuple[int, int]
]:
    """ SIEVE 0. Строим широкие кандидаты тремя независимыми путями: 1. одинаковый barcode; 2. одинаковый normalized brand + общий identity token; 3. общий identity token + совместимый/неизвестный бренд. Это позволяет не потерять карточки из разных category_id и исторические дубли брендов. """

    pairs: set[
        tuple[int, int]
    ] = set()

    barcode_index: dict[
        str,
        list[int],
    ] = defaultdict(
        list
    )

    brand_index: dict[
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
        for item in rows
    }

    for item in rows:
        barcode = normalize_barcode(
            item.product.barcode
        )

        if barcode:
            barcode_index[
                barcode
            ].append(
                item.product.id
            )

        if item.normalized_brand:
            brand_index[
                item.normalized_brand
            ].append(
                item.product.id
            )

        for token in item.identity_tokens:
            token_index[
                token
            ].append(
                item.product.id
            )

    stats.exact_barcode_groups = sum(
        1
        for ids in barcode_index.values()
        if len(ids) >= 2
    )

    stats.normalized_brand_groups = sum(
        1
        for ids in brand_index.values()
        if len(ids) >= 2
    )

    stats.token_blocks = sum(
        1
        for ids in token_index.values()
        if len(ids) >= 2
    )

    #
    # 1. SAME BARCODE.
    #
    for ids in barcode_index.values():
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
    # 2. SAME NORMALIZED BRAND.
    #
    for ids in brand_index.values():
        if len(ids) < 2:
            continue

        stats.possible_pairs_raw += (
            len(ids)
            * (
                len(ids) - 1
            )
            // 2
        )

        token_to_ids: dict[
            str,
            list[int],
        ] = defaultdict(
            list
        )

        for product_id in ids:
            item = by_id[
                product_id
            ]

            for token in item.identity_tokens:
                token_to_ids[
                    token
                ].append(
                    product_id
                )

        for token_ids in token_to_ids.values():
            if len(token_ids) < 2:
                continue

            for (
                left_id,
                right_id,
            ) in combinations(
                token_ids,
                2,
            ):
                pairs.add(
                    pair_key(
                        left_id,
                        right_id,
                    )
                )

    #
    # 3. TOKEN-FIRST FALLBACK.
    #
    # Нужен для исторических дублей брендов или
    # карточек с неизвестным брендом.
    #
    for ids in token_index.values():
        if len(ids) < 2:
            continue

        #
        # Ограничиваем очень широкие токены.
        # Если один токен встречается у сотен товаров,
        # это уже слишком слабый блок.
        #
        if len(ids) > 80:
            continue

        for (
            left_id,
            right_id,
        ) in combinations(
            ids,
            2,
        ):
            left = by_id[
                left_id
            ]

            right = by_id[
                right_id
            ]

            if brands_compatible(
                left,
                right,
            ):
                pairs.add(
                    pair_key(
                        left_id,
                        right_id,
                    )
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

            identity_tokens = frozenset(
                identity_name_tokens(
                    cleaned_name
                )
            )

            searchable_tokens = frozenset(
                token
                for token
                in WORD_PATTERN.findall(
                    normalized(
                        cleaned_name
                    )
                )
                if len(token) >= 3
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
                    normalized_category=(
                        normalized(
                            html_clean_text(
                                category.name
                            )
                        )
                    ),
                    identity_tokens=(
                        identity_tokens
                    ),
                    searchable_tokens=(
                        searchable_tokens
                    ),
                )
            )

        return items


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
        "Exact barcode groups:",
        stats.exact_barcode_groups,
    )

    print(
        "Normalized brand groups:",
        stats.normalized_brand_groups,
    )

    print(
        "Identity token blocks:",
        stats.token_blocks,
    )

    print(
        "Raw same-brand possible pairs:",
        stats.possible_pairs_raw,
    )

    print(
        "SIEVE 0 - coarse pairs generated:",
        stats.coarse_pairs_generated,
    )

    print(
        "SIEVE 1 - passed brand compatibility:",
        stats.passed_brand_sieve,
    )

    print(
        "SIEVE 2 - passed category compatibility:",
        stats.passed_category_sieve,
    )

    print(
        "SIEVE 3 - passed structural conflicts:",
        stats.passed_structure_sieve,
    )

    print(
        "SIEVE 4 - passed name similarity:",
        stats.passed_name_sieve,
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
        "MarkaRadar Duplicate Scanner v6"
    )

    print(
        "MODE: MULTI-STAGE SIEVE"
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

        #
        # SIEVE 1 — бренд.
        #
        if not brands_compatible(
            left,
            right,
        ):
            stats.reject += 1

            reject_reason_counts[
                "different_brand"
            ] += 1

            continue

        stats.passed_brand_sieve += 1

        #
        # SIEVE 2 — категория.
        #
        category_ok = categories_compatible(
            left,
            right,
        )

        if category_ok:
            stats.passed_category_sieve += 1

        #
        # SIEVE 3 — структура SKU.
        #
        evidence = build_evidence(
            left=left,
            right=right,
        )

        structural_hard_conflicts = tuple(
            conflict
            for conflict
            in evidence.hard_conflicts
            if not conflict.startswith(
                "different_brand:"
            )
        )

        if structural_hard_conflicts:
            classification = (
                CandidateClass.REJECT
            )

            reason = (
                "hard_identity_conflict"
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

            stats.reject += 1

            reject_reason_counts[
                reason
            ] += 1

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

        stats.passed_structure_sieve += 1

        #
        # SIEVE 4 — имя.
        #
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

            stats.reject += 1

            reject_reason_counts[
                reason
            ] += 1

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

        stats.passed_name_sieve += 1

        #
        # SIEVE 5/6 — финальная классификация.
        #
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
            stats.reject += 1

            reject_reason_counts[
                reason
            ] += 1

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
