import re


CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


LATIN_TO_CYRILLIC_GROUPS = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("yo", "ё"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ye", "е"),
)


LATIN_TO_CYRILLIC = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "дж",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "й",
    "z": "з",
}


def normalize_text(value: str) -> str:
    """
    Приводит строку к единому виду для поиска.

    Пример:
    "  Coca-Cola Ёж  " -> "coca cola еж"
    """

    normalized = value.lower().replace("ё", "е")

    normalized = re.sub(
        r"[^a-zа-я0-9]+",
        " ",
        normalized,
    )

    return " ".join(normalized.split())


def contains_cyrillic(value: str) -> bool:
    """Проверяет, есть ли в строке кириллица."""

    return bool(
        re.search(
            r"[а-яё]",
            value.lower(),
        )
    )


def contains_latin(value: str) -> bool:
    """Проверяет, есть ли в строке латиница."""

    return bool(
        re.search(
            r"[a-z]",
            value.lower(),
        )
    )


def transliterate_cyrillic_to_latin(
    value: str,
) -> str:
    """
    Транслитерирует кириллицу в латиницу.

    Примеры:
    "барилла" -> "barilla"
    "макфа" -> "makfa"
    """

    result: list[str] = []

    for character in value.lower():
        result.append(
            CYRILLIC_TO_LATIN.get(
                character,
                character,
            )
        )

    return normalize_text(
        "".join(result)
    )


def transliterate_latin_to_cyrillic(
    value: str,
) -> str:
    """
    Приблизительно преобразует латиницу в кириллицу.

    Примеры:
    "barilla" -> "барилла"
    "makfa" -> "макфа"
    """

    converted = value.lower()

    for latin_group, cyrillic_letter in (
        LATIN_TO_CYRILLIC_GROUPS
    ):
        converted = converted.replace(
            latin_group,
            cyrillic_letter,
        )

    result: list[str] = []

    for character in converted:
        result.append(
            LATIN_TO_CYRILLIC.get(
                character,
                character,
            )
        )

    return normalize_text(
        "".join(result)
    )


def build_search_variants(
    value: str,
) -> list[str]:
    """
    Создаёт варианты пользовательского запроса.

    Для "Барилла":
    - барилла
    - barilla

    Для "Barilla":
    - barilla
    - барилла

    Опечатки в каждом варианте затем обрабатывает pg_trgm.
    """

    normalized = normalize_text(value)

    if not normalized:
        return []

    variants: list[str] = [
        normalized,
    ]

    if contains_cyrillic(normalized):
        variants.append(
            transliterate_cyrillic_to_latin(
                normalized
            )
        )

    if contains_latin(normalized):
        variants.append(
            transliterate_latin_to_cyrillic(
                normalized
            )
        )

    # Удаляем пустые и повторяющиеся варианты,
    # сохраняя исходный порядок.
    unique_variants: list[str] = []
    seen: set[str] = set()

    for variant in variants:
        normalized_variant = normalize_text(
            variant
        )

        if not normalized_variant:
            continue

        if normalized_variant in seen:
            continue

        seen.add(normalized_variant)
        unique_variants.append(
            normalized_variant
        )

    return unique_variants
