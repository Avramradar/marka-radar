from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_source import ProductSource
from app.utils.text import normalize_text


logger = logging.getLogger(__name__)


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


GENERIC_PRODUCT_NAMES = {
    "кофе",
    "молоко",
    "чай",
    "вода",
    "пицца",
    "сыр",
    "масло",
    "йогурт",
    "кефир",
    "сельдь",
    "сок",
    "сметана",
    "творог",
    "сливки",
    "колбаса",
    "майонез",
    "паштет",
    "печенье",
    "шоколад",
    "мороженое",
    "пельмени",
    "вареники",
}


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


#
# Разные внешние каталоги могут называть одну
# и ту же конкретную категорию по-разному.
# Это только разрешение для fuzzy-match:
# группа НЕ доказывает, что SKU одинаковый.
#
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


PLACEHOLDER_IMAGE_MARKERS = (
    "placeholder",
    "no-image",
    "no_image",
    "default-product",
    "default_product",
    "image-not-found",
    "image_not_found",
)


MARKETING_DESCRIPTION_MARKERS = (
    "купить",
    "заказать",
    "доставка",
    "акция",
    "скидка",
    "выгодная цена",
    "лучшая цена",
)


PACKAGE_FAMILIES = {
    "г": "mass",
    "кг": "mass",
    "мл": "volume",
    "л": "volume",
}


#
# Слова, которые полезны для показа пользователю,
# но почти ничего не доказывают при идентификации SKU.
#
NAME_MATCH_STOPWORDS = {
    *GENERIC_PRODUCT_NAMES,
    "продукт",
    "продукты",
    "товар",
    "товары",
    "традиции",
    "традиционный",
    "традиционные",
    "классический",
    "классическая",
    "классическое",
    "классические",
    "охлажденный",
    "охлажденная",
    "замороженный",
    "замороженная",
    "бзмж",
    "гост",
}


#
# Отдельный набор для SKU-вариантов.
#
# Идея:
# если один товар называется "Сервелат",
# а второй "Сервелат Финский", слово "финский"
# нельзя просто проигнорировать.
#
# То же самое:
# "Сливушка" / "Сливушка с индейкой"
# "kana" / "kana+lõhe"
#
# При этом общие типы товара, маркетинговые слова
# и служебные слова не считаются SKU-вариантами.
#
GENERIC_VARIANT_WORDS = {
    *GENERIC_PRODUCT_NAMES,

    "продукт",
    "продукты",
    "товар",
    "товары",

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
    "классическое",
    "классические",

    "традиции",
    "традиционный",
    "традиционная",
    "традиционное",
    "традиционные",

    "охлажденный",
    "охлажденная",
    "замороженный",
    "замороженная",

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


#
# Unicode-safe tokenizer.
#
# [^\W_] означает Unicode word-char кроме "_".
# Благодаря этому не теряются õ, ä, ö, ü, é и т.д.
#
UNICODE_WORD_PATTERN = re.compile(
    r"[^\W_]+",
    flags=re.UNICODE,
)

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
    r"(кг|г|гр|мл|л|kg|g|ml|l)\b",
    flags=re.IGNORECASE,
)

NUMBER_PATTERN = re.compile(
    r"(?<![\d.,])(\d+(?:[.,]\d+)?)(?![\d.,])",
    flags=re.IGNORECASE,
)

PACKAGE_TOKEN_PATTERN = re.compile(
    r"^\d+(?:[.,]\d+)?(?:г|гр|кг|мл|л|g|kg|ml|l)$",
    flags=re.IGNORECASE,
)


class ProductMatchType(StrEnum):
    SOURCE_LINK = "source_link"
    BARCODE = "barcode"
    BRAND_AND_NAME = "brand_and_name"
    BRAND_AND_SIMILAR_NAME = "brand_and_similar_name"
    NAME = "name"
    CREATED = "created"


@dataclass(slots=True)
class ExternalProductData:
    source: str
    name: str

    #
    # Постоянная идентичность внешней карточки.
    #
    # После успешного merge Product Merge Engine
    # теперь сам гарантирует ProductSource:
    #
    # provider + source_id -> product_id
    #
    # Поэтому все прямые вызовы merge_external_product()
    # получают ту же provenance-защиту, что и
    # provider_import_service.
    #
    source_id: str | None = None
    source_url: str | None = None

    brand_name: str | None = None
    barcode: str | None = None

    category_id: int | None = None
    family_id: int | None = None

    package_value: Decimal | float | int | None = None
    package_unit: str | None = None

    subtype: str | None = None
    description: str | None = None
    image_url: str | None = None
    keywords: str | None = None

    source_priority: int = 50
    confidence: float = 100.0


@dataclass(slots=True)
class ProductMergeResult:
    product: Product
    brand: Brand

    created: bool
    match_type: ProductMatchType

    updated_fields: tuple[str, ...]

    source: str

    conflicts: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SimilarProductCandidate:
    """ Внутренняя оценка кандидата fuzzy-match. score: Итоговая уверенность 0..1. token_coverage: Какая доля меньшего набора значимых слов совпала. token_jaccard: Пересечение / объединение значимых слов. """

    product: Product
    score: float
    token_coverage: float
    token_jaccard: float


@dataclass(slots=True, frozen=True)
class SkuIdentityGuard:
    """ Результат дополнительного SKU-сита. compatible=False означает: автоматический fuzzy/name merge запрещён. Причины специально сохраняются для логов, чтобы потом было понятно, почему товар не склеился. """

    compatible: bool

    conflicts: tuple[str, ...]

    current_variants: tuple[str, ...]
    incoming_variants: tuple[str, ...]

    current_only_variants: tuple[str, ...]
    incoming_only_variants: tuple[str, ...]


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def html_clean_text(value: Any) -> str:
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


def comparable_text(value: Any) -> str:
    """ Unicode-safe текст для токенизации. Здесь намеренно НЕ используем normalize_text(), потому что для SKU-вариантов нельзя терять Unicode-буквы вроде õ / ä / ü / é. """

    return (
        html_clean_text(
            value
        )
        .casefold()
        .replace(
            "ё",
            "е",
        )
    )


def normalized(value: Any) -> str:
    cleaned = clean_text(
        value
    )

    if not cleaned:
        return ""

    return (
        normalize_text(
            cleaned
        )
        .replace(
            "ё",
            "е",
        )
    )


def decimal_text(value: str) -> str:
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


def normalize_barcode( barcode: str | None, ) -> str | None:
    if not barcode:
        return None

    digits = "".join(
        char
        for char in str(
            barcode
        )
        if char.isdigit()
    )

    if not 8 <= len(
        digits
    ) <= 14:
        return None

    return digits


