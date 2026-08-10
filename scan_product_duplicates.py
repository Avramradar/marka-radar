from __future__ import annotations

import asyncio
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
# MarkaRadar Duplicate Scanner v5
#
# Архитектура: "сито".
#
# Вместо того чтобы сразу глубоко сравнивать каждую пару,
# мы последовательно пропускаем товары через уровни:
#
# SIEVE 0 — сильные блоки-кандидаты
# SIEVE 1 — грубое совпадение identity-токенов
# SIEVE 2 — жёсткие структурные конфликты
# SIEVE 3 — package / проценты / количество
# SIEVE 4 — сходство названий
# SIEVE 5 — различающие SKU-токены
# SIEVE 6 — итоговая классификация
#
# На каждом следующем уровне остаётся меньше пар.
#
# Результат:
# AUTO_SAFE — можно рассматривать для автоматического merge
# REVIEW — похожи, но нужен дополнительный источник/проверка
# REJECT — доказано разные SKU или доказательств недостаточно
#
# ВАЖНО:
# Этот файл НИЧЕГО не меняет в БД.
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


@dataclass(slots=True)
class SieveStats:
    products_loaded: int = 0

    brand_category_groups: int = 0

    possible_pairs_inside_groups: int = 0
    coarse_pairs_generated: int = 0

    rejected_different_barcode: int = 0
    rejected_package_conflict: int = 0
    rejected_percentage_conflict: int = 0
    rejected_count_conflict: int = 0
    rejected_name_package_conflict: int = 0
    rejected_variant_conflict: int = 0
    rejected_weak_identity: int = 0

    passed_hard_sieve: int = 0
    passed_name_sieve: int = 0
    passed_variant_sieve: int = 0

    auto_safe: int = 0
    review: int = 0
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


#
# Слова, которые сами по себе не различают SKU.
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
    """ Нормализация, которая сохраняет: - проценты; - точки; - запятые. Это важно для: 2.5% 3.2% 1.35кг """

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
    """ Масса/объём прямо из названия. Примеры: 1.35кг -> 1.35:кг 300г -> 300:г """

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
    """ Выделяет слова, которые могут отличать SKU. Примеры: финский индейкой armonioso intenso cremoso forte """

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


def calculate_possible_pairs( size: int, ) -> int:
    if size < 2:
        return 0

    return (
        size
        * (
            size - 1
        )
        // 2
    )


def coarse_pair_key( left_id: int, right_id: int, ) -> tuple[int, int]:
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


