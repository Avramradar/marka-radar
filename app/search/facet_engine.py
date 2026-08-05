import re
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.product_repository import (
    search_products,
)
from app.utils.text import normalize_text


class FacetType(StrEnum):
    """
    Поддерживаемые типы уточнений.
    """

    PRODUCT_KIND = "product_kind"
    PROCESSING = "processing"
    SPECIAL = "special"
    FORM = "form"
    PREPARATION = "preparation"
    FAT_PERCENT = "fat_percent"


@dataclass(slots=True, frozen=True)
class FacetDefinition:
    """
    Описание контролируемого фасета.

    key:
        Внутренний уникальный идентификатор.

    title:
        Понятный текст кнопки.

    query_term:
        Значение, добавляемое к поисковому запросу.

    match_terms:
        Слова и фразы, по которым определяется
        принадлежность товара к фасету.
    """

    key: str
    title: str
    query_term: str
    match_terms: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class FacetCandidate:
    """
    Подготовленный товар для анализа фасетов.

    normalized_text:
        Нормализованный текст для поиска слов
        и смысловых характеристик.

    raw_text:
        Исходный текст с сохранёнными знаками
        процентов, запятыми и числами.

        Он необходим для извлечения жирности.
    """

    normalized_text: str
    raw_text: str


@dataclass(slots=True, frozen=True)
class FacetOption:
    """
    Один вариант уточнения.
    """

    facet_type: FacetType
    key: str
    title: str
    query: str
    count: int
    usefulness: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "facet_type": self.facet_type.value,
            "key": self.key,
            "title": self.title,
            "query": self.query,
            "count": self.count,
            "usefulness": self.usefulness,
        }


@dataclass(slots=True, frozen=True)
class FacetGroup:
    """
    Группа связанных уточнений.

    Например:

    Способ обработки:
    - пастеризованное;
    - ультрапастеризованное.
    """

    facet_type: FacetType
    title: str
    options: tuple[FacetOption, ...]
    priority: int = 100

    @property
    def total_count(self) -> int:
        return sum(
            option.count
            for option in self.options
        )

    @property
    def average_usefulness(self) -> float:
        if not self.options:
            return 0.0

        return sum(
            option.usefulness
            for option in self.options
        ) / len(self.options)

    def as_dict(self) -> dict[str, Any]:
        return {
            "facet_type": self.facet_type.value,
            "title": self.title,
            "priority": self.priority,
            "options": [
                option.as_dict()
                for option in self.options
            ],
        }


@dataclass(slots=True, frozen=True)
class FacetSearchResult:
    """
    Результат работы Facet Engine.

    В groups обычно возвращается только одна
    наиболее полезная группа следующего уровня.
    """

    original_query: str
    normalized_query: str
    product_type: str | None
    groups: tuple[FacetGroup, ...]
    candidates_count: int

    @property
    def has_facets(self) -> bool:
        return any(
            group.options
            for group in self.groups
        )

    @property
    def options_count(self) -> int:
        return sum(
            len(group.options)
            for group in self.groups
        )


MILK_PROCESSING = (
    FacetDefinition(
        key="pasteurized",
        title="Пастеризованное",
        query_term="пастеризованное",
        match_terms=(
            "пастеризованное",
            "пастеризованный",
            "пастеризованная",
        ),
    ),
    FacetDefinition(
        key="ultra_pasteurized",
        title="Ультрапастеризованное",
        query_term="ультрапастеризованное",
        match_terms=(
            "ультрапастеризованное",
            "ультрапастеризованный",
            "ультрапастеризованная",
            "ультравысокотемпературно",
            "ультравысокотемпературное",
            "uht",
        ),
    ),
    FacetDefinition(
        key="baked",
        title="Топлёное",
        query_term="топлёное",
        match_terms=(
            "топленое",
            "топлёное",
            "топленый",
            "топлёный",
            "топленая",
            "топлёная",
        ),
    ),
)