def normalize_package_unit( unit: str | None, ) -> str | None:
    clean_unit = normalized(
        unit
    )

    if not clean_unit:
        return None

    aliases = {
        "гр": "г",
        "грамм": "г",
        "грамма": "г",
        "граммов": "г",
        "gram": "г",
        "grams": "г",
        "g": "г",

        "килограмм": "кг",
        "килограмма": "кг",
        "килограммов": "кг",
        "kg": "кг",

        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитров": "мл",
        "ml": "мл",

        "литр": "л",
        "литра": "л",
        "литров": "л",
        "l": "л",
    }

    return aliases.get(
        clean_unit,
        clean_text(
            unit
        ).lower(),
    )


def normalize_package_value( value: Decimal | float | int | None, ) -> Decimal | None:
    if value is None:
        return None

    try:
        decimal_value = Decimal(
            str(
                value
            )
        )
    except Exception:
        return None

    if decimal_value <= 0:
        return None

    return decimal_value


def package_to_base( value: Decimal | float | int | None, unit: str | None, ) -> tuple[
    str | None,
    Decimal | None,
]:
    normalized_value = (
        normalize_package_value(
            value
        )
    )

    normalized_unit = (
        normalize_package_unit(
            unit
        )
    )

    if (
        normalized_value is None
        or normalized_unit is None
    ):
        return None, None

    family = PACKAGE_FAMILIES.get(
        normalized_unit
    )

    if family is None:
        return None, None

    if normalized_unit in {
        "кг",
        "л",
    }:
        normalized_value *= Decimal(
            "1000"
        )

    return (
        family,
        normalized_value,
    )


def package_values_compatible( *, current_value: Decimal | float | int | None, current_unit: str | None, incoming_value: Decimal | float | int | None, incoming_unit: str | None, tolerance_percent: Decimal = Decimal("3"), ) -> bool | None:
    """ Возвращает: True: обе упаковки известны и совместимы; False: обе известны и конфликтуют; None: хотя бы у одной стороны упаковка неполная/неизвестная. Для merge главное правило: False = жёсткий запрет на объединение. """

    (
        current_family,
        current_base,
    ) = package_to_base(
        current_value,
        current_unit,
    )

    (
        incoming_family,
        incoming_base,
    ) = package_to_base(
        incoming_value,
        incoming_unit,
    )

    if (
        current_base is None
        or incoming_base is None
        or current_family is None
        or incoming_family is None
    ):
        return None

    if (
        current_family
        != incoming_family
    ):
        return False

    larger = max(
        current_base,
        incoming_base,
    )

    if larger <= 0:
        return None

    difference_percent = (
        abs(
            current_base
            - incoming_base
        )
        / larger
        * Decimal("100")
    )

    return (
        difference_percent
        <= tolerance_percent
    )


def is_unknown_brand( brand_name: str | None, ) -> bool:
    normalized_name = normalized(
        brand_name
    )

    return (
        not normalized_name
        or normalized_name
        in {
            normalized(
                item
            )
            for item
            in UNKNOWN_BRAND_NAMES
        }
    )


def is_generic_category( category_name: str | None, ) -> bool:
    return (
        normalized(
            category_name
        )
        in {
            normalized(
                item
            )
            for item
            in GENERIC_CATEGORY_NAMES
        }
    )


def category_equivalence_key( category_name: str | None, ) -> str:
    key = normalized(
        category_name
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
            for item
            in group
        }

        if key in normalized_group:
            return (
                f"equiv:{index}"
            )

    return key


