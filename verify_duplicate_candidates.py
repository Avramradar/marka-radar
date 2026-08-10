from __future__ import annotations

import asyncio
import html
import json
import re
from collections import Counter
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Iterable

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
    normalize_package_unit,
    normalized,
    package_values_compatible,
)


#
# MarkaRadar Duplicate Candidate Verifier v3
#
# Главное изменение относительно v2:
#
# - ручного списка CANDIDATE_PAIRS больше нет;
# - verifier читает duplicate_candidates.json,
# который создаёт scan_product_duplicates.py v11;
# - автоматически проверяются ВСЕ кандидаты классов:
# AUTO_SAFE
# REVIEW
# BARCODE_CONFLICT_REVIEW
# - результаты сохраняются в:
# duplicate_verification_results.json
# - БД НЕ изменяется;
# - автоматический merge НЕ выполняется.
#
# Важный принцип:
#
# Даже если scanner назвал пару AUTO_SAFE, verifier
# не обязан подтверждать её как SAME_SKU.
# Второй слой проверки остаётся независимым и
# консервативным.
#


CANDIDATE_JSON_PATH = Path(
    "duplicate_candidates.json"
)

VERIFICATION_JSON_PATH = Path(
    "duplicate_verification_results.json"
)

ALLOWED_SCANNER_CLASSES = {
    "AUTO_SAFE",
    "REVIEW",
    "BARCODE_CONFLICT_REVIEW",
}

MAX_RESULT_OUTPUT_PER_CLASS = 50
FINAL_TOP_IDS = 30


class VerificationClass(StrEnum):
    CONFIRMED_SAME_SKU = "CONFIRMED_SAME_SKU"
    NEEDS_MANUAL_REVIEW = "NEEDS_MANUAL_REVIEW"
    CONFIRMED_DIFFERENT_SKU = "CONFIRMED_DIFFERENT_SKU"


@dataclass(slots=True, frozen=True)
class ScannerCandidate:
    classification: str
    reason: str
    score: float
    left_id: int
    right_id: int


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
class PairEvidence:
    same_brand: bool

    same_name: bool
    name_coverage: float
    name_jaccard: float
    name_common_count: int

    category_compatible: bool

    left_barcode: str | None
    right_barcode: str | None
    same_barcode: bool
    different_known_barcodes: bool
    one_barcode_only: bool

    package_compatible: bool | None

    left_percentages: tuple[str, ...]
    right_percentages: tuple[str, ...]

    left_counts: tuple[str, ...]
    right_counts: tuple[str, ...]

    left_name_packages: tuple[str, ...]
    right_name_packages: tuple[str, ...]

    left_variants: tuple[str, ...]
    right_variants: tuple[str, ...]

    left_only_variants: tuple[str, ...]
    right_only_variants: tuple[str, ...]

    subtype_equal: bool | None
    same_family: bool | None

    shared_source_identity: bool
    independent_external_sources: bool
    same_provider_different_source_ids: bool

    positive_evidence: tuple[str, ...]
    negative_evidence: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class VerificationResult:
    scanner_classification: str
    scanner_reason: str
    scanner_score: float

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

    left_variants: tuple[str, ...]
    right_variants: tuple[str, ...]

    left_only_variants: tuple[str, ...]
    right_only_variants: tuple[str, ...]

    positive_evidence: tuple[str, ...]
    negative_evidence: tuple[str, ...]


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

