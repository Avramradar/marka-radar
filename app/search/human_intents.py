import re
from dataclasses import dataclass
from typing import Any

from app.utils.text import normalize_text


MAX_INTENT_WORDS = 4
MAX_INTENT_LENGTH = 48


TECHNICAL_WORDS = {
    "coffee",
    "coffees",
    "instant",
    "freeze",
    "dried",
    "food",
    "foods",
    "product",
    "products",
    "milk",
    "milks",
    "drink",
    "drinks",
    "beverage",
    "beverages",
    "processed",
    "processing",
    "natural",
    "натуральный",
    "натуральная",
    "натуральное",
    "продукт",
    "продукты",
    "товар",
    "товары",
    "обработанный",
    "обработанное",
    "обработанная",
    "пищевой",
    "пищевая",
    "пищевое",
}


TECHNICAL_PHRASES = {
    "массовая доля жира",
    "массовой долей жира",
    "массовой доли жира",
    "ультравысокотемпературно обработанное",
    "ультравысокотемпературной обработки",
    "пищевая продукция",
    "продукт пищевой",
}


WORD_REPLACEMENTS = {
    "ультравысокотемпературно": "ультрапастеризованное",
    "ультравысокотемпературный": "ультрапастеризованное",
    "ультравысокотемпературное": "ультрапастеризованное",
    "ультрапастеризованный": "ультрапастеризованное",
    "пастеризованный": "пастеризованное",
    "сгущенное": "сгущённое",
    "топленое": "топлёное",
    "безлактозный": "безлактозное",
    "растворимый": "растворимый",
}


CANONICAL_INTENTS = {
    # Молоко
    "молоко питьевое": "Питьевое",
    "молоко ультрапастеризованное": (
        "Ультрапастеризованное"
    ),
    "молоко пастеризованное": "Пастеризованное",
    "молоко топленое": "Топлёное",
    "молоко топлёное": "Топлёное",
    "молоко сгущенное": "Сгущённое",
    "молоко сгущённое": "Сгущённое",
    "молоко сухое": "Сухое",
    "молоко безлактозное": "Безлактозное",
    "молоко козье": "Козье",
    "молоко кокосовое": "Кокосовое",
    "молоко овсяное": "Овсяное",
    "молоко соевое": "Соевое",

    # Кофе
    "кофе растворимый": "Растворимый",
    "кофе сублимированный": "Сублимированный",
    "кофе молотый": "Молотый",
    "кофе зерновой": "В зёрнах",
    "кофе в зернах": "В зёрнах",
    "кофе в зёрнах": "В зёрнах",
    "кофе капсулы": "В капсулах",
    "кофе в капсулах": "В капсулах",
    "кофе дрип": "Дрип-пакеты",
    "кофе три в одном": "3 в 1",
    "кофе 3 в 1": "3 в 1",

    # Сельдь
    "сельдь в масле": "В масле",
    "сельдь слабосоленая": "Слабосолёная",
    "сельдь слабосолёная": "Слабосолёная",
    "сельдь пряного посола": "Пряного посола",
    "сельдь филе": "Филе",
    "филе сельди": "Филе",
    "сельдь пресервы": "Пресервы",
}


@dataclass(slots=True, frozen=True)
class HumanIntent:
    """
    Понятное пользователю уточнение поиска.

    title:
        Текст кнопки.

    query:
        Реальный запрос, который будет выполнен
        после нажатия.

    count:
        Количество подходящих товаров.

    source_title:
        Исходное техническое название.
    """

    title: str
    query: str
    count: int
    source_title: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "query": self.query,
            "count": self.count,
            "source_title": self.source_title,
        }


def normalize_for_comparison(
    value: str,
) -> str:
    """
    Нормализует строку для сравнения вариантов.
    """

    normalized = normalize_text(
        value
    )

    normalized = normalized.replace(
        "ё",
        "е",
    )

    return " ".join(
        normalized.split()
    )


def remove_technical_phrases(
    value: str,
) -> str:
    """
    Удаляет длинные служебные фразы.
    """

    cleaned = value

    for phrase in TECHNICAL_PHRASES:
        normalized_phrase = normalize_for_comparison(
            phrase
        )

        cleaned = re.sub(
            rf"\b{re.escape(normalized_phrase)}\b",
            " ",
            cleaned,
        )

    return " ".join(
        cleaned.split()
    )


def replace_words(
    value: str,
) -> str:
    """
    Приводит близкие формулировки
    к единому человеческому варианту.
    """

    result: list[str] = []

    for word in value.split():
        result.append(
            WORD_REPLACEMENTS.get(
                word,
                word,
            )
        )

    return " ".join(
        result
    )


def remove_technical_words(
    value: str,
) -> str:
    """
    Удаляет английские и служебные слова,
    которые не должны попадать в интерфейс.
    """

    words = [
        word
        for word in value.split()
        if (
            word not in TECHNICAL_WORDS
            and not re.fullmatch(
                r"[a-z]+",
                word,
            )
        )
    ]

    return " ".join(
        words
    )