def is_generic_product_name( name: str | None, ) -> bool:
    return (
        normalized(
            name
        )
        in {
            normalized(
                item
            )
            for item
            in GENERIC_PRODUCT_NAMES
        }
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


def tokenize_text( value: str | None, ) -> set[str]:
    return {
        token
        for token
        in unicode_tokens(
            value
        )
        if len(
            token
        ) >= 2
    }


def identity_name_tokens( value: str | None, ) -> set[str]:
    """ Значимые слова названия для fuzzy-match. Исключаем: - общий тип товара; - единицы измерения; - чистые числа; - слабые маркетинговые/описательные слова. Unicode-буквы сохраняются. """

    text = comparable_text(
        value
    )

    # Отдельно извлекаем Unicode-буквы и числа.
    # Так упаковка "700г" не превращается в
    # значимый identity-token "700г".
    raw_tokens = re.findall(
        r"[^\W\d_]+|\d+(?:[.,]\d+)?",
        text,
        flags=re.UNICODE,
    )

    if not raw_tokens:
        return set()

    result: set[str] = set()

    normalized_stopwords = {
        normalized(
            item
        )
        for item
        in NAME_MATCH_STOPWORDS
    }

    unit_tokens = {
        "г",
        "гр",
        "кг",
        "мл",
        "л",
        "g",
        "kg",
        "ml",
        "l",
    }

    for raw_token in raw_tokens:
        token = (
            raw_token
            .replace(
                ",",
                ".",
            )
            .strip()
        )

        if not token:
            continue

        if token in unit_tokens:
            continue

        if token.replace(
            ".",
            "",
            1,
        ).isdigit():
            continue

        if normalized(
            token
        ) in normalized_stopwords:
            continue

        if len(
            token
        ) < 3:
            continue

        result.add(
            token
        )

    return result


def tokenize_brand( brand_name: str | None, ) -> set[str]:
    return {
        token
        for token
        in unicode_tokens(
            brand_name
        )
        if len(
            token
        ) >= 3
    }


def variant_tokens( value: str | None, *, brand_name: str | None, ) -> set[str]:
    """ Значимые SKU-варианты названия. В отличие от fuzzy identity_name_tokens(), здесь специально сохраняются слова, которые могут обозначать вкус/вид/рецептуру/модель SKU. Например: финский индейкой зерновой чизкейк lõhe """

    brand_tokens = tokenize_brand(
        brand_name
    )

    result: set[str] = set()

    for token in unicode_tokens(
        value
    ):
        if len(
            token
        ) < 3:
            continue

        if token.isdigit():
            continue

        if token in brand_tokens:
            continue

        if normalized(
            token
        ) in {
            normalized(
                item
            )
            for item
            in GENERIC_VARIANT_WORDS
        }:
            continue

        if PACKAGE_TOKEN_PATTERN.fullmatch(
            token
        ):
            continue

        result.add(
            token
        )

    return result


def extract_percentages( value: str | None, ) -> tuple[str, ...]:
    text = comparable_text(
        value
    )

    return tuple(
        sorted(
            {
                decimal_text(
                    match.group(
                        1
                    )
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
            match.group(
                2
            )
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


def extract_numeric_markers( value: str | None, ) -> tuple[str, ...]:
    """ Числовые маркеры названия, которые могут отличать SKU. Перед извлечением убираем: - упаковку 300г / 1.5л; - количество 10 шт / 20 капсул. Проценты НЕ убираем: "сметана 15%" и "сметана 20" должны иметь шанс быть распознаны как конфликтующие числовые варианты. """

    text = comparable_text(
        value
    )

    text = PACKAGE_IN_NAME_PATTERN.sub(
        " ",
        text,
    )

    text = COUNT_PATTERN.sub(
        " ",
        text,
    )

    values = {
        decimal_text(
            match.group(
                1
            )
        )
        for match
        in NUMBER_PATTERN.finditer(
            text
        )
    }

    return tuple(
        sorted(
            values
        )
    )


def values_conflict( left_values: tuple[str, ...], right_values: tuple[str, ...], ) -> bool:
    left = set(
        left_values
    )

    right = set(
        right_values
    )

    return bool(
        left
        and right
        and left
        != right
    )


def sku_identity_guard( *, product: Product, incoming: ExternalProductData, brand_name: str | None, ) -> SkuIdentityGuard:
    """ Дополнительное сито перед автоматическим name/fuzzy merge. Оно не ищет совпадение само. Оно отвечает только на вопрос: "есть ли в названиях доказанный или подозрительный SKU-конфликт?" Для нового автоматического merge правило намеренно консервативное: даже ОДНОСТОРОННИЙ значимый variant-token запрещает fuzzy-склейку. Это именно то, что не позволяет автоматически склеить: "Сервелат" + "Сервелат Финский" "Сливушка" + "Сливушка с индейкой" "kana" + "kana+lõhe" """

    conflicts: list[str] = []

    current_percentages = (
        extract_percentages(
            product.name
        )
    )

    incoming_percentages = (
        extract_percentages(
            incoming.name
        )
    )

    if values_conflict(
        current_percentages,
        incoming_percentages,
    ):
        conflicts.append(
            "different_percentage:"
            f"{current_percentages}"
            "!="
            f"{incoming_percentages}"
        )

    current_counts = (
        extract_counts(
            product.name
        )
    )

    incoming_counts = (
        extract_counts(
            incoming.name
        )
    )

    if values_conflict(
        current_counts,
        incoming_counts,
    ):
        conflicts.append(
            "different_count:"
            f"{current_counts}"
            "!="
            f"{incoming_counts}"
        )

    current_name_packages = (
        extract_name_packages(
            product.name
        )
    )

    incoming_name_packages = (
        extract_name_packages(
            incoming.name
        )
    )

    if values_conflict(
        current_name_packages,
        incoming_name_packages,
    ):
        conflicts.append(
            "different_name_package:"
            f"{current_name_packages}"
            "!="
            f"{incoming_name_packages}"
        )

    current_numbers = (
        extract_numeric_markers(
            product.name
        )
    )

    incoming_numbers = (
        extract_numeric_markers(
            incoming.name
        )
    )

    if values_conflict(
        current_numbers,
        incoming_numbers,
    ):
        conflicts.append(
            "different_numeric_marker:"
            f"{current_numbers}"
            "!="
            f"{incoming_numbers}"
        )

    current_variants_set = (
        variant_tokens(
            product.name,
            brand_name=brand_name,
        )
    )

    incoming_variants_set = (
        variant_tokens(
            incoming.name,
            brand_name=brand_name,
        )
    )

    common_variants = (
        current_variants_set
        & incoming_variants_set
    )

    current_only = (
        current_variants_set
        - common_variants
    )

    incoming_only = (
        incoming_variants_set
        - common_variants
    )

    if (
        current_only
        or incoming_only
    ):
        conflicts.append(
            "sku_variant_difference:"
            f"{tuple(sorted(current_only))}"
            "|"
            f"{tuple(sorted(incoming_only))}"
        )

    return SkuIdentityGuard(
        compatible=(
            not conflicts
        ),
        conflicts=tuple(
            conflicts
        ),
        current_variants=tuple(
            sorted(
                current_variants_set
            )
        ),
        incoming_variants=tuple(
            sorted(
                incoming_variants_set
            )
        ),
        current_only_variants=tuple(
            sorted(
                current_only
            )
        ),
        incoming_only_variants=tuple(
            sorted(
                incoming_only
            )
        ),
    )


def text_similarity( left: str | None, right: str | None, ) -> float:
    left_tokens = tokenize_text(
        left
    )

    right_tokens = tokenize_text(
        right
    )

    if (
        not left_tokens
        or not right_tokens
    ):
        return 0.0

    union = len(
        left_tokens
        | right_tokens
    )

    if union <= 0:
        return 0.0

    return (
        len(
            left_tokens
            & right_tokens
        )
        / union
    )


def identity_name_similarity( left: str | None, right: str | None, ) -> tuple[
    float,
    float,
    int,
]:
    """ Возвращает: coverage: пересечение / размер меньшего набора; jaccard: пересечение / объединение; common_count: число общих значимых токенов. """

    left_tokens = identity_name_tokens(
        left
    )

    right_tokens = identity_name_tokens(
        right
    )

    if (
        not left_tokens
        or not right_tokens
    ):
        return (
            0.0,
            0.0,
            0,
        )

    intersection = (
        left_tokens
        & right_tokens
    )

    common_count = len(
        intersection
    )

    smaller_size = min(
        len(
            left_tokens
        ),
        len(
            right_tokens
        ),
    )

    union_size = len(
        left_tokens
        | right_tokens
    )

    coverage = (
        common_count
        / smaller_size
        if smaller_size > 0
        else 0.0
    )

    jaccard = (
        common_count
        / union_size
        if union_size > 0
        else 0.0
    )

    return (
        coverage,
        jaccard,
        common_count,
    )


def name_quality_score( value: str | None, ) -> float:
    text = clean_text(
        value
    )

    if not text:
        return 0.0

    score = 20.0

    if not is_generic_product_name(
        text
    ):
        score += 30.0

    score += min(
        len(
            tokenize_text(
                text
            )
        )
        * 5.0,
        25.0,
    )

    if re.search(
        r"\d",
        text,
    ):
        score += 5.0

    if len(
        text
    ) >= 12:
        score += 10.0

    if len(
        text
    ) > 180:
        score -= 20.0

    return max(
        0.0,
        min(
            score,
            100.0,
        ),
    )


def description_quality_score( value: str | None, ) -> float:
    text = clean_text(
        value
    )

    if not text:
        return 0.0

    score = min(
        len(
            text
        ) / 8.0,
        70.0,
    )

    normalized_text = normalized(
        text
    )

    marketing_hits = sum(
        1
        for marker
        in MARKETING_DESCRIPTION_MARKERS
        if marker
        in normalized_text
    )

    score -= (
        marketing_hits
        * 10.0
    )

    if len(
        text
    ) >= 80:
        score += 10.0

    if len(
        text
    ) >= 200:
        score += 10.0

    return max(
        0.0,
        min(
            score,
            100.0,
        ),
    )


def image_quality_score( value: str | None, ) -> float:
    """ Только локальная эвристика для выбора между двумя уже найденными image_url. Это НЕ заменяет Image Validator. """

    image = clean_text(
        value
    )

    if not image:
        return 0.0

    normalized_image = normalized(
        image
    )

    if any(
        marker
        in normalized_image
        for marker
        in PLACEHOLDER_IMAGE_MARKERS
    ):
        return 10.0

    if image.startswith(
        (
            "http://",
            "https://",
        )
    ):
        score = 70.0

        if re.search(
            r"\.(jpg|jpeg|png|webp)(?:$|\?)",
            image,
            flags=re.IGNORECASE,
        ):
            score += 10.0

        return min(
            score,
            100.0,
        )

    #
    # Telegram file_id / внутреннее изображение.
    #
    return 95.0


def is_better_name( *, current_name: str | None, incoming_name: str | None, ) -> bool:
    current = clean_text(
        current_name
    )

    incoming = clean_text(
        incoming_name
    )

    if not incoming:
        return False

    if not current:
        return True

    if (
        normalized(
            current
        )
        == normalized(
            incoming
        )
    ):
        return False

    if (
        is_generic_product_name(
            current
        )
        and not is_generic_product_name(
            incoming
        )
    ):
        return True

    similarity = text_similarity(
        current,
        incoming,
    )

    #
    # Хорошее существующее имя нельзя заменить
    # совершенно другим названием.
    #
    if (
        not is_generic_product_name(
            current
        )
        and similarity < 0.20
    ):
        return False

    return (
        name_quality_score(
            incoming
        )
        >= name_quality_score(
            current
        )
        + 15.0
    )


def should_replace_description( *, current_value: str | None, incoming_value: str | None, ) -> bool:
    incoming = clean_text(
        incoming_value
    )

    if not incoming:
        return False

    current = clean_text(
        current_value
    )

    if not current:
        return True

    return (
        description_quality_score(
            incoming
        )
        >= description_quality_score(
            current
        )
        + 15.0
    )


def should_replace_image( *, current_value: str | None, incoming_value: str | None, ) -> bool:
    incoming = clean_text(
        incoming_value
    )

    if not incoming:
        return False

    current = clean_text(
        current_value
    )

    if not current:
        return True

    return (
        image_quality_score(
            incoming
        )
        >= image_quality_score(
            current
        )
        + 20.0
    )


def should_fill_text( current_value: Any, incoming_value: Any, ) -> bool:
    return (
        not clean_text(
            current_value
        )
        and bool(
            clean_text(
                incoming_value
            )
        )
    )


def combine_keywords( current_keywords: str | None, incoming_keywords: str | None, ) -> str | None:
    current = clean_text(
        current_keywords
    )

    incoming = clean_text(
        incoming_keywords
    )

    if (
        not current
        and not incoming
    ):
        return None

    if not current:
        return incoming

    if not incoming:
        return current

    values: list[str] = []
    seen: set[str] = set()

    for text in (
        current,
        incoming,
    ):
        parts = [
            part.strip()
            for part
            in text.replace(
                ";",
                ",",
            ).split(
                ","
            )
            if part.strip()
        ]

        if len(
            parts
        ) == 1:
            parts = [
                text
            ]

        for part in parts:
            key = normalized(
                part
            )

            if (
                not key
                or key in seen
            ):
                continue

            seen.add(
                key
            )

            values.append(
                part
            )

    return ", ".join(
        values
    )


def build_search_text( *, product: Product, brand: Brand, category: Category | None = None, ) -> str:
    parts = (
        product.name,
        brand.name,
        (
            category.name
            if category
            is not None
            else None
        ),
        product.subtype,
        product.keywords,
        product.description,
        product.package_value,
        product.package_unit,
        product.barcode,
    )

    normalized_parts = [
        normalized(
            part
        )
        for part
        in parts
        if part is not None
    ]

    return " ".join(
        part
        for part
        in normalized_parts
        if part
    )


async def find_brand( *, session: AsyncSession, brand_name: str, ) -> Brand | None:
    normalized_name = normalized(
        brand_name
    )

    if not normalized_name:
        return None

    statement = (
        select(
            Brand
        )
        .where(
            or_(
                Brand.normalized_name
                == normalized_name,
                Brand.name.ilike(
                    brand_name
                ),
            )
        )
        .limit(
            1
        )
    )

    result = await session.execute(
        statement
    )

    return (
        result.scalar_one_or_none()
    )


async def get_or_create_brand( *, session: AsyncSession, brand_name: str | None, ) -> Brand:
    clean_brand = clean_text(
        brand_name
    )

    if (
        not clean_brand
        or is_unknown_brand(
            clean_brand
        )
    ):
        clean_brand = (
            "Бренд не указан"
        )

    existing = await find_brand(
        session=session,
        brand_name=clean_brand,
    )

    if existing is not None:
        return existing

    brand = Brand(
        name=clean_brand,
        normalized_name=normalized(
            clean_brand
        ),
    )

    session.add(
        brand
    )

    await session.flush()

    return brand


async def get_brand_by_id( *, session: AsyncSession, brand_id: int, ) -> Brand | None:
    result = await session.execute(
        select(
            Brand
        )
        .where(
            Brand.id
            == brand_id
        )
        .limit(
            1
        )
    )

    return (
        result.scalar_one_or_none()
    )


async def get_category_by_id( *, session: AsyncSession, category_id: int | None, ) -> Category | None:
    if category_id is None:
        return None

    result = await session.execute(
        select(
            Category
        )
        .where(
            Category.id
            == category_id
        )
        .limit(
            1
        )
    )

    return (
        result.scalar_one_or_none()
    )


async def find_product_by_source( *, session: AsyncSession, source: str | None, source_id: str | None, ) -> Product | None:
    """ Самое устойчивое сопоставление после того, как внешний товар уже однажды был привязан: provider + source_id -> product_id Такая связь важнее повторного fuzzy-match. """

    cleaned_source = clean_text(
        source
    )

    cleaned_source_id = clean_text(
        source_id
    )

    if (
        not cleaned_source
        or not cleaned_source_id
    ):
        return None

    result = await session.execute(
        select(
            Product
        )
        .join(
            ProductSource,
            ProductSource.product_id
            == Product.id,
        )
        .where(
            ProductSource.provider
            == cleaned_source,
            ProductSource.source_id
            == cleaned_source_id,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            1
        )
    )

    product = (
        result.scalar_one_or_none()
    )

    if product is not None:
        logger.info(
            "Product source match: "
            "provider=%s source_id=%s "
            "product_id=%s",
            cleaned_source,
            cleaned_source_id,
            product.id,
        )

    return product


async def get_product_source( *, session: AsyncSession, source: str | None, source_id: str | None, ) -> ProductSource | None:
    """ Возвращает существующую provenance-связь независимо от активности Product. """

    cleaned_source = clean_text(
        source
    )

    cleaned_source_id = clean_text(
        source_id
    )

    if (
        not cleaned_source
        or not cleaned_source_id
    ):
        return None

    result = await session.execute(
        select(
            ProductSource
        )
        .where(
            ProductSource.provider
            == cleaned_source,
            ProductSource.source_id
            == cleaned_source_id,
        )
        .limit(
            1
        )
    )

    return (
        result.scalar_one_or_none()
    )


async def ensure_product_source( *, session: AsyncSession, product: Product, incoming: ExternalProductData, ) -> ProductSource | None:
    """ Гарантирует постоянную связь: provider + source_id -> product_id Вызывается непосредственно из merge_external_product(), поэтому provenance сохраняется даже у адаптеров, которые обходят provider_import_service. ВАЖНО: существующую связь с другим product_id автоматически НЕ перепривязываем. """

    source = clean_text(
        incoming.source
    )

    source_id = clean_text(
        incoming.source_id
    )

    source_url = (
        clean_text(
            incoming.source_url
        )
        or None
    )

    if (
        not source
        or not source_id
    ):
        return None

    existing = await get_product_source(
        session=session,
        source=source,
        source_id=source_id,
    )

    if existing is not None:
        if (
            int(
                existing.product_id
            )
            != int(
                product.id
            )
        ):
            logger.warning(
                "ProductSource conflict inside "
                "Product Merge Engine: "
                "provider=%s source_id=%s "
                "existing_product_id=%s "
                "merge_product_id=%s",
                source,
                source_id,
                existing.product_id,
                product.id,
            )

            return existing

        if (
            source_url
            and existing.source_url
            != source_url
        ):
            existing.source_url = (
                source_url
            )

            await session.flush()

        return existing

    product_source = ProductSource(
        product_id=int(
            product.id
        ),
        provider=source,
        source_id=source_id,
        source_url=source_url,
    )

    session.add(
        product_source
    )

    await session.flush()

    logger.info(
        "ProductSource ensured by merge engine: "
        "provider=%s source_id=%s "
        "product_id=%s source_url=%r",
        source,
        source_id,
        product.id,
        source_url,
    )

    return product_source


async def find_product_by_barcode( *, session: AsyncSession, barcode: str | None, ) -> Product | None:
    normalized_barcode = (
        normalize_barcode(
            barcode
        )
    )

    if normalized_barcode is None:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.barcode
            == normalized_barcode,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            1
        )
    )

    return (
        result.scalar_one_or_none()
    )


def product_package_compatible_with_incoming( *, product: Product, incoming: ExternalProductData, ) -> bool:
    compatibility = (
        package_values_compatible(
            current_value=(
                product.package_value
            ),
            current_unit=(
                product.package_unit
            ),
            incoming_value=(
                incoming.package_value
            ),
            incoming_unit=(
                incoming.package_unit
            ),
        )
    )

    #
    # False — доказанный конфликт SKU.
    # None — данных недостаточно.
    #
    return (
        compatibility
        is not False
    )


def barcodes_do_not_conflict( *, product: Product, incoming: ExternalProductData, ) -> bool:
    current_barcode = (
        normalize_barcode(
            product.barcode
        )
    )

    incoming_barcode = (
        normalize_barcode(
            incoming.barcode
        )
    )

    if (
        current_barcode
        and incoming_barcode
        and current_barcode
        != incoming_barcode
    ):
        return False

    return True


async def categories_compatible( *, session: AsyncSession, product: Product, incoming: ExternalProductData, ) -> bool:
    """ Категория используется как страховка. Разные конкретные категории = запрет fuzzy-merge. Исключения: - одна категория общая/неизвестная; - категории входят в одну известную equivalence-группу. """

    if incoming.category_id is None:
        return True

    if (
        product.category_id
        == incoming.category_id
    ):
        return True

    current_category = (
        await get_category_by_id(
            session=session,
            category_id=(
                product.category_id
            ),
        )
    )

    incoming_category = (
        await get_category_by_id(
            session=session,
            category_id=(
                incoming.category_id
            ),
        )
    )

    if (
        current_category is None
        or incoming_category is None
    ):
        return True

    if (
        is_generic_category(
            current_category.name
        )
        or is_generic_category(
            incoming_category.name
        )
    ):
        return True

    current_key = (
        category_equivalence_key(
            current_category.name
        )
    )

    incoming_key = (
        category_equivalence_key(
            incoming_category.name
        )
    )

    return bool(
        current_key
        and incoming_key
        and current_key
        == incoming_key
    )


async def find_product_by_brand_and_name( *, session: AsyncSession, brand: Brand, incoming: ExternalProductData, ) -> Product | None:
    """ Точное совпадение brand + normalized_name. Даже здесь: - разные barcode запрещены; - несовместимая упаковка запрещена; - числовые/структурные конфликты имени запрещены. """

    normalized_name = normalized(
        incoming.name
    )

    if not normalized_name:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.brand_id
            == brand.id,
            Product.normalized_name
            == normalized_name,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            10
        )
    )

    products = list(
        result.scalars().all()
    )

    compatible: list[
        Product
    ] = []

    for product in products:
        if not (
            product_package_compatible_with_incoming(
                product=product,
                incoming=incoming,
            )
        ):
            continue

        if not barcodes_do_not_conflict(
            product=product,
            incoming=incoming,
        ):
            continue

        guard = sku_identity_guard(
            product=product,
            incoming=incoming,
            brand_name=brand.name,
        )

        if not guard.compatible:
            continue

        compatible.append(
            product
        )

    if len(
        compatible
    ) != 1:
        return None

    return compatible[
        0
    ]


async def find_product_by_brand_and_similar_name( *, session: AsyncSession, brand: Brand, incoming: ExternalProductData, ) -> Product | None:
    """ Безопасный fuzzy-match для multi-source enrichment. Используется ТОЛЬКО когда: - входящий бренд реальный; - ищем только товары того же brand_id; - разные штрихкоды запрещают merge; - разные известные упаковки запрещают merge; - разные конкретные категории запрещают merge; - значимые SKU-варианты не конфликтуют; - проценты/количество/числовые маркеры не конфликтуют; - названия имеют сильное совпадение по значимым словам; - лучший кандидат заметно лучше второго. При сомнении создаётся отдельная карточка. """

    if is_unknown_brand(
        brand.name
    ):
        return None

    incoming_tokens = (
        identity_name_tokens(
            incoming.name
        )
    )

    #
    # Одного значимого слова недостаточно.
    #
    if len(
        incoming_tokens
    ) < 2:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.brand_id
            == brand.id,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            100
        )
    )

    products = list(
        result.scalars().all()
    )

    candidates: list[
        SimilarProductCandidate
    ] = []

    for product in products:
        if not barcodes_do_not_conflict(
            product=product,
            incoming=incoming,
        ):
            continue

        package_compatibility = (
            package_values_compatible(
                current_value=(
                    product.package_value
                ),
                current_unit=(
                    product.package_unit
                ),
                incoming_value=(
                    incoming.package_value
                ),
                incoming_unit=(
                    incoming.package_unit
                ),
            )
        )

        if (
            package_compatibility
            is False
        ):
            continue

        if not await categories_compatible(
            session=session,
            product=product,
            incoming=incoming,
        ):
            continue

        guard = sku_identity_guard(
            product=product,
            incoming=incoming,
            brand_name=brand.name,
        )

        if not guard.compatible:
            logger.debug(
                "Fuzzy product candidate rejected "
                "by SKU guard: "
                "source=%s incoming=%r "
                "product_id=%s current=%r "
                "conflicts=%s",
                incoming.source,
                incoming.name,
                product.id,
                product.name,
                guard.conflicts,
            )

            continue

        (
            coverage,
            jaccard,
            common_count,
        ) = identity_name_similarity(
            product.name,
            incoming.name,
        )

        if common_count < 2:
            continue

        if coverage < 0.80:
            continue

        if jaccard < 0.55:
            continue

        score = (
            coverage
            * 0.65
            + jaccard
            * 0.35
        )

        if (
            package_compatibility
            is True
        ):
            score += 0.08

        score = min(
            score,
            1.0,
        )

        candidates.append(
            SimilarProductCandidate(
                product=product,
                score=score,
                token_coverage=coverage,
                token_jaccard=jaccard,
            )
        )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item.score,
        reverse=True,
    )

    best = candidates[
        0
    ]

    if best.score < 0.78:
        return None

    if len(
        candidates
    ) >= 2:
        second = candidates[
            1
        ]

        if (
            best.score
            - second.score
            < 0.12
        ):
            logger.info(
                "Fuzzy product match ambiguous: "
                "source=%s incoming=%r "
                "best_product_id=%s best_score=%.3f "
                "second_product_id=%s second_score=%.3f",
                incoming.source,
                incoming.name,
                best.product.id,
                best.score,
                second.product.id,
                second.score,
            )

            return None

    logger.info(
        "Fuzzy product match accepted: "
        "source=%s incoming=%r "
        "product_id=%s current=%r "
        "score=%.3f coverage=%.3f jaccard=%.3f "
        "incoming_package=%s%s current_package=%s%s",
        incoming.source,
        incoming.name,
        best.product.id,
        best.product.name,
        best.score,
        best.token_coverage,
        best.token_jaccard,
        incoming.package_value,
        incoming.package_unit
        or "",
        best.product.package_value,
        best.product.package_unit
        or "",
    )

    return (
        best.product
    )