MILK_PRODUCT_KIND = (
    FacetDefinition(
        key="drinking",
        title="Питьевое",
        query_term="питьевое",
        match_terms=(
            "молоко питьевое",
            "питьевое молоко",
        ),
    ),
    FacetDefinition(
        key="condensed",
        title="Сгущённое",
        query_term="сгущённое",
        match_terms=(
            "сгущенное",
            "сгущённое",
            "сгущенка",
            "сгущёнка",
            "condensed milk",
        ),
    ),
    FacetDefinition(
        key="dry",
        title="Сухое",
        query_term="сухое",
        match_terms=(
            "сухое молоко",
            "молоко сухое",
            "milk powder",
        ),
    ),
    FacetDefinition(
        key="plant",
        title="Растительное",
        query_term="растительное",
        match_terms=(
            "растительное молоко",
            "овсяное молоко",
            "соевое молоко",
            "кокосовое молоко",
            "миндальное молоко",
            "oat milk",
            "soy milk",
            "coconut milk",
            "almond milk",
        ),
    ),
)


MILK_SPECIAL = (
    FacetDefinition(
        key="lactose_free",
        title="Безлактозное",
        query_term="безлактозное",
        match_terms=(
            "безлактозное",
            "без лактозы",
            "низколактозное",
            "low lactose",
            "lactose free",
        ),
    ),
)


COFFEE_FORM = (
    FacetDefinition(
        key="instant",
        title="Растворимый",
        query_term="растворимый",
        match_terms=(
            "растворимый кофе",
            "кофе растворимый",
            "instant coffee",
        ),
    ),
    FacetDefinition(
        key="ground",
        title="Молотый",
        query_term="молотый",
        match_terms=(
            "молотый кофе",
            "кофе молотый",
            "ground coffee",
        ),
    ),
    FacetDefinition(
        key="beans",
        title="В зёрнах",
        query_term="в зёрнах",
        match_terms=(
            "кофе в зернах",
            "кофе в зёрнах",
            "зерновой кофе",
            "кофе зерновой",
            "coffee beans",
        ),
    ),
    FacetDefinition(
        key="capsules",
        title="В капсулах",
        query_term="в капсулах",
        match_terms=(
            "кофе в капсулах",
            "кофейные капсулы",
            "coffee capsules",
        ),
    ),
    FacetDefinition(
        key="drip",
        title="Дрип-пакеты",
        query_term="дрип-пакеты",
        match_terms=(
            "дрип пакет",
            "дрип-пакет",
            "дрип пакеты",
            "дрип-пакеты",
            "drip coffee",
            "coffee drip",
        ),
    ),
)


COFFEE_PREPARATION = (
    FacetDefinition(
        key="three_in_one",
        title="3 в 1",
        query_term="3 в 1",
        match_terms=(
            "3 в 1",
            "три в одном",
            "3in1",
        ),
    ),
    FacetDefinition(
        key="decaf",
        title="Без кофеина",
        query_term="без кофеина",
        match_terms=(
            "без кофеина",
            "декофеинизированный",
            "декофеинизированное",
            "decaf",
        ),
    ),
    FacetDefinition(
        key="sublimated",
        title="Сублимированный",
        query_term="сублимированный",
        match_terms=(
            "сублимированный",
            "сублимированное",
            "freeze dried",
            "freeze-dried",
        ),
    ),
)


HERRING_PRODUCT_KIND = (
    FacetDefinition(
        key="fillet",
        title="Филе",
        query_term="филе",
        match_terms=(
            "филе сельди",
            "сельдь филе",
            "филе-кусочки",
            "филе кусочки",
        ),
    ),
    FacetDefinition(
        key="preserves",
        title="Пресервы",
        query_term="пресервы",
        match_terms=(
            "пресервы",
            "пресерв",
        ),
    ),
    FacetDefinition(
        key="pieces",
        title="Кусочки",
        query_term="кусочки",
        match_terms=(
            "кусочки сельди",
            "сельдь кусочки",
            "филе-кусочки",
            "филе кусочки",
        ),
    ),
)


HERRING_PREPARATION = (
    FacetDefinition(
        key="lightly_salted",
        title="Слабосолёная",
        query_term="слабосолёная",
        match_terms=(
            "слабосоленая",
            "слабосолёная",
            "слабосоленый",
            "слабосолёный",
            "малосольная",
        ),
    ),
    FacetDefinition(
        key="spicy_salted",
        title="Пряного посола",
        query_term="пряного посола",
        match_terms=(
            "пряного посола",
            "пряный посол",
        ),
    ),
    FacetDefinition(
        key="in_oil",
        title="В масле",
        query_term="в масле",
        match_terms=(
            "в масле",
            "масляной заливке",
            "масляная заливка",
        ),
    ),
    FacetDefinition(
        key="with_onion",
        title="С луком",
        query_term="с луком",
        match_terms=(
            "с луком",
            "луковая заливка",
        ),
    ),
)