def generate_coarse_pairs( group: list[ProductMeta], ) -> set[
    tuple[int, int]
]:
    """ SIEVE 0. Самое крупное сито. Вместо полного O(n²) сравнения всех товаров внутри brand+category строим инвертированный индекс по identity-токенам. Пара становится кандидатом, если: - есть хотя бы один общий identity-токен; ИЛИ - normalized_name полностью одинаков; ИЛИ - barcode одинаковый. Это резко сокращает число пар до глубокого анализа. """

    pairs: set[
        tuple[int, int]
    ] = set()

    token_index: dict[
        str,
        list[int],
    ] = defaultdict(
        list
    )

    exact_name_index: dict[
        str,
        list[int],
    ] = defaultdict(
        list
    )

    barcode_index: dict[
        str,
        list[int],
    ] = defaultdict(
        list
    )

    by_id: dict[
        int,
        ProductMeta,
    ] = {}

    for item in group:
        product = item.product

        by_id[
            product.id
        ] = item

        for token in item.identity_tokens:
            token_index[
                token
            ].append(
                product.id
            )

        exact_name = normalized(
            product.name
        )

        if exact_name:
            exact_name_index[
                exact_name
            ].append(
                product.id
            )

        barcode = normalize_barcode(
            product.barcode
        )

        if barcode:
            barcode_index[
                barcode
            ].append(
                product.id
            )

    for ids in token_index.values():
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
                coarse_pair_key(
                    left_id,
                    right_id,
                )
            )

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
                coarse_pair_key(
                    left_id,
                    right_id,
                )
            )

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
                coarse_pair_key(
                    left_id,
                    right_id,
                )
            )

    return pairs


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

    left_name_packages = extract_name_packages(
        left.name
    )

    right_name_packages = extract_name_packages(
        right.name
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
    # SIEVE 2 — жёсткие SKU-конфликты.
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

    if package_compatibility is False:
        hard_conflicts.append(
            "different_package:"
            f"{package_text(left)}"
            "!="
            f"{package_text(right)}"
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

    #
    # SIEVE 5 — вариантные SKU-токены.
    #
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

    if bool(left_barcode) != bool(right_barcode):
        soft_differences.append(
            "barcode_present_only_on_one_side"
        )

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


def hard_conflict_reason( evidence: PairEvidence, ) -> str:
    for conflict in evidence.hard_conflicts:
        if conflict.startswith(
            "different_barcode:"
        ):
            return "different_barcode"

        if conflict.startswith(
            "different_package:"
        ):
            return "different_package"

        if conflict.startswith(
            "different_percentage:"
        ):
            return "different_percentage"

        if conflict.startswith(
            "different_count:"
        ):
            return "different_count"

        if conflict.startswith(
            "different_name_package:"
        ):
            return "different_name_package"

        if conflict.startswith(
            "different_variant_tokens:"
        ):
            return "different_variant_tokens"

    return "hard_identity_conflict"


def classify_pair( *, left: Product, right: Product, evidence: PairEvidence, ) -> tuple[
    CandidateClass,
    str,
    float,
]:
    """ SIEVE 6 — финальный уровень. До сюда доходят только пары, которые пережили все более крупные фильтры. """

    if evidence.hard_conflicts:
        return (
            CandidateClass.REJECT,
            hard_conflict_reason(
                evidence
            ),
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

    same_barcode = bool(
        evidence.left_barcode
        and evidence.right_barcode
        and evidence.left_barcode
        == evidence.right_barcode
    )

    #
    # Самое мелкое сито №1:
    # одинаковый barcode.
    #
    if same_barcode:
        if evidence.soft_differences:
            return (
                CandidateClass.REVIEW,
                "same_barcode_but_card_data_differs",
                98.0,
            )

        return (
            CandidateClass.AUTO_SAFE,
            "same_barcode",
            100.0,
        )

    #
    # Самое мелкое сито №2:
    # точное имя + подтверждённая упаковка.
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
    # Самое мелкое сито №3:
    # практически идентичное имя +
    # одинаковая известная упаковка.
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
    # REVIEW:
    # barcode есть только у одной карточки.
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
    # REVIEW:
    # сильное имя, но данных ещё мало.
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

        rows = []

        for (
            product,
            brand,
            category,
            source_count,
        ) in result.all():
            rows.append(
                ProductMeta(
                    product=product,
                    brand=brand,
                    category=category,
                    source_count=int(
                        source_count
                        or 0
                    ),
                    identity_tokens=frozenset(
                        identity_name_tokens(
                            product.name
                        )
                    ),
                )
            )

        return rows


def update_reject_stats( *, reason: str, stats: SieveStats, ) -> None:
    stats.reject += 1

    if reason == "different_barcode":
        stats.rejected_different_barcode += 1

    elif reason == "different_package":
        stats.rejected_package_conflict += 1

    elif reason == "different_percentage":
        stats.rejected_percentage_conflict += 1

    elif reason == "different_count":
        stats.rejected_count_conflict += 1

    elif reason == "different_name_package":
        stats.rejected_name_package_conflict += 1

    elif reason == "different_variant_tokens":
        stats.rejected_variant_conflict += 1

    else:
        stats.rejected_weak_identity += 1


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
        "Brand+category groups:",
        stats.brand_category_groups,
    )

    print(
        "Possible pairs inside groups:",
        stats.possible_pairs_inside_groups,
    )

    print(
        "SIEVE 0 - coarse pairs generated:",
        stats.coarse_pairs_generated,
    )

    print(
        "SIEVE 2 - rejected different barcode:",
        stats.rejected_different_barcode,
    )

    print(
        "SIEVE 3 - rejected package conflict:",
        stats.rejected_package_conflict,
    )

    print(
        "SIEVE 3 - rejected percentage conflict:",
        stats.rejected_percentage_conflict,
    )

    print(
        "SIEVE 3 - rejected count conflict:",
        stats.rejected_count_conflict,
    )

    print(
        "SIEVE 3 - rejected name-package conflict:",
        stats.rejected_name_package_conflict,
    )

    print(
        "Passed hard-identity sieve:",
        stats.passed_hard_sieve,
    )

    print(
        "Passed name-similarity sieve:",
        stats.passed_name_sieve,
    )

    print(
        "SIEVE 5 - rejected variant conflict:",
        stats.rejected_variant_conflict,
    )

    print(
        "Passed variant sieve:",
        stats.passed_variant_sieve,
    )

    print(
        "Rejected weak identity:",
        stats.rejected_weak_identity,
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
        "REJECT:",
        stats.reject,
    )


async def main() -> None:
    print(
        "=" * 80
    )

    print(
        "MarkaRadar Duplicate Scanner v5"
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

    stats.brand_category_groups = len(
        by_brand_category
    )

    auto_safe: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    review: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject_examples: dict[
        tuple[int, int],
        DuplicateCandidate,
    ] = {}

    reject_reason_counts: Counter[str] = Counter()

    for group in by_brand_category.values():
        group_size = len(
            group
        )

        if group_size < 2:
            continue

        stats.possible_pairs_inside_groups += (
            calculate_possible_pairs(
                group_size
            )
        )

        by_id = {
            item.product.id: item
            for item in group
        }

        coarse_pairs = generate_coarse_pairs(
            group
        )

        stats.coarse_pairs_generated += len(
            coarse_pairs
        )

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
                left=left.product,
                right=right.product,
                brand_name=left.brand.name,
            )

            #
            # SIEVE 2 + SIEVE 3:
            # жёсткие identity / package признаки.
            #
            if evidence.hard_conflicts:
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

                key = coarse_pair_key(
                    left_id,
                    right_id,
                )

                reject_reason_counts[
                    reason
                ] += 1

                update_reject_stats(
                    reason=reason,
                    stats=stats,
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

                continue

            stats.passed_hard_sieve += 1

            #
            # SIEVE 4:
            # грубая похожесть имени.
            #
            same_name = (
                normalized(
                    left.product.name
                )
                == normalized(
                    right.product.name
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

                key = coarse_pair_key(
                    left_id,
                    right_id,
                )

                reject_reason_counts[
                    reason
                ] += 1

                update_reject_stats(
                    reason=reason,
                    stats=stats,
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

                continue

            stats.passed_name_sieve += 1

            #
            # SIEVE 5 + SIEVE 6:
            # variant-токены + финальная классификация.
            #
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

            key = coarse_pair_key(
                left_id,
                right_id,
            )

            if (
                classification
                == CandidateClass.AUTO_SAFE
            ):
                stats.passed_variant_sieve += 1
                stats.auto_safe += 1

                auto_safe[
                    key
                ] = candidate

            elif (
                classification
                == CandidateClass.REVIEW
            ):
                stats.passed_variant_sieve += 1
                stats.review += 1

                review[
                    key
                ] = candidate

            else:
                reject_reason_counts[
                    reason
                ] += 1

                update_reject_stats(
                    reason=reason,
                    stats=stats,
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
