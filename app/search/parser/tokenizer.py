import re
from dataclasses import dataclass

from app.utils.text import normalize_text


TOKEN_PATTERN = re.compile(
    r"""
    \d+(?:[.,]\d+)?%?
    |
    [a-zа-яё]+
    """,
    re.IGNORECASE | re.VERBOSE,
)


PACKAGE_UNITS = {
    "г",
    "гр",
    "кг",
    "мл",
    "л",
    "шт",
    "g",
    "kg",
    "ml",
    "l",
}


STOP_WORDS = {
    "и",
    "в",
    "во",
    "на",
    "с",
    "со",
    "из",
    "для",
    "по",
    "под",
    "без",
    "от",
    "до",
    "к",
    "у",
    "а",
    "или",
}


@dataclass(slots=True, frozen=True)
class QueryToken:
    """
    Один токен поискового запроса.

    Пример:

        QueryToken(
            value="930",
            position=4,
            is_number=True,
            is_percent=False,
            is_unit=False,
        )
    """

    value: str
    position: int
    is_number: bool
    is_percent: bool
    is_unit: bool
    is_stop_word: bool


def is_number_token(
    value: str,
) -> bool:
    """
    Проверяет, является ли токен числом.

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
        float(normalized.rstrip("%"))
    except ValueError:
        return False

    return True


def tokenize_query(
    value: str,
    *,
    keep_stop_words: bool = False,
) -> list[QueryToken]:
    """
    Разбивает запрос на структурированные токены.

    Пример:

        Домик в деревне молоко 3.2% 930 мл

    Результат:

        домик
        деревне
        молоко
        3.2%
        930
        мл
    """

    normalized = normalize_text(value)

    if not normalized:
        return []

    raw_tokens = TOKEN_PATTERN.findall(
        normalized
    )

    tokens: list[QueryToken] = []

    for position, raw_token in enumerate(
        raw_tokens
    ):
        token = raw_token.lower().replace(
            "ё",
            "е",
        )

        is_percent = token.endswith("%")

        token_without_percent = token.rstrip(
            "%"
        )

        is_number = is_number_token(
            token
        )

        is_unit = (
            token_without_percent
            in PACKAGE_UNITS
        )

        is_stop_word = (
            token_without_percent
            in STOP_WORDS
        )

        if (
            is_stop_word
            and not keep_stop_words
        ):
            continue

        tokens.append(
            QueryToken(
                value=token_without_percent,
                position=position,
                is_number=is_number,
                is_percent=is_percent,
                is_unit=is_unit,
                is_stop_word=is_stop_word,
            )
        )

    return tokens


def token_values(
    tokens: list[QueryToken],
) -> list[str]:
    """
    Возвращает только текстовые значения токенов.
    """

    return [
        token.value
        for token in tokens
    ]


def join_tokens(
    tokens: list[QueryToken],
) -> str:
    """
    Собирает токены обратно в строку.
    """

    return " ".join(
        token_values(tokens)
    )