async def find_safe_name_match( *, session: AsyncSession, incoming: ExternalProductData, incoming_brand: Brand, ) -> Product | None:
    """ Последний безопасный fallback: полное normalized_name при совместимом бренде и отсутствии package/barcode/SKU-конфликта. """

    normalized_name = normalized(
        incoming.name
    )

    if not normalized_name:
        return None

    result = await session.execute(
        select(
            Product
        )
        .where(
            Product.normalized_name
            == normalized_name,
            Product.is_active.is_(
                True
            ),
        )
        .limit(
            10
        )
    )

    products = list(
        result.scalars().all()
    )

    compatible_products: list[
        Product
    ] = []

    for product in products:
        if not (
            product_package_compatible_with_incoming(
                product=product,
                incoming=incoming,
            )
        ):
            continue

        if not barcodes_do_not_conflict(
            product=product,
            incoming=incoming,
        ):
            continue

        existing_brand = (
            await get_brand_by_id(
                session=session,
                brand_id=(
                    product.brand_id
                ),
            )
        )

        existing_brand_name = (
            existing_brand.name
            if existing_brand
            is not None
            else None
        )

        if (
            not is_unknown_brand(
                existing_brand_name
            )
            and not is_unknown_brand(
                incoming_brand.name
            )
            and normalized(
                existing_brand_name
            )
            != normalized(
                incoming_brand.name
            )
        ):
            continue

        guard_brand_name = (
            existing_brand_name
            or incoming_brand.name
        )

        guard = sku_identity_guard(
            product=product,
            incoming=incoming,
            brand_name=guard_brand_name,
        )

        if not guard.compatible:
            continue

        compatible_products.append(
            product
        )

    if len(
        compatible_products
    ) != 1:
        return None

    return (
        compatible_products[
            0
        ]
    )