PRODUCT_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "молоко": (
        "молоко",
        "milk",
    ),
    "кофе": (
        "кофе",
        "coffee",
        "кофейный",
    ),
    "сельдь": (
        "сельдь",
        "селедка",
        "селёдка",
        "herring",
    ),
}


FACET_SCHEMAS: dict[
    str,
    tuple[
        tuple[
            FacetType,
            str,
            tuple[FacetDefinition, ...],
            int,
        ],
        ...,
    ],
] = {
    "молоко": (
        (
            FacetType.PROCESSING,
            "Способ обработки",
            MILK_PROCESSING,
            10,
        ),
        (
            FacetType.PRODUCT_KIND,
            "Вид молока",
            MILK_PRODUCT_KIND,
            20,
        ),
        (
            FacetType.SPECIAL,
            "Особенности",
            MILK_SPECIAL,
            30,
        ),
    ),
    "кофе": (
        (
            FacetType.FORM,
            "Вид кофе",
            COFFEE_FORM,
            10,
        ),
        (
            FacetType.PREPARATION,
            "Особенности",
            COFFEE_PREPARATION,
            20,
        ),
    ),
    "сельдь": (
        (
            FacetType.PRODUCT_KIND,
            "Вид сельди",
            HERRING_PRODUCT_KIND,
            10,
        ),
        (
            FacetType.PREPARATION,
            "Способ приготовления",
            HERRING_PREPARATION,
            20,
        ),
    ),
}


FAT_PERCENT_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d{1,2}(?:[.,]\d{1,2})?)"
    r"\s*%"
)


def normalize_facet_text(
    value: Any,
) -> str:
    """
    Нормализует текст для смыслового анализа.

    Этот вариант используется для словесных
    фасетов. Проценты извлекаются отдельно
    из исходного текста.
    """

    if value is None:
        return ""

    normalized = normalize_text(
        str(value)
    )

    normalized = normalized.replace(
        "ё",
        "е",
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    ).strip()

    return normalized


def normalize_raw_text(
    value: Any,
) -> str:
    """
    Мягко нормализует исходный текст.

    В отличие от normalize_text(), сохраняет:
    - знак процента;
    - запятые;
    - точки;
    - числовые значения.
    """

    if value is None:
        return ""

    text = str(value).lower()

    text = text.replace(
        "ё",
        "е",
    )

    text = text.replace(
        "\xa0",
        " ",
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    ).strip()

    return text


def detect_product_type(
    query: str,
) -> str | None:
    """
    Определяет базовый тип продукта.
    """

    normalized_query = normalize_facet_text(
        query
    )

    if not normalized_query:
        return None

    for product_type, aliases in (
        PRODUCT_TYPE_ALIASES.items()
    ):
        for alias in aliases:
            normalized_alias = normalize_facet_text(
                alias
            )

            if not normalized_alias:
                continue

            if " " in normalized_alias:
                if normalized_alias in normalized_query:
                    return product_type
                continue

            if re.search(
                rf"(?<!\w)"
                rf"{re.escape(normalized_alias)}"
                rf"(?!\w)",
                normalized_query,
            ):
                return product_type

    return None


def get_candidate_values(
    *,
    product,
    brand,
    category,
) -> tuple[Any, ...]:
    """
    Возвращает поля товара, которые можно
    использовать для анализа фасетов.
    """

    return (
        getattr(
            product,
            "name",
            None,
        ),
        getattr(
            product,
            "normalized_name",
            None,
        ),
        getattr(
            product,
            "subtype",
            None,
        ),
        getattr(
            product,
            "description",
            None,
        ),
        getattr(
            product,
            "keywords",
            None,
        ),
        getattr(
            product,
            "search_text",
            None,
        ),
        getattr(
            brand,
            "name",
            None,
        ),
        getattr(
            category,
            "name",
            None,
        ),
    )