def remove_repeated_words(
    value: str,
) -> str:
    """
    Удаляет повторяющиеся слова,
    сохраняя исходный порядок.
    """

    result: list[str] = []
    seen: set[str] = set()

    for word in value.split():
        normalized_word = (
            word.lower().replace(
                "ё",
                "е",
            )
        )

        if normalized_word in seen:
            continue

        seen.add(
            normalized_word
        )

        result.append(
            word
        )

    return " ".join(
        result
    )


def remove_query_prefix(
    *,
    title: str,
    original_query: str,
) -> str:
    """
    Убирает исходный широкий запрос
    из начала уточнения.

    Пример:

        запрос: "молоко"
        вариант: "молоко питьевое"

    результат:

        "питьевое"
    """

    normalized_title = normalize_for_comparison(
        title
    )

    normalized_query = normalize_for_comparison(
        original_query
    )

    if not normalized_query:
        return normalized_title

    if normalized_title == normalized_query:
        return ""

    prefix = f"{normalized_query} "

    if normalized_title.startswith(
        prefix
    ):
        return normalized_title[
            len(prefix):
        ].strip()

    return normalized_title


def canonicalize_intent_title(
    *,
    title: str,
    original_query: str,
) -> str:
    """
    Превращает техническое название
    в короткое пользовательское уточнение.
    """

    normalized_title = normalize_for_comparison(
        title
    )

    normalized_title = remove_technical_phrases(
        normalized_title
    )

    normalized_title = replace_words(
        normalized_title
    )

    normalized_title = remove_technical_words(
        normalized_title
    )

    normalized_title = remove_repeated_words(
        normalized_title
    )

    full_key = normalize_for_comparison(
        normalized_title
    )

    if full_key in CANONICAL_INTENTS:
        return CANONICAL_INTENTS[
            full_key
        ]

    without_query = remove_query_prefix(
        title=normalized_title,
        original_query=original_query,
    )

    without_query = remove_repeated_words(
        without_query
    )

    if not without_query:
        return ""

    words = without_query.split()

    short_title = " ".join(
        words[:MAX_INTENT_WORDS]
    )

    short_title = short_title[
        :MAX_INTENT_LENGTH
    ].strip()

    if not short_title:
        return ""

    return (
        short_title[0].upper()
        + short_title[1:]
    )


def build_intent_deduplication_key(
    title: str,
) -> str:
    """
    Создаёт ключ для объединения дублей.

    Некоторые близкие формулировки считаются
    одним пользовательским намерением.
    """

    normalized = normalize_for_comparison(
        title
    )

    synonym_groups = {
        "ультрапастеризованное": {
            "ультрапастеризованное",
            "ультравысокотемпературное",
        },
        "растворимый": {
            "растворимый",
            "сублимированный растворимый",
        },
        "в зернах": {
            "в зернах",
            "зерновой",
        },
        "сгущенное": {
            "сгущенное",
            "сгущеное",
        },
    }

    for canonical, variants in (
        synonym_groups.items()
    ):
        if normalized in variants:
            return canonical

    return normalized


def is_valid_human_intent(
    title: str,
) -> bool:
    """
    Проверяет, можно ли показывать
    уточнение пользователю.
    """

    if not title:
        return False

    normalized = normalize_for_comparison(
        title
    )

    if not normalized:
        return False

    if len(title) > MAX_INTENT_LENGTH:
        return False

    if len(title.split()) > MAX_INTENT_WORDS:
        return False

    if any(
        word in TECHNICAL_WORDS
        for word in normalized.split()
    ):
        return False

    if re.search(
        r"\b[a-z]{2,}\b",
        normalized,
    ):
        return False

    return True


def prepare_human_intents(
    *,
    original_query: str,
    groups: list[dict[str, Any]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    Превращает технические группы поиска
    в короткие человеческие уточнения.

    Выполняет:

    - удаление английского мусора;
    - сокращение длинных формулировок;
    - удаление повторов;
    - объединение близких вариантов;
    - ограничение количества кнопок.
    """

    safe_limit = max(
        1,
        min(
            limit,
            8,
        ),
    )

    prepared_by_key: dict[
        str,
        HumanIntent,
    ] = {}

    for group in groups:
        source_title = str(
            group.get(
                "title",
                "",
            )
        ).strip()

        source_query = str(
            group.get(
                "query",
                "",
            )
        ).strip()

        count = int(
            group.get(
                "count",
                0,
            )
        )

        if (
            not source_title
            or not source_query
            or count <= 0
        ):
            continue

        human_title = canonicalize_intent_title(
            title=source_title,
            original_query=original_query,
        )

        if not is_valid_human_intent(
            human_title
        ):
            continue

        deduplication_key = (
            build_intent_deduplication_key(
                human_title
            )
        )

        existing = prepared_by_key.get(
            deduplication_key
        )

        intent = HumanIntent(
            title=human_title,
            query=source_query,
            count=count,
            source_title=source_title,
        )

        if existing is None:
            prepared_by_key[
                deduplication_key
            ] = intent
            continue

        # При дублях сохраняем вариант,
        # у которого больше товаров.
        if intent.count > existing.count:
            prepared_by_key[
                deduplication_key
            ] = intent

    prepared = sorted(
        prepared_by_key.values(),
        key=lambda intent: (
            intent.count,
            -len(intent.title),
            intent.title.lower(),
        ),
        reverse=True,
    )

    return [
        intent.as_dict()
        for intent in prepared[
            :safe_limit
        ]
    ]