UNICODE_WORD_PATTERN = re.compile(
    r"[^\W_]+",
    flags=re.UNICODE,
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


GENERIC_CATEGORY_NAMES = {
    "",
    "продукты",
    "продукт",
    "еда",
    "food",
    "foods",
    "products",
    "product",
    "прочее",
    "другое",
    "other",
}


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


def comparable_text( value: object, ) -> str:
    return (
        clean_html_text(
            value
        )
        .casefold()
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
            f"{match.group(2).casefold().replace('ё', 'е')}"
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


def unicode_tokens( value: str | None, ) -> list[str]:
    text = comparable_text(
        value
    )

    return [
        token
        for token
        in UNICODE_WORD_PATTERN.findall(
            text
        )
        if token
    ]


def tokenize_brand( brand_name: str | None, ) -> set[str]:
    return {
        token
        for token
        in unicode_tokens(
            brand_name
        )
        if len(token) >= 3
    }


def variant_tokens( value: str | None, *, brand_name: str | None, ) -> set[str]:
    brand_tokens = tokenize_brand(
        brand_name
    )

    result: set[str] = set()

    for token in unicode_tokens(
        value
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


def category_bucket( category_name: str | None, ) -> str:
    key = normalized_clean(
        category_name
    )

    if not key:
        return ""

    for index, group in enumerate(
        CATEGORY_EQUIVALENCE_GROUPS,
        start=1,
    ):
        normalized_group = {
            normalized_clean(
                item
            )
            for item in group
        }

        if key in normalized_group:
            return (
                f"equiv:{index}"
            )

    return key


def is_generic_category_name( category_name: str | None, ) -> bool:
    key = normalized_clean(
        category_name
    )

    return (
        key
        in {
            normalized_clean(
                item
            )
            for item
            in GENERIC_CATEGORY_NAMES
        }
    )


def categories_compatible( left: Category, right: Category, ) -> bool:
    if left.id == right.id:
        return True

    left_bucket = category_bucket(
        left.name
    )

    right_bucket = category_bucket(
        right.name
    )

    if (
        left_bucket
        and right_bucket
        and left_bucket
        == right_bucket
    ):
        return True

    if (
        is_generic_category_name(
            left.name
        )
        or is_generic_category_name(
            right.name
        )
    ):
        return True

    return False


def same_optional_text( left: str | None, right: str | None, ) -> bool | None:
    left_value = normalized_clean(
        left
    )

    right_value = normalized_clean(
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
        normalized_clean(
            source.provider
        )
        for source
        in bundle.sources
        if normalized_clean(
            source.provider
        )
    }


def source_identity_set( bundle: ProductBundle, ) -> set[
    tuple[str, str]
]:
    return {
        (
            normalized_clean(
                source.provider
            ),
            clean_text(
                source.source_id
            ),
        )
        for source
        in bundle.sources
        if (
            normalized_clean(
                source.provider
            )
            and clean_text(
                source.source_id
            )
        )
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

    score += min(
        text_quality_score(
            product.description
        ),
        20.0,
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


def pair_key( left_id: int, right_id: int, ) -> tuple[int, int]:
    return (
        min(
            int(left_id),
            int(right_id),
        ),
        max(
            int(left_id),
            int(right_id),
        ),
    )


def load_scanner_candidates() -> list[
    ScannerCandidate
]:
    if not CANDIDATE_JSON_PATH.exists():
        raise FileNotFoundError(
            "duplicate_candidates.json not found. "
            "Run scan_product_duplicates.py v11 first."
        )

    payload = json.loads(
        CANDIDATE_JSON_PATH.read_text(
            encoding="utf-8"
        )
    )

    raw_candidates = payload.get(
        "candidates",
        []
    )

    if not isinstance(
        raw_candidates,
        list,
    ):
        raise ValueError(
            "Invalid duplicate_candidates.json: "
            "'candidates' must be a list."
        )

    deduplicated: dict[
        tuple[int, int],
        ScannerCandidate,
    ] = {}

    for raw in raw_candidates:
        if not isinstance(
            raw,
            dict,
        ):
            continue

        classification = clean_text(
            raw.get(
                "classification"
            )
        )

        if (
            classification
            not in ALLOWED_SCANNER_CLASSES
        ):
            continue

        try:
            left_id = int(
                raw[
                    "left_id"
                ]
            )

            right_id = int(
                raw[
                    "right_id"
                ]
            )

            score = float(
                raw.get(
                    "score",
                    0.0,
                )
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        key = pair_key(
            left_id,
            right_id,
        )

        candidate = ScannerCandidate(
            classification=classification,
            reason=clean_text(
                raw.get(
                    "reason"
                )
            ),
            score=score,
            left_id=key[0],
            right_id=key[1],
        )

        previous = deduplicated.get(
            key
        )

        if (
            previous is None
            or candidate.score
            > previous.score
        ):
            deduplicated[
                key
            ] = candidate

    return sorted(
        deduplicated.values(),
        key=lambda item: (
            item.classification,
            -item.score,
            item.left_id,
            item.right_id,
        ),
    )


async def load_bundles( *, product_ids: set[int], ) -> dict[
    int,
    ProductBundle
]:
    if not product_ids:
        return {}

    async with (
        async_session_maker()
        as session
    ):
        product_result = await session.execute(
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
                Product.id.in_(
                    sorted(
                        product_ids
                    )
                )
            )
        )

        product_rows = (
            product_result.all()
        )

        source_result = await session.execute(
            select(
                ProductSource
            )
            .where(
                ProductSource.product_id.in_(
                    sorted(
                        product_ids
                    )
                )
            )
            .order_by(
                ProductSource.product_id.asc(),
                ProductSource.provider.asc(),
                ProductSource.source_id.asc(),
            )
        )

        sources_by_product: dict[
            int,
            list[SourceInfo],
        ] = defaultdict(
            list
        )

        for source in (
            source_result.scalars().all()
        ):
            sources_by_product[
                source.product_id
            ].append(
                SourceInfo(
                    provider=(
                        source.provider
                    ),
                    source_id=(
                        source.source_id
                    ),
                    source_url=(
                        source.source_url
                    ),
                )
            )

        bundles: dict[
            int,
            ProductBundle,
        ] = {}

        for (
            product,
            brand,
            category,
            family,
        ) in product_rows:
            bundles[
                product.id
            ] = ProductBundle(
                product=product,
                brand=brand,
                category=category,
                family=family,
                sources=tuple(
                    sources_by_product.get(
                        product.id,
                        [],
                    )
                ),
            )

        return bundles


def build_evidence( *, left: ProductBundle, right: ProductBundle, ) -> PairEvidence:
    positive: list[str] = []
    negative: list[str] = []

    left_product = left.product
    right_product = right.product

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
        and coverage >= 0.75
        and jaccard >= 0.50
    ):
        positive.append(
            "strong_name_similarity"
        )
    elif (
        common_count >= 1
        and coverage >= 0.50
    ):
        positive.append(
            "related_name"
        )
    else:
        negative.append(
            "weak_name_similarity"
        )

    category_compatible = (
        categories_compatible(
            left.category,
            right.category,
        )
    )

    if category_compatible:
        positive.append(
            "compatible_category"
        )
    else:
        negative.append(
            "different_category"
        )

    left_barcode = normalize_barcode(
        left_product.barcode
    )

    right_barcode = normalize_barcode(
        right_product.barcode
    )

    same_barcode = bool(
        left_barcode
        and right_barcode
        and left_barcode
        == right_barcode
    )

    different_known_barcodes = bool(
        left_barcode
        and right_barcode
        and left_barcode
        != right_barcode
    )

    one_barcode_only = (
        bool(
            left_barcode
        )
        != bool(
            right_barcode
        )
    )

    if same_barcode:
        positive.append(
            "same_barcode"
        )

    elif different_known_barcodes:
        negative.append(
            "different_barcode"
        )

    elif one_barcode_only:
        positive.append(
            "barcode_known_on_one_side"
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

    if package_compatibility is True:
        positive.append(
            "same_package"
        )

    elif package_compatibility is False:
        negative.append(
            "different_package"
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

    if values_conflict(
        left_percentages,
        right_percentages,
    ):
        negative.append(
            "different_percentage"
        )
    elif (
        left_percentages
        and right_percentages
        and left_percentages
        == right_percentages
    ):
        positive.append(
            "same_percentage"
        )

    if values_conflict(
        left_counts,
        right_counts,
    ):
        negative.append(
            "different_count"
        )
    elif (
        left_counts
        and right_counts
        and left_counts
        == right_counts
    ):
        positive.append(
            "same_count"
        )

    if values_conflict(
        left_name_packages,
        right_name_packages,
    ):
        negative.append(
            "different_name_package"
        )
    elif (
        left_name_packages
        and right_name_packages
        and left_name_packages
        == right_name_packages
    ):
        positive.append(
            "same_name_package"
        )

    left_variants_set = variant_tokens(
        left_product.name,
        brand_name=left.brand.name,
    )

    right_variants_set = variant_tokens(
        right_product.name,
        brand_name=right.brand.name,
    )

    common_variants = (
        left_variants_set
        & right_variants_set
    )

    left_only_variants = (
        left_variants_set
        - common_variants
    )

    right_only_variants = (
        right_variants_set
        - common_variants
    )

    if (
        left_only_variants
        and right_only_variants
    ):
        negative.append(
            "different_variant_tokens"
        )

    elif (
        left_only_variants
        or right_only_variants
    ):
        negative.append(
            "one_sided_variant_tokens"
        )

    elif (
        left_variants_set
        and right_variants_set
    ):
        positive.append(
            "same_variant_tokens"
        )

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

    same_family: bool | None = None

    if (
        left.family is not None
        and right.family is not None
    ):
        same_family = (
            left.family.id
            == right.family.id
        )

        if same_family:
            positive.append(
                "same_family"
            )
        else:
            negative.append(
                "different_family"
            )

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

    shared_source_identity = bool(
        left_source_ids
        & right_source_ids
    )

    independent_external_sources = bool(
        left.sources
        and right.sources
        and len(
            left_providers
            | right_providers
        ) >= 2
    )

    same_provider_different_source_ids = bool(
        left.sources
        and right.sources
        and left_providers
        and left_providers
        == right_providers
        and not shared_source_identity
    )

    if shared_source_identity:
        positive.append(
            "shared_provider_source_identity"
        )

    if independent_external_sources:
        positive.append(
            "independent_external_sources"
        )

    if same_provider_different_source_ids:
        negative.append(
            "same_provider_different_source_ids"
        )

    return PairEvidence(
        same_brand=same_brand,
        same_name=same_name,
        name_coverage=coverage,
        name_jaccard=jaccard,
        name_common_count=common_count,
        category_compatible=category_compatible,
        left_barcode=left_barcode,
        right_barcode=right_barcode,
        same_barcode=same_barcode,
        different_known_barcodes=(
            different_known_barcodes
        ),
        one_barcode_only=(
            one_barcode_only
        ),
        package_compatible=(
            package_compatibility
        ),
        left_percentages=(
            left_percentages
        ),
        right_percentages=(
            right_percentages
        ),
        left_counts=(
            left_counts
        ),
        right_counts=(
            right_counts
        ),
        left_name_packages=(
            left_name_packages
        ),
        right_name_packages=(
            right_name_packages
        ),
        left_variants=tuple(
            sorted(
                left_variants_set
            )
        ),
        right_variants=tuple(
            sorted(
                right_variants_set
            )
        ),
        left_only_variants=tuple(
            sorted(
                left_only_variants
            )
        ),
        right_only_variants=tuple(
            sorted(
                right_only_variants
            )
        ),
        subtype_equal=subtype_equal,
        same_family=same_family,
        shared_source_identity=(
            shared_source_identity
        ),
        independent_external_sources=(
            independent_external_sources
        ),
        same_provider_different_source_ids=(
            same_provider_different_source_ids
        ),
        positive_evidence=tuple(
            positive
        ),
        negative_evidence=tuple(
            negative
        ),
    )


def make_result( *, scanner: ScannerCandidate, classification: VerificationClass, reason: str, confidence: float, left: ProductBundle, right: ProductBundle, evidence: PairEvidence, canonical_product_id: int | None, ) -> VerificationResult:
    return VerificationResult(
        scanner_classification=(
            scanner.classification
        ),
        scanner_reason=(
            scanner.reason
        ),
        scanner_score=(
            scanner.score
        ),
        classification=classification,
        reason=reason,
        confidence=confidence,
        left_id=left.product.id,
        right_id=right.product.id,
        canonical_product_id=(
            canonical_product_id
        ),
        left_name=left.product.name,
        right_name=right.product.name,
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
        left_sources=left.sources,
        right_sources=right.sources,
        left_variants=(
            evidence.left_variants
        ),
        right_variants=(
            evidence.right_variants
        ),
        left_only_variants=(
            evidence.left_only_variants
        ),
        right_only_variants=(
            evidence.right_only_variants
        ),
        positive_evidence=(
            evidence.positive_evidence
        ),
        negative_evidence=(
            evidence.negative_evidence
        ),
    )


def verify_pair( *, scanner: ScannerCandidate, left: ProductBundle, right: ProductBundle, ) -> VerificationResult:
    evidence = build_evidence(
        left=left,
        right=right,
    )

    negative = set(
        evidence.negative_evidence
    )

    positive = set(
        evidence.positive_evidence
    )

    hard_different = {
        "different_brand",
        "different_package",
        "different_percentage",
        "different_count",
        "different_name_package",
        "different_variant_tokens",
    }

    if (
        hard_different
        & negative
    ):
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.CONFIRMED_DIFFERENT_SKU
            ),
            reason=(
                "hard_identity_conflict"
            ),
            confidence=99.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    if (
        evidence.different_known_barcodes
        and (
            "one_sided_variant_tokens"
            in negative
            or "different_subtype"
            in negative
        )
    ):
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.CONFIRMED_DIFFERENT_SKU
            ),
            reason=(
                "different_barcode_plus_variant_difference"
            ),
            confidence=99.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    #
    # Два разных известных barcode НИКОГДА
    # автоматически не подтверждают SAME_SKU.
    #
    if evidence.different_known_barcodes:
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "barcode_conflict_requires_external_confirmation"
            ),
            confidence=75.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    if (
        "one_sided_variant_tokens"
        in negative
    ):
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "one_sided_sku_variant_requires_confirmation"
            ),
            confidence=70.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    if (
        "different_subtype"
        in negative
    ):
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "subtype_difference_requires_confirmation"
            ),
            confidence=70.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    strong_name = bool(
        evidence.same_name
        or (
            evidence.name_common_count >= 3
            and evidence.name_coverage >= 0.95
            and evidence.name_jaccard >= 0.80
        )
    )

    core_identity = bool(
        evidence.same_brand
        and evidence.category_compatible
        and strong_name
        and evidence.package_compatible is True
    )

    exact_external_identity = bool(
        evidence.shared_source_identity
    )

    same_barcode_confirmation = bool(
        evidence.same_barcode
    )

    independent_source_confirmation = bool(
        evidence.independent_external_sources
        and evidence.same_name
        and evidence.same_brand
        and evidence.category_compatible
        and evidence.package_compatible is True
        and not (
            {
                "one_sided_variant_tokens",
                "different_variant_tokens",
                "different_subtype",
                "different_barcode",
            }
            & negative
        )
    )

    independently_confirmed = bool(
        same_barcode_confirmation
        or exact_external_identity
        or independent_source_confirmation
    )

    if (
        core_identity
        and independently_confirmed
    ):
        canonical_id = (
            choose_canonical_product(
                left=left,
                right=right,
            )
        )

        if same_barcode_confirmation:
            confidence = 100.0
            reason = (
                "same_barcode_plus_core_identity"
            )

        elif exact_external_identity:
            confidence = 99.0
            reason = (
                "same_external_identity_plus_core_identity"
            )

        else:
            confidence = 98.0
            reason = (
                "independent_sources_plus_exact_core_identity"
            )

        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.CONFIRMED_SAME_SKU
            ),
            reason=reason,
            confidence=confidence,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=(
                canonical_id
            ),
        )

    if core_identity:
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "core_identity_matches_but_no_independent_confirmation"
            ),
            confidence=85.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    related_identity = bool(
        evidence.same_brand
        and evidence.category_compatible
        and evidence.package_compatible is not False
        and (
            evidence.same_name
            or evidence.name_common_count >= 1
            or "same_percentage"
            in positive
            or "same_name_package"
            in positive
        )
    )

    if related_identity:
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.NEEDS_MANUAL_REVIEW
            ),
            reason=(
                "related_core_identity_needs_more_evidence"
            ),
            confidence=75.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    if (
        "different_category"
        in negative
        and "weak_name_similarity"
        in negative
    ):
        return make_result(
            scanner=scanner,
            classification=(
                VerificationClass.CONFIRMED_DIFFERENT_SKU
            ),
            reason=(
                "weak_name_and_incompatible_category"
            ),
            confidence=95.0,
            left=left,
            right=right,
            evidence=evidence,
            canonical_product_id=None,
        )

    return make_result(
        scanner=scanner,
        classification=(
            VerificationClass.NEEDS_MANUAL_REVIEW
        ),
        reason=(
            "insufficient_evidence_for_safe_decision"
        ),
        confidence=60.0,
        left=left,
        right=right,
        evidence=evidence,
        canonical_product_id=None,
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
        "scanner:",
        item.scanner_classification,
        "|",
        item.scanner_score,
        "|",
        item.scanner_reason,
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
        "variant_tokens:",
        item.left_variants,
        "|",
        item.right_variants,
    )

    print(
        "only_variants:",
        item.left_only_variants,
        "|",
        item.right_only_variants,
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


def result_to_json( item: VerificationResult, ) -> dict:
    data = asdict(
        item
    )

    data[
        "classification"
    ] = item.classification.value

    return data


def write_verification_json( *, scanner_candidates: list[ ScannerCandidate ], results: list[ VerificationResult ], missing_pairs: list[ tuple[int, int] ], ) -> None:
    counts = Counter(
        item.classification.value
        for item in results
    )

    payload = {
        "schema_version": 1,
        "verifier_version": "v3",
        "generated_at_utc": (
            datetime.now(
                timezone.utc
            )
            .isoformat()
        ),
        "source_file": str(
            CANDIDATE_JSON_PATH
        ),
        "database_changes": False,
        "auto_merge_executed": False,
        "pairs_requested": len(
            scanner_candidates
        ),
        "pairs_verified": len(
            results
        ),
        "pairs_missing": len(
            missing_pairs
        ),
        "counts": {
            key: counts[
                key
            ]
            for key
            in (
                VerificationClass.CONFIRMED_SAME_SKU.value,
                VerificationClass.NEEDS_MANUAL_REVIEW.value,
                VerificationClass.CONFIRMED_DIFFERENT_SKU.value,
            )
        },
        "missing_pairs": [
            {
                "left_id": left_id,
                "right_id": right_id,
            }
            for (
                left_id,
                right_id,
            )
            in missing_pairs
        ],
        "results": [
            result_to_json(
                item
            )
            for item
            in results
        ],
    }

    VERIFICATION_JSON_PATH.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
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


def print_class_block( *, title: str, items: list[ VerificationResult ], ) -> None:
    print()
    print(
        "=" * 80
    )
    print(
        title
    )
    print(
        "=" * 80
    )

    if not items:
        print(
            "none"
        )
        return

    for index, item in enumerate(
        items[
            :MAX_RESULT_OUTPUT_PER_CLASS
        ],
        start=1,
    ):
        print_result(
            index=index,
            item=item,
        )

    if (
        len(
            items
        )
        > MAX_RESULT_OUTPUT_PER_CLASS
    ):
        print(
            "-" * 80
        )

        print(
            "OUTPUT TRUNCATED:",
            (
                len(
                    items
                )
                - MAX_RESULT_OUTPUT_PER_CLASS
            ),
            "more result(s) stored in",
            str(
                VERIFICATION_JSON_PATH
            ),
        )


def print_final_summary( *, scanner_candidates: list[ ScannerCandidate ], results: list[ VerificationResult ], missing_pairs: list[ tuple[int, int] ], ) -> None:
    counts = Counter(
        item.classification.value
        for item in results
    )

    scanner_counts = Counter(
        item.classification
        for item in scanner_candidates
    )

    same_items = sorted(
        (
            item
            for item in results
            if (
                item.classification
                == VerificationClass.CONFIRMED_SAME_SKU
            )
        ),
        key=lambda item: (
            item.confidence,
            item.scanner_score,
            -item.left_id,
            -item.right_id,
        ),
        reverse=True,
    )

    review_items = sorted(
        (
            item
            for item in results
            if (
                item.classification
                == VerificationClass.NEEDS_MANUAL_REVIEW
            )
        ),
        key=lambda item: (
            item.confidence,
            item.scanner_score,
            -item.left_id,
            -item.right_id,
        ),
        reverse=True,
    )

    different_items = sorted(
        (
            item
            for item in results
            if (
                item.classification
                == VerificationClass.CONFIRMED_DIFFERENT_SKU
            )
        ),
        key=lambda item: (
            item.confidence,
            item.scanner_score,
            -item.left_id,
            -item.right_id,
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
        "Input file:",
        str(
            CANDIDATE_JSON_PATH
        ),
    )

    print(
        "Scanner candidates:",
        len(
            scanner_candidates
        ),
    )

    print(
        " AUTO_SAFE:",
        scanner_counts[
            "AUTO_SAFE"
        ],
    )

    print(
        " REVIEW:",
        scanner_counts[
            "REVIEW"
        ],
    )

    print(
        " BARCODE_CONFLICT_REVIEW:",
        scanner_counts[
            "BARCODE_CONFLICT_REVIEW"
        ],
    )

    print()

    print(
        "Pairs verified:",
        len(
            results
        ),
    )

    print(
        "Pairs missing:",
        len(
            missing_pairs
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

    if missing_pairs:
        print(
            "MISSING IDS:",
            ", ".join(
                f"{left}<->{right}"
                for (
                    left,
                    right,
                )
                in missing_pairs[
                    :FINAL_TOP_IDS
                ]
            ),
        )

    print()

    print(
        "VERIFICATION JSON:",
        str(
            VERIFICATION_JSON_PATH
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
        "MarkaRadar Duplicate Candidate Verifier v3"
    )

    print(
        "MODE: AUTOMATIC DEEP VERIFICATION OF ALL SCANNER CANDIDATES"
    )

    print(
        "INPUT:",
        str(
            CANDIDATE_JSON_PATH
        ),
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

    scanner_candidates = (
        load_scanner_candidates()
    )

    print(
        "Scanner candidates loaded:",
        len(
            scanner_candidates
        ),
    )

    product_ids = {
        product_id
        for candidate
        in scanner_candidates
        for product_id
        in (
            candidate.left_id,
            candidate.right_id,
        )
    }

    bundles = await load_bundles(
        product_ids=product_ids
    )

    print(
        "Unique candidate products loaded:",
        len(
            bundles
        ),
    )

    results: list[
        VerificationResult
    ] = []

    missing_pairs: list[
        tuple[int, int]
    ] = []

    for scanner in scanner_candidates:
        left = bundles.get(
            scanner.left_id
        )

        right = bundles.get(
            scanner.right_id
        )

        if (
            left is None
            or right is None
        ):
            missing_pairs.append(
                (
                    scanner.left_id,
                    scanner.right_id,
                )
            )

            continue

        result = verify_pair(
            scanner=scanner,
            left=left,
            right=right,
        )

        results.append(
            result
        )

    same_items = sorted(
        [
            item
            for item in results
            if (
                item.classification
                == VerificationClass.CONFIRMED_SAME_SKU
            )
        ],
        key=lambda item: (
            item.confidence,
            item.scanner_score,
        ),
        reverse=True,
    )

    review_items = sorted(
        [
            item
            for item in results
            if (
                item.classification
                == VerificationClass.NEEDS_MANUAL_REVIEW
            )
        ],
        key=lambda item: (
            item.confidence,
            item.scanner_score,
        ),
        reverse=True,
    )

    different_items = sorted(
        [
            item
            for item in results
            if (
                item.classification
                == VerificationClass.CONFIRMED_DIFFERENT_SKU
            )
        ],
        key=lambda item: (
            item.confidence,
            item.scanner_score,
        ),
        reverse=True,
    )

    write_verification_json(
        scanner_candidates=(
            scanner_candidates
        ),
        results=results,
        missing_pairs=(
            missing_pairs
        ),
    )

    print_class_block(
        title=(
            "CONFIRMED SAME SKU"
        ),
        items=same_items,
    )

    print_class_block(
        title=(
            "NEEDS MANUAL REVIEW"
        ),
        items=review_items,
    )

    print_class_block(
        title=(
            "CONFIRMED DIFFERENT SKU"
        ),
        items=different_items,
    )

    #
    # Финальная сводка обязательно последняя.
    #
    print_final_summary(
        scanner_candidates=(
            scanner_candidates
        ),
        results=results,
        missing_pairs=(
            missing_pairs
        ),
    )


if __name__ == "__main__":
    asyncio.run(
        main()
    )