async def find_matching_product( *, session: AsyncSession, incoming: ExternalProductData, brand: Brand, ) -> tuple[
    Product | None,
    ProductMatchType | None,
]:
    """ Приоритет сопоставления: 1. постоянная связь provider + source_id; 2. точный barcode; 3. точный brand + name; 4. безопасный brand + similar name; 5. безопасное точное имя; 6. создание нового Product. Source-link и barcode намеренно сильнее name-сита. SKU-сито применяется к автоматическим name/fuzzy сопоставлениям. """

    source_match = (
        await find_product_by_source(
            session=session,
            source=incoming.source,
            source_id=incoming.source_id,
        )
    )

    if source_match is not None:
        return (
            source_match,
            ProductMatchType.SOURCE_LINK,
        )

    barcode_match = (
        await find_product_by_barcode(
            session=session,
            barcode=incoming.barcode,
        )
    )

    if barcode_match is not None:
        return (
            barcode_match,
            ProductMatchType.BARCODE,
        )

    brand_name_match = (
        await find_product_by_brand_and_name(
            session=session,
            brand=brand,
            incoming=incoming,
        )
    )

    if brand_name_match is not None:
        return (
            brand_name_match,
            ProductMatchType.BRAND_AND_NAME,
        )

    similar_name_match = (
        await find_product_by_brand_and_similar_name(
            session=session,
            brand=brand,
            incoming=incoming,
        )
    )

    if similar_name_match is not None:
        return (
            similar_name_match,
            ProductMatchType.BRAND_AND_SIMILAR_NAME,
        )

    if not is_generic_product_name(
        incoming.name
    ):
        name_match = (
            await find_safe_name_match(
                session=session,
                incoming=incoming,
                incoming_brand=brand,
            )
        )

        if name_match is not None:
            return (
                name_match,
                ProductMatchType.NAME,
            )

    return (
        None,
        None,
    )


