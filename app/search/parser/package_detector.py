from dataclasses import dataclass

from app.search.parser.tokenizer import QueryToken


UNIT_ALIASES = {
    "г": "г",
    "гр": "г",
    "g": "г",
    "кг": "кг",
    "kg": "кг",
    "мл": "мл",
    "ml": "мл",
    "л": "л",
    "l": "л",
    "шт": "шт",
}


@dataclass(slots=True, frozen=True)
class DetectedPackage:
    """
    Результат распознавания упаковки.

    Пример:

        930 мл

    превращается в:

        value=930.0
        unit="мл"
        token_indexes=(4, 5)
    """

    value: float
    unit: str
    token_indexes: tuple[int, ...]


def normalize_number(
    value: str,
) -> float | None:
    """
    Преобразует числовой токен в float.

    Поддерживает:

    930
    3.2
    3,2
    """

    normalized = value.replace(
        ",",
        ".",
    )

    try:
        return float(normalized)
    except ValueError:
        return None


def detect_package(
    tokens: list[QueryToken],
) -> DetectedPackage | None:
    """
    Ищет размер упаковки в токенах запроса.

    Поддерживает:

    500 г
    930 мл
    1.5 л
    2 кг

    Проценты вроде 3.2% не считаются упаковкой.
    """

    for index, token in enumerate(tokens):
        if not token.is_number:
            continue

        if token.is_percent:
            continue

        next_index = index + 1

        if next_index >= len(tokens):
            continue

        next_token = tokens[next_index]

        if not next_token.is_unit:
            continue

        unit = UNIT_ALIASES.get(
            next_token.value
        )

        if unit is None:
            continue

        value = normalize_number(
            token.value
        )

        if value is None or value <= 0:
            continue

        return DetectedPackage(
            value=value,
            unit=unit,
            token_indexes=(
                index,
                next_index,
            ),
        )

    return None


def remove_package_tokens(
    tokens: list[QueryToken],
    detected_package: DetectedPackage | None,
) -> list[QueryToken]:
    """
    Удаляет токены упаковки из запроса.

    Пример:

        сельдь 250 г в масле

    становится:

        сельдь в масле
    """

    if detected_package is None:
        return list(tokens)

    excluded_indexes = set(
        detected_package.token_indexes
    )

    return [
        token
        for index, token in enumerate(tokens)
        if index not in excluded_indexes
    ]