def build_facet_candidate(
    *,
    product,
    brand,
    category,
) -> FacetCandidate:
    """
    Собирает две версии текста товара:

    1. нормализованную — для слов;
    2. исходную — для процентов и чисел.
    """

    values = get_candidate_values(
        product=product,
        brand=brand,
        category=category,
    )

    normalized_parts = [
        normalize_facet_text(value)
        for value in values
        if value is not None
    ]

    raw_parts = [
        normalize_raw_text(value)
        for value in values
        if value is not None
    ]

    return FacetCandidate(
        normalized_text=" ".join(
            part
            for part in normalized_parts
            if part
        ),
        raw_text=" ".join(
            part
            for part in raw_parts
            if part
        ),
    )


def text_contains_term(
    *,
    text: str,
    term: str,
) -> bool:
    """
    Проверяет наличие слова или фразы.
    """

    normalized_term = normalize_facet_text(
        term
    )

    if not normalized_term:
        return False

    if " " in normalized_term:
        return normalized_term in text

    return bool(
        re.search(
            rf"(?<!\w)"
            rf"{re.escape(normalized_term)}"
            rf"(?!\w)",
            text,
        )
    )


def matches_definition(
    *,
    candidate: FacetCandidate,
    definition: FacetDefinition,
) -> bool:
    """
    Проверяет соответствие товара фасету.
    """

    return any(
        text_contains_term(
            text=candidate.normalized_text,
            term=term,
        )
        for term in definition.match_terms
    )


def query_already_contains_definition(
    *,
    query: str,
    definition: FacetDefinition,
) -> bool:
    """
    Проверяет, выбрал ли пользователь
    этот фасет ранее.
    """

    normalized_query = normalize_facet_text(
        query
    )

    terms = (
        definition.query_term,
        *definition.match_terms,
    )

    return any(
        text_contains_term(
            text=normalized_query,
            term=term,
        )
        for term in terms
    )


def query_contains_fat_percent(
    query: str,
) -> bool:
    """
    Проверяет, указана ли жирность в запросе.
    """

    raw_query = normalize_raw_text(
        query
    )

    return bool(
        FAT_PERCENT_PATTERN.search(
            raw_query
        )
    )


def build_refined_query(
    *,
    original_query: str,
    query_term: str,
) -> str:
    """
    Формирует новый запрос после выбора фасета.
    """

    normalized_original = " ".join(
        original_query.strip().split()
    )

    normalized_term = " ".join(
        query_term.strip().split()
    )

    if not normalized_original:
        return normalized_term

    if not normalized_term:
        return normalized_original

    normalized_original_for_check = (
        normalize_raw_text(
            normalized_original
        )
    )

    normalized_term_for_check = (
        normalize_raw_text(
            normalized_term
        )
    )

    if (
        normalized_term_for_check
        in normalized_original_for_check
    ):
        return normalized_original

    return " ".join(
        (
            normalized_original,
            normalized_term,
        )
    )


def count_facet_options(
    *,
    original_query: str,
    candidates: Iterable[FacetCandidate],
    definitions: tuple[FacetDefinition, ...],
) -> list[
    tuple[
        FacetDefinition,
        int,
    ]
]:
    """
    Подсчитывает количество товаров
    для каждого фасета.
    """

    counts: Counter[str] = Counter()

    definitions_by_key = {
        definition.key: definition
        for definition in definitions
    }

    for candidate in candidates:
        matched_keys: set[str] = set()

        for definition in definitions:
            if query_already_contains_definition(
                query=original_query,
                definition=definition,
            ):
                continue

            if matches_definition(
                candidate=candidate,
                definition=definition,
            ):
                matched_keys.add(
                    definition.key
                )

        for key in matched_keys:
            counts[key] += 1

    result: list[
        tuple[
            FacetDefinition,
            int,
        ]
    ] = []

    for key, count in counts.items():
        definition = definitions_by_key.get(
            key
        )

        if definition is None:
            continue

        result.append(
            (
                definition,
                count,
            )
        )

    return result


def calculate_option_usefulness(
    *,
    count: int,
    candidates_count: int,
) -> float:
    """
    Оценивает полезность отдельного варианта.

    Вариант полезен, если:
    - поддерживается несколькими товарами;
    - не охватывает почти всю выдачу;
    - реально уменьшает множество кандидатов.
    """

    if (
        count <= 0
        or candidates_count <= 0
    ):
        return 0.0

    if count < 2:
        return 0.0

    coverage = (
        count
        / candidates_count
    )

    if coverage >= 0.96:
        return 0.0

    balance_score = max(
        0.0,
        1.0 - abs(
            coverage - 0.35
        ),
    )

    support_score = min(
        count / 12.0,
        1.0,
    )

    narrowing_score = (
        1.0 - coverage
    )

    return (
        balance_score * 0.45
        + support_score * 0.25
        + narrowing_score * 0.30
    )