async def merge_product_fields( *, session: AsyncSession, product: Product, incoming_brand: Brand, incoming: ExternalProductData, ) -> tuple[
    list[str],
    list[str],
    Brand,
    Category | None,
]:
    """ Обогащает существующий канонический Product. Конфликтующие identity-поля не перезаписываются. """

    updated_fields: list[str] = []
    conflicts: list[str] = []

    current_brand = (
        await get_brand_by_id(
            session=session,
            brand_id=(
                product.brand_id
            ),
        )
    )

    current_category = (
        await get_category_by_id(
            session=session,
            category_id=(
                product.category_id
            ),
        )
    )

    incoming_name = clean_text(
        incoming.name
    )

    if is_better_name(
        current_name=product.name,
        incoming_name=incoming_name,
    ):
        product.name = (
            incoming_name
        )

        product.normalized_name = (
            normalized(
                incoming_name
            )
        )

        updated_fields.append(
            "name"
        )

    normalized_barcode = (
        normalize_barcode(
            incoming.barcode
        )
    )

    current_barcode = (
        normalize_barcode(
            product.barcode
        )
    )

    if (
        current_barcode is None
        and normalized_barcode
    ):
        product.barcode = (
            normalized_barcode
        )

        updated_fields.append(
            "barcode"
        )

    elif (
        current_barcode
        and normalized_barcode
        and current_barcode
        != normalized_barcode
    ):
        conflicts.append(
            "barcode_conflict:"
            f"{current_barcode}"
            "!="
            f"{normalized_barcode}"
        )

    incoming_package_value = (
        normalize_package_value(
            incoming.package_value
        )
    )

    incoming_package_unit = (
        normalize_package_unit(
            incoming.package_unit
        )
    )

    package_compatibility = (
        package_values_compatible(
            current_value=(
                product.package_value
            ),
            current_unit=(
                product.package_unit
            ),
            incoming_value=(
                incoming_package_value
            ),
            incoming_unit=(
                incoming_package_unit
            ),
        )
    )

    if (
        package_compatibility
        is False
    ):
        conflicts.append(
            "package_conflict:"
            f"{product.package_value}"
            f"{product.package_unit or ''}"
            "!="
            f"{incoming_package_value}"
            f"{incoming_package_unit or ''}"
        )

    else:
        if (
            product.package_value
            is None
            and incoming_package_value
            is not None
        ):
            product.package_value = (
                incoming_package_value
            )

            updated_fields.append(
                "package_value"
            )

        if (
            not product.package_unit
            and incoming_package_unit
        ):
            product.package_unit = (
                incoming_package_unit
            )

            updated_fields.append(
                "package_unit"
            )

    if should_fill_text(
        product.subtype,
        incoming.subtype,
    ):
        product.subtype = (
            clean_text(
                incoming.subtype
            )
        )

        updated_fields.append(
            "subtype"
        )

    elif (
        clean_text(
            product.subtype
        )
        and clean_text(
            incoming.subtype
        )
        and normalized(
            product.subtype
        )
        != normalized(
            incoming.subtype
        )
    ):
        conflicts.append(
            "subtype_conflict:"
            f"{clean_text(product.subtype)}"
            "!="
            f"{clean_text(incoming.subtype)}"
        )

    if should_replace_description(
        current_value=(
            product.description
        ),
        incoming_value=(
            incoming.description
        ),
    ):
        product.description = (
            clean_text(
                incoming.description
            )
        )

        updated_fields.append(
            "description"
        )

    if should_replace_image(
        current_value=(
            product.image_url
        ),
        incoming_value=(
            incoming.image_url
        ),
    ):
        product.image_url = (
            clean_text(
                incoming.image_url
            )
        )

        updated_fields.append(
            "image_url"
        )

    merged_keywords = (
        combine_keywords(
            product.keywords,
            incoming.keywords,
        )
    )

    if (
        merged_keywords
        and merged_keywords
        != product.keywords
    ):
        product.keywords = (
            merged_keywords
        )

        updated_fields.append(
            "keywords"
        )

    #
    # Family — полезное enrichment-поле,
    # но НЕ доказательство идентичности SKU.
    #
    if (
        product.family_id is None
        and incoming.family_id
        is not None
    ):
        product.family_id = (
            incoming.family_id
        )

        updated_fields.append(
            "family_id"
        )

    elif (
        product.family_id
        is not None
        and incoming.family_id
        is not None
        and int(
            product.family_id
        )
        != int(
            incoming.family_id
        )
    ):
        conflicts.append(
            "family_conflict:"
            f"{product.family_id}"
            "!="
            f"{incoming.family_id}"
        )

    current_brand_name = (
        current_brand.name
        if current_brand
        is not None
        else None
    )

    if (
        not is_unknown_brand(
            incoming_brand.name
        )
        and (
            current_brand is None
            or is_unknown_brand(
                current_brand_name
            )
        )
        and product.brand_id
        != incoming_brand.id
    ):
        product.brand_id = (
            incoming_brand.id
        )

        current_brand = (
            incoming_brand
        )

        updated_fields.append(
            "brand_id"
        )

    elif (
        current_brand is not None
        and not is_unknown_brand(
            current_brand.name
        )
        and not is_unknown_brand(
            incoming_brand.name
        )
        and normalized(
            current_brand.name
        )
        != normalized(
            incoming_brand.name
        )
    ):
        conflicts.append(
            "brand_conflict:"
            f"{current_brand.name}"
            "!="
            f"{incoming_brand.name}"
        )

    if incoming.category_id is not None:
        incoming_category = (
            await get_category_by_id(
                session=session,
                category_id=(
                    incoming.category_id
                ),
            )
        )

        if (
            incoming_category
            is not None
        ):
            current_category_name = (
                current_category.name
                if current_category
                is not None
                else None
            )

            should_replace_category = (
                current_category is None
                or is_generic_category(
                    current_category_name
                )
            )

            if (
                should_replace_category
                and product.category_id
                != incoming_category.id
            ):
                product.category_id = (
                    incoming_category.id
                )

                current_category = (
                    incoming_category
                )

                updated_fields.append(
                    "category_id"
                )

            elif (
                current_category
                is not None
                and not is_generic_category(
                    current_category.name
                )
                and not is_generic_category(
                    incoming_category.name
                )
                and category_equivalence_key(
                    current_category.name
                )
                != category_equivalence_key(
                    incoming_category.name
                )
                and product.category_id
                != incoming_category.id
            ):
                conflicts.append(
                    "category_conflict:"
                    f"{current_category.name}"
                    "!="
                    f"{incoming_category.name}"
                )

    actual_brand = (
        current_brand
        or incoming_brand
    )

    new_search_text = (
        build_search_text(
            product=product,
            brand=actual_brand,
            category=current_category,
        )
    )

    if (
        clean_text(
            product.search_text
        )
        != clean_text(
            new_search_text
        )
    ):
        product.search_text = (
            new_search_text
        )

        updated_fields.append(
            "search_text"
        )

    return (
        updated_fields,
        conflicts,
        actual_brand,
        current_category,
    )


