import re

from app.services.canonical_family_rules import (
    find_canonical_family_name,
)
from app.utils.text import normalize_text


MARKETING_WORDS = {
    "новинка",
    "премиум",
    "отборный",
    "отборная",
    "отборное",
    "классический",
    "классическая",
    "классическое",
    "традиционный",
    "традиционная",
    "традиционное",
    "оригинальный",
    "оригинальная",
    "оригинальное",
    "натуральный",
    "натуральная",
    "натуральное",
    "домашний",
    "домашняя",
    "домашнее",
    "вкусный",
    "вкусная",
    "вкусное",
    "лучший",
    "лучшая",
    "лучшее",
}


PACKAGE_WORDS = {
    "г",
    "гр",
    "кг",
    "мл",
    "л",
    "шт",
    "штук",
    "уп",
    "упаковка",
    "упаковке",
    "банка",
    "банке",
    "бутылка",
    "бутылке",
    "пачка",
    "пачке",
}


TECHNICAL_WORDS = {
    "массовая",
    "массовой",
    "доля",
    "долей",
    "доли",
    "жира",
    "жир",
    "пищевой",
    "пищевая",
    "пищевое",
    "продукция",
    "продукт",
    "товар",
    "coffees",
    "coffee",
    "dried",
    "instant",
}


def remove_brand_from_name(
    *,
    product_name: str,
    brand_name: str,
) -> str:
    """
    Удаляет бренд из названия товара.

    Поддерживает бренды из нескольких слов.

    Пример:
    "Молоко Домик в деревне 3.5%"
    ->
    "молоко 3 5"
    """

    normalized_product = normalize_text(
        product_name
    )

    normalized_brand = normalize_text(
        brand_name
    )

    if not normalized_brand:
        return normalized_product

    if normalized_brand in {
        "бренд не указан",
        "не указан",
        "unknown",
        "no brand",
        "без бренда",
    }:
        return normalized_product

    cleaned = normalized_product.replace(
        normalized_brand,
        " ",
    )

    return " ".join(
        cleaned.split()
    )


def remove_package_information(
    value: str,
) -> str:
    """
    Удаляет вес, объём, количество, проценты
    и номера разновидностей.

    Примеры:
    "молоко 3.5% 930 мл"
    ->
    "молоко"

    "spaghetti №5 500 г"
    ->
    "spaghetti"
    """

    cleaned = value

    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*%\b",
        " ",
        cleaned,
    )

    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*"
        r"(?:г|гр|кг|мл|л|шт|штук|g|kg|ml|l)\b",
        " ",
        cleaned,
    )

    cleaned = re.sub(
        r"(?:№|#)\s*\d+\b",
        " ",
        cleaned,
    )

    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\b",
        " ",
        cleaned,
    )

    return " ".join(
        cleaned.split()
    )


def remove_marketing_words(
    value: str,
) -> str:
    """
    Удаляет слова, которые не определяют
    вид товара.
    """

    filtered_words = [
        word
        for word in value.split()
        if (
            word not in MARKETING_WORDS
            and word not in PACKAGE_WORDS
            and word not in TECHNICAL_WORDS
        )
    ]

    return " ".join(
        filtered_words
    )


def remove_repeated_words(
    value: str,
) -> str:
    """
    Удаляет повторяющиеся слова,
    сохраняя исходный порядок.

    Пример:
    "кофе кофе растворимый"
    ->
    "кофе растворимый"
    """

    result: list[str] = []
    seen: set[str] = set()

    for word in value.split():
        if word in seen:
            continue

        seen.add(word)
        result.append(word)

    return " ".join(
        result
    )


def limit_family_words(
    value: str,
    *,
    max_words: int = 5,
) -> str:
    """
    Ограничивает резервное название семейства,
    чтобы оно не превращалось в полное название товара.
    """

    return " ".join(
        value.split()[:max_words]
    )


def build_fallback_family_name(
    *,
    product_name: str,
    brand_name: str,
    subtype: str | None = None,
) -> str:
    """
    Строит резервное семейство для товаров,
    для которых пока нет канонического правила.
    """

    base_name = remove_brand_from_name(
        product_name=product_name,
        brand_name=brand_name,
    )

    base_name = remove_package_information(
        base_name
    )

    base_name = remove_marketing_words(
        base_name
    )

    if subtype:
        normalized_subtype = normalize_text(
            subtype
        )

        if (
            normalized_subtype
            and normalized_subtype not in base_name
        ):
            base_name = (
                f"{base_name} "
                f"{normalized_subtype}"
            )

    base_name = normalize_text(
        base_name
    )

    base_name = remove_repeated_words(
        base_name
    )

    return limit_family_words(
        base_name,
        max_words=5,
    )


def build_product_family_name(
    *,
    product_name: str,
    brand_name: str,
    category_name: str | None = None,
    subtype: str | None = None,
    keywords: str | None = None,
) -> str:
    """
    Формирует название семейства товара.

    Сначала используются канонические правила:

    "Домик в деревне Молоко 3.5% 930 мл"
    ->
    "Молоко"

    "Кофе растворимый сублимированный"
    ->
    "Кофе растворимый"

    "Сельдь филе в масле"
    ->
    "Сельдь в масле"

    Для остальных категорий используется
    резервная нормализация названия.
    """

    canonical_name = find_canonical_family_name(
        product_name=product_name,
        brand_name=brand_name,
        category_name=category_name,
        subtype=subtype,
        keywords=keywords,
    )

    if canonical_name:
        return normalize_text(
            canonical_name
        )

    fallback_name = build_fallback_family_name(
        product_name=product_name,
        brand_name=brand_name,
        subtype=subtype,
    )

    return normalize_text(
        fallback_name
    )