def build_definition_group(
    *,
    original_query: str,
    facet_type: FacetType,
    group_title: str,
    definitions: tuple[FacetDefinition, ...],
    candidates: list[FacetCandidate],
    candidates_count: int,
    option_limit: int,
    priority: int,
) -> FacetGroup | None:
    """
    Строит группу словесных фасетов.
    """

    counted_options = count_facet_options(
        original_query=original_query,
        candidates=candidates,
        definitions=definitions,
    )

    ranked_options: list[
        tuple[
            float,
            int,
            FacetDefinition,
        ]
    ] = []

    for definition, count in counted_options:
        usefulness = calculate_option_usefulness(
            count=count,
            candidates_count=candidates_count,
        )

        if usefulness <= 0:
            continue

        ranked_options.append(
            (
                usefulness,
                count,
                definition,
            )
        )

    ranked_options.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2].title,
        ),
        reverse=True,
    )

    options = tuple(
        FacetOption(
            facet_type=facet_type,
            key=definition.key,
            title=definition.title,
            query=build_refined_query(
                original_query=original_query,
                query_term=definition.query_term,
            ),
            count=count,
            usefulness=usefulness,
        )
        for (
            usefulness,
            count,
            definition,
        ) in ranked_options[
            :option_limit
        ]
    )

    if len(options) < 2:
        return None

    return FacetGroup(
        facet_type=facet_type,
        title=group_title,
        options=options,
        priority=priority,
    )


def normalize_fat_value(
    value: float,
) -> str:
    """
    Форматирует значение жирности.

    3.0 -> 3
    3.2 -> 3,2
    """

    formatted = f"{value:g}"

    return formatted.replace(
        ".",
        ",",
    )


def extract_fat_percentages(
    *,
    original_query: str,
    candidates: list[FacetCandidate],
    candidates_count: int,
    option_limit: int,
    priority: int = 15,
) -> FacetGroup | None:
    """
    Извлекает жирность из исходных строк.

    Важно: используется candidate.raw_text,
    потому что обычная нормализация может
    удалить знак процента.
    """

    if query_contains_fat_percent(
        original_query
    ):
        return None

    counts: Counter[str] = Counter()

    for candidate in candidates:
        values_in_candidate: set[str] = set()

        for match in FAT_PERCENT_PATTERN.finditer(
            candidate.raw_text
        ):
            raw_value = (
                match.group(1)
                .replace(",", ".")
            )

            try:
                numeric_value = float(
                    raw_value
                )
            except ValueError:
                continue

            # Реалистичный диапазон жирности
            # молочных продуктов.
            if not 0.0 <= numeric_value <= 20.0:
                continue

            normalized_value = f"{numeric_value:g}"

            values_in_candidate.add(
                normalized_value
            )

        for value in values_in_candidate:
            counts[value] += 1

    ranked_options: list[
        tuple[
            float,
            int,
            float,
            str,
        ]
    ] = []

    for value, count in counts.items():
        usefulness = calculate_option_usefulness(
            count=count,
            candidates_count=candidates_count,
        )

        if usefulness <= 0:
            continue

        numeric_value = float(
            value
        )

        ranked_options.append(
            (
                usefulness,
                count,
                numeric_value,
                value,
            )
        )

    ranked_options.sort(
        key=lambda item: (
            item[0],
            item[1],
            -abs(
                item[2] - 3.2
            ),
        ),
        reverse=True,
    )

    options = tuple(
        FacetOption(
            facet_type=FacetType.FAT_PERCENT,
            key=(
                "fat_"
                + value.replace(
                    ".",
                    "_",
                )
            ),
            title=(
                f"{normalize_fat_value(numeric_value)}%"
            ),
            query=build_refined_query(
                original_query=original_query,
                query_term=(
                    f"{normalize_fat_value(numeric_value)}%"
                ),
            ),
            count=count,
            usefulness=usefulness,
        )
        for (
            usefulness,
            count,
            numeric_value,
            value,
        ) in ranked_options[
            :option_limit
        ]
    )

    if len(options) < 2:
        return None

    return FacetGroup(
        facet_type=FacetType.FAT_PERCENT,
        title="Жирность",
        options=options,
        priority=priority,
    )