async def create_product( *, session: AsyncSession, incoming: ExternalProductData, brand: Brand, ) -> Product:
    if incoming.category_id is None:
        raise ValueError(
            "Невозможно создать новый товар "
            "без category_id."
        )

    product_name = clean_text(
        incoming.name
    )

    if not product_name:
        raise ValueError(
            "Невозможно создать товар "
            "без названия."
        )

    product = Product(
        name=product_name,
        normalized_name=normalized(
            product_name
        ),
        brand_id=brand.id,
        category_id=(
            incoming.category_id
        ),
        family_id=(
            incoming.family_id
        ),
        barcode=normalize_barcode(
            incoming.barcode
        ),
        package_value=(
            normalize_package_value(
                incoming.package_value
            )
        ),
        package_unit=(
            normalize_package_unit(
                incoming.package_unit
            )
        ),
        subtype=(
            clean_text(
                incoming.subtype
            )
            or None
        ),
        description=(
            clean_text(
                incoming.description
            )
            or None
        ),
        image_url=(
            clean_text(
                incoming.image_url
            )
            or None
        ),
        keywords=(
            clean_text(
                incoming.keywords
            )
            or None
        ),
        is_active=True,
    )

    session.add(
        product
    )

    await session.flush()

    category = (
        await get_category_by_id(
            session=session,
            category_id=(
                product.category_id
            ),
        )
    )

    product.search_text = (
        build_search_text(
            product=product,
            brand=brand,
            category=category,
        )
    )

    await session.flush()

    return product


