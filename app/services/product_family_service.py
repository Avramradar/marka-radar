import re

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


def remove_brand_from_name(
    *,
    product_name: str,
    brand_name: str,
) -> str:
    """
    Удаляет бренд из начала или середины названия товара.

    Пример:
    "VICI Сельдь филе в масле"
    ->
    "Сельдь филе в масле"
    """

    normalized_product = normalize_text(
        product_name
    )

    normalized_brand = normalize_text(
        brand_name
    )

    if not normalized_brand:
        return normalized_product

    brand_pattern = re.escape(
        normalized_brand
    )

    cleaned = re.sub(
        rf"\b{brand_pattern}\b",
        " ",
        normalized_product,
        flags=re.IGNORECASE,
    )

    return " ".join(
        cleaned.split()
    )


def remove_package_information(
    value: str,
) -> str:
    """
    Удаляет вес, объём, количество и проценты.

    Примеры:
    "молоко 3.2 930 мл"
    ->
    "молоко"

    "спагетти 450 г"
    ->
    "спагетти"
    """

    cleaned = value

    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*%\b",
        " ",
        cleaned,
    )

    cleaned = re.sub(
        r"\b\d+(?:[.,]\d+)?\s*"
        r"(?:г|гр|кг|мл|л|шт|штук)\b",
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
    реальный вид товара.
    """

    words = value.split()

    filtered_words = [
        word
        for word in words
        if (
            word not in MARKETING_WORDS
            and word not in PACKAGE_WORDS
        )
    ]

    return " ".join(
        filtered_words
    )


def limit_family_words(
    value: str,
    *,
    max_words: int = 6,
) -> str:
    """
    Ограничивает название семейства,
    чтобы оно не превращалось в полное
    название товара.
    """

    words = value.split()

    return " ".join(
        words[:max_words]
    )


def build_product_family_name(
    *,
    product_name: str,
    brand_name: str,
    subtype: str | None = None,
) -> str:
    """
    Формирует нормализованное название семейства.

    Примеры:

    VICI Сельдь филе в масле 240 г
    ->
    сельдь филе в масле

    Barilla Spaghetti №5 500 г
    ->
    spaghetti

    Простоквашино Молоко 3.2% 930 мл
    ->
    молоко
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
            and normalized_subtype
            not in base_name
        ):
            base_name = (
                f"{base_name} "
                f"{normalized_subtype}"
            ).strip()

    base_name = limit_family_words(
        base_name,
        max_words=6,
    )

    base_name = normalize_text(
        base_name
    )

    return base_name