def select_next_group(
    groups: list[FacetGroup],
) -> FacetGroup | None:
    """
    Выбирает только один следующий уровень.

    Сначала учитывается смысловой приоритет,
    затем полезность вариантов.

    Это предотвращает смешивание кнопок:

    - пастеризованное;
    - 3,2%;
    - безлактозное

    на одном экране.
    """

    if not groups:
        return None

    return sorted(
        groups,
        key=lambda group: (
            group.priority,
            -group.average_usefulness,
            -group.total_count,
            group.title,
        ),
    )[0]


async def build_product_facets(
    *,
    session: AsyncSession,
    query: str,
    candidates_limit: int = 100,
    group_limit: int = 1,
    option_limit: int = 5,
) -> FacetSearchResult:
    """
    Главная точка входа Facet Engine.

    Алгоритм:

    1. определяет тип продукта;
    2. получает подходящие товары;
    3. строит контролируемые фасеты;
    4. извлекает жирность из исходных строк;
    5. выбирает только один следующий уровень;
    6. не показывает уже выбранные параметры.

    group_limit сохранён для совместимости
    с существующим Search Pipeline, но движок
    намеренно возвращает только один лучший
    следующий уровень.
    """

    cleaned_query = " ".join(
        query.strip().split()
    )

    normalized_query = normalize_facet_text(
        cleaned_query
    )

    product_type = detect_product_type(
        cleaned_query
    )

    if (
        not cleaned_query
        or product_type is None
    ):
        return FacetSearchResult(
            original_query=query,
            normalized_query=normalized_query,
            product_type=product_type,
            groups=(),
            candidates_count=0,
        )

    safe_candidates_limit = max(
        10,
        min(
            candidates_limit,
            200,
        ),
    )

    safe_option_limit = max(
        2,
        min(
            option_limit,
            6,
        ),
    )

    rows = await search_products(
        session=session,
        query=cleaned_query,
        limit=safe_candidates_limit,
    )

    if not rows:
        return FacetSearchResult(
            original_query=cleaned_query,
            normalized_query=normalized_query,
            product_type=product_type,
            groups=(),
            candidates_count=0,
        )

    candidates = [
        build_facet_candidate(
            product=product,
            brand=brand,
            category=category,
        )
        for product, brand, category
        in rows
    ]

    candidates_count = len(
        candidates
    )

    available_groups: list[
        FacetGroup
    ] = []

    schema = FACET_SCHEMAS.get(
        product_type,
        (),
    )

    for (
        facet_type,
        group_title,
        definitions,
        priority,
    ) in schema:
        group = build_definition_group(
            original_query=cleaned_query,
            facet_type=facet_type,
            group_title=group_title,
            definitions=definitions,
            candidates=candidates,
            candidates_count=candidates_count,
            option_limit=safe_option_limit,
            priority=priority,
        )

        if group is not None:
            available_groups.append(
                group
            )

    if product_type == "молоко":
        fat_group = extract_fat_percentages(
            original_query=cleaned_query,
            candidates=candidates,
            candidates_count=candidates_count,
            option_limit=safe_option_limit,
            priority=15,
        )

        if fat_group is not None:
            available_groups.append(
                fat_group
            )

    selected_group = select_next_group(
        available_groups
    )

    selected_groups: tuple[
        FacetGroup,
        ...
    ]

    if selected_group is None:
        selected_groups = ()
    else:
        selected_groups = (
            selected_group,
        )

    return FacetSearchResult(
        original_query=cleaned_query,
        normalized_query=normalized_query,
        product_type=product_type,
        groups=selected_groups,
        candidates_count=candidates_count,
    )


def flatten_facet_options(
    result: FacetSearchResult,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    Превращает выбранную группу
    в список кнопок для Telegram.

    Порядок вариантов сохраняется таким,
    каким его определил Facet Engine.
    """

    safe_limit = max(
        1,
        min(
            limit,
            8,
        ),
    )

    if not result.groups:
        return []

    group = result.groups[0]

    return [
        {
            "title": option.title,
            "query": option.query,
            "count": option.count,
            "facet_type": (
                option.facet_type.value
            ),
            "facet_key": option.key,
            "group_title": group.title,
        }
        for option in group.options[
            :safe_limit
        ]
    ]