def created_product_updated_fields( *, product: Product, ) -> tuple[str, ...]:
    """ Возвращает только реально заполненные поля. """

    fields: list[str] = [
        "name",
        "brand_id",
        "category_id",
    ]

    optional_fields = (
        "family_id",
        "barcode",
        "package_value",
        "package_unit",
        "subtype",
        "description",
        "image_url",
        "keywords",
        "search_text",
    )

    for field in optional_fields:
        value = getattr(
            product,
            field,
            None,
        )

        if value is None:
            continue

        if (
            isinstance(
                value,
                str,
            )
            and not value.strip()
        ):
            continue

        fields.append(
            field
        )

    return tuple(
        fields
    )


async def merge_external_product( *, session: AsyncSession, incoming: ExternalProductData, commit: bool = False, ) -> ProductMergeResult:
    """ Главная точка Product Merge Engine. Цель: несколько внешних источников должны улучшать ОДНУ каноническую карточку, если есть достаточно доказательств, что это один SKU. Приоритет: ProductSource -> barcode -> exact brand/name -> safe fuzzy -> safe exact name -> create Для name/fuzzy merge жёсткими блокерами являются: - разные известные barcode; - несовместимые известные упаковки; - разные конкретные категории; - разные проценты/количество/числовые маркеры; - значимые SKU-варианты. После успешного merge/create сервис сам гарантирует ProductSource, если source_id указан. Поэтому старые карточки могут постепенно обогащаться при новых запросах независимо от конкретного адаптера. При сомнении система НЕ склеивает товары. """

    source = clean_text(
        incoming.source
    )

    if not source:
        raise ValueError(
            "Не указан источник товара."
        )

    if not clean_text(
        incoming.name
    ):
        raise ValueError(
            "Не указано название товара."
        )

    incoming.source = (
        source
    )

    incoming.source_id = (
        clean_text(
            incoming.source_id
        )
        or None
    )

    incoming.source_url = (
        clean_text(
            incoming.source_url
        )
        or None
    )

    incoming.source_priority = max(
        0,
        min(
            int(
                incoming.source_priority
            ),
            100,
        ),
    )

    incoming.confidence = max(
        0.0,
        min(
            float(
                incoming.confidence
            ),
            100.0,
        ),
    )

    incoming_brand = (
        await get_or_create_brand(
            session=session,
            brand_name=(
                incoming.brand_name
            ),
        )
    )

    (
        product,
        match_type,
    ) = await find_matching_product(
        session=session,
        incoming=incoming,
        brand=incoming_brand,
    )

    created = False

    result_brand = (
        incoming_brand
    )

    conflicts: tuple[
        str,
        ...
    ] = ()

    if product is None:
        product = (
            await create_product(
                session=session,
                incoming=incoming,
                brand=incoming_brand,
            )
        )

        created = True

        match_type = (
            ProductMatchType.CREATED
        )

        updated_fields = (
            created_product_updated_fields(
                product=product
            )
        )

    else:
        #
        # ProductSource — сильный постоянный ключ,
        # но если тот же внешний source_id внезапно
        # пришёл с ДРУГИМ известным barcode, это
        # похоже на переиспользование карточки
        # провайдером или ошибку данных.
        #
        # В таком случае не обогащаем Product вообще:
        # сохраняем старую привязку и только логируем
        # конфликт. Иначе чужое имя/фото/описание
        # могли бы испортить каноническую карточку.
        #
        if (
            match_type
            == ProductMatchType.SOURCE_LINK
            and not barcodes_do_not_conflict(
                product=product,
                incoming=incoming,
            )
        ):
            current_barcode = normalize_barcode(
                product.barcode
            )

            incoming_barcode = normalize_barcode(
                incoming.barcode
            )

            updated_fields = ()

            conflicts = (
                "source_link_barcode_conflict:"
                f"{current_barcode}"
                "!="
                f"{incoming_barcode}",
            )

            current_brand = await get_brand_by_id(
                session=session,
                brand_id=product.brand_id,
            )

            result_brand = (
                current_brand
                or incoming_brand
            )

        else:
            (
                updated_list,
                conflict_list,
                result_brand,
                _category,
            ) = await merge_product_fields(
                session=session,
                product=product,
                incoming_brand=incoming_brand,
                incoming=incoming,
            )

            updated_fields = tuple(
                updated_list
            )

            conflicts = tuple(
                conflict_list
            )

    await session.flush()

    #
    # Ключевое изменение:
    # provenance сохраняется в самой центральной
    # точке Product Merge Engine.
    #
    # provider_import_service может повторно вызвать
    # свой save_product_source(); это безопасно и
    # идемпотентно.
    #
    saved_source = (
        await ensure_product_source(
            session=session,
            product=product,
            incoming=incoming,
        )
    )

    if (
        saved_source is not None
        and int(
            saved_source.product_id
        )
        != int(
            product.id
        )
    ):
        conflicts = tuple(
            [
                *conflicts,
                (
                    "product_source_conflict:"
                    f"{saved_source.product_id}"
                    "!="
                    f"{product.id}"
                ),
            ]
        )

    await session.flush()

    if conflicts:
        logger.warning(
            "Product merge conflicts: "
            "product_id=%s source=%s "
            "match=%s conflicts=%s",
            product.id,
            source,
            match_type,
            conflicts,
        )

    logger.info(
        "Product merge complete: "
        "product_id=%s source=%s "
        "source_id=%s "
        "created=%s match=%s "
        "updated=%s conflicts=%s",
        product.id,
        source,
        incoming.source_id,
        created,
        match_type,
        updated_fields,
        conflicts,
    )

    if commit:
        await session.commit()

    return ProductMergeResult(
        product=product,
        brand=result_brand,
        created=created,
        match_type=(
            match_type
            or ProductMatchType.CREATED
        ),
        updated_fields=tuple(
            updated_fields
        ),
        source=source,
        conflicts=conflicts,
    )
