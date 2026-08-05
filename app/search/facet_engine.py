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
    PRODUCT_KIND = "product_kind"
    PROCESSING = "processing"
    SPECIAL = "special"
    FORM = "form"
    PREPARATION = "preparation"
    FAT_PERCENT = "fat_percent"


@dataclass(slots=True, frozen=True)
class FacetDefinition:
    """
    Описание одного разрешённого фасета.

    match_terms:
        Слова и фразы, по которым фасет
        определяется в данных товара.

    title:
        Понятное название кнопки.

    query_term:
        Значение, добавляемое к запросу
        после выбора фасета.
    """

    key: str
    title: str
    query_term: str
    match_terms: tuple[str, ...]


@dataclass(slots=True, frozen=True)
class FacetOption:
    """
    Один вариант уточнения для пользователя.
    """

    facet_type: FacetType
    key: str
    title: str
    query: str
    count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "facet_type": self.facet_type.value,
            "key": self.key,
            "title": self.title,
            "query": self.query,
            "count": self.count,
        }


@dataclass(slots=True, frozen=True)
class FacetGroup:
    """
    Группа связанных уточнений.

    Например:

    Вид продукта:
    - питьевое;
    - сгущённое;
    - сухое.
    """

    facet_type: FacetType
    title: str
    options: tuple[FacetOption, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "facet_type": self.facet_type.value,
            "title": self.title,
            "options": [
                option.as_dict()
                for option in self.options
            ],
        }


@dataclass(slots=True, frozen=True)
class FacetSearchResult:
    """
    Результат анализа фасетов.

    product_type:
        Понятный базовый тип продукта:
        молоко, кофе или сельдь.

    groups:
        Только разрешённые и реально найденные
        варианты уточнения.
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

MILK_PROCESSING = (
    FacetDefinition(
        key="pasteurized",
        title="Пастеризованное",
        query_term="пастеризованное",
        match_terms=(
            "пастеризованное",
            "пастеризованный",
        ),
    ),
    FacetDefinition(
        key="ultra_pasteurized",
        title="Ультрапастеризованное",
        query_term="ультрапастеризованное",
        match_terms=(
            "ультрапастеризованное",
            "ультрапастеризованный",
            "ультравысокотемпературно",
            "ультравысокотемпературное",
            "uhт",
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
            "decaf",
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
            "лук",
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
        ],
        ...,
    ],
] = {
    "молоко": (
        (
            FacetType.PRODUCT_KIND,
            "Какой вид молока?",
            MILK_PRODUCT_KIND,
        ),
        (
            FacetType.PROCESSING,
            "Способ обработки",
            MILK_PROCESSING,
        ),
        (
            FacetType.SPECIAL,
            "Особенности",
            MILK_SPECIAL,
        ),
    ),
    "кофе": (
        (
            FacetType.FORM,
            "Какой кофе?",
            COFFEE_FORM,
        ),
        (
            FacetType.PREPARATION,
            "Особенности",
            COFFEE_PREPARATION,
        ),
    ),
    "сельдь": (
        (
            FacetType.PRODUCT_KIND,
            "Какой вид сельди?",
            HERRING_PRODUCT_KIND,
        ),
        (
            FacetType.PREPARATION,
            "Способ приготовления",
            HERRING_PREPARATION,
        ),
    ),
}


FAT_PERCENT_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d{1,2}(?:[.,]\d)?)"
    r"\s*%"
)


def normalize_facet_text(
    value: Any,
) -> str:
    """
    Нормализует текст товара для анализа фасетов.
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


def detect_product_type(
    query: str,
) -> str | None:
    """
    Определяет базовый тип продукта по запросу.

    На первом этапе поддерживаются:
    - молоко;
    - кофе;
    - сельдь.
    """

    normalized_query = normalize_facet_text(
        query
    )

    query_words = set(
        normalized_query.split()
    )

    for product_type, aliases in (
        PRODUCT_TYPE_ALIASES.items()
    ):
        for alias in aliases:
            normalized_alias = normalize_facet_text(
                alias
            )

            if (
                normalized_alias
                in normalized_query
            ):
                return product_type

            if normalized_alias in query_words:
                return product_type

    return None


def build_product_search_text(
    *,
    product,
    brand,
    category,
) -> str:
    """
    Собирает внутренний текст товара,
    используемый только для анализа фасетов.

    Этот текст никогда не показывается
    пользователю.
    """

    values = (
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

    normalized_values = [
        normalize_facet_text(value)
        for value in values
        if value
    ]

    return " ".join(
        normalized_values
    )


def text_contains_term(
    *,
    text: str,
    term: str,
) -> bool:
    """
    Проверяет наличие слова или фразы.

    Для однословного значения используются
    границы слова, чтобы избегать случайных
    частичных совпадений.
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
    product_text: str,
    definition: FacetDefinition,
) -> bool:
    """
    Проверяет соответствие товара
    разрешённому варианту фасета.
    """

    return any(
        text_contains_term(
            text=product_text,
            term=term,
        )
        for term in definition.match_terms
    )


def query_already_contains_option(
    *,
    query: str,
    definition: FacetDefinition,
) -> bool:
    """
    Не показывает фасет, уже указанный
    пользователем в исходном запросе.
    """

    normalized_query = normalize_facet_text(
        query
    )

    return any(
        text_contains_term(
            text=normalized_query,
            term=term,
        )
        for term in (
            definition.query_term,
            *definition.match_terms,
        )
    )


def build_refined_query(
    *,
    original_query: str,
    query_term: str,
) -> str:
    """
    Формирует безопасный поисковый запрос
    после выбора фасета.

    В отличие от старых интентов, исходный
    запрос не теряется.
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

    combined = (
        f"{normalized_original} "
        f"{normalized_term}"
    )

    return " ".join(
        combined.split()
    )


def count_facet_options(
    *,
    original_query: str,
    candidate_texts: Iterable[str],
    definitions: tuple[FacetDefinition, ...],
) -> list[
    tuple[
        FacetDefinition,
        int,
    ]
]:
    """
    Подсчитывает количество товаров
    для каждого разрешённого фасета.
    """

    counts: Counter[str] = Counter()

    definitions_by_key = {
        definition.key: definition
        for definition in definitions
    }

    for product_text in candidate_texts:
        matched_keys: set[str] = set()

        for definition in definitions:
            if query_already_contains_option(
                query=original_query,
                definition=definition,
            ):
                continue

            if matches_definition(
                product_text=product_text,
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
    Оценивает полезность фасета.

    Плохие варианты:
    - встречаются только у одного товара;
    - охватывают почти всю выдачу и ничего
      фактически не уточняют.

    Лучшие варианты делят выдачу
    на осмысленные части.
    """

    if (
        count <= 0
        or candidates_count <= 0
    ):
        return 0.0

    coverage = (
        count
        / candidates_count
    )

    if count < 2:
        return 0.0

    if coverage >= 0.95:
        return 0.0

    balance_score = (
        1.0
        - abs(
            coverage - 0.40
        )
    )

    support_score = min(
        count / 10.0,
        1.0,
    )

    return (
        balance_score * 0.7
        + support_score * 0.3
    )


def build_facet_group(
    *,
    original_query: str,
    facet_type: FacetType,
    group_title: str,
    definitions: tuple[FacetDefinition, ...],
    candidate_texts: list[str],
    candidates_count: int,
    option_limit: int,
) -> FacetGroup | None:
    """
    Создаёт одну группу полезных фасетов.
    """

    counted_options = count_facet_options(
        original_query=original_query,
        candidate_texts=candidate_texts,
        definitions=definitions,
    )

    ranked_options: list[
        tuple[
            float,
            FacetDefinition,
            int,
        ]
    ] = []

    for definition, count in counted_options:
        usefulness = (
            calculate_option_usefulness(
                count=count,
                candidates_count=(
                    candidates_count
                ),
            )
        )

        if usefulness <= 0:
            continue

        ranked_options.append(
            (
                usefulness,
                definition,
                count,
            )
        )

    ranked_options.sort(
        key=lambda item: (
            item[0],
            item[2],
            item[1].title,
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
        )
        for (
            _usefulness,
            definition,
            count,
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
    )


def extract_fat_percentages(
    *,
    original_query: str,
    candidate_texts: list[str],
    candidates_count: int,
    option_limit: int,
) -> FacetGroup | None:
    """
    Извлекает реальные значения жирности
    из найденных молочных товаров.

    Вместо кнопок «Жира», «Долей»
    пользователь получает:

    - 1,5%;
    - 2,5%;
    - 3,2%;
    - 3,5%.
    """

    normalized_query = normalize_facet_text(
        original_query
    )

    if FAT_PERCENT_PATTERN.search(
        normalized_query
    ):
        return None

    counts: Counter[str] = Counter()

    for product_text in candidate_texts:
        values_in_product: set[str] = set()

        for match in FAT_PERCENT_PATTERN.finditer(
            product_text
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

            if not 0 <= numeric_value <= 20:
                continue

            normalized_value = (
                f"{numeric_value:g}"
            )

            values_in_product.add(
                normalized_value
            )

        for value in values_in_product:
            counts[value] += 1

    ranked: list[
        tuple[
            float,
            float,
            int,
            str,
        ]
    ] = []

    for value, count in counts.items():
        usefulness = (
            calculate_option_usefulness(
                count=count,
                candidates_count=(
                    candidates_count
                ),
            )
        )

        if usefulness <= 0:
            continue

        numeric_value = float(
            value
        )

        ranked.append(
            (
                usefulness,
                -abs(
                    numeric_value - 3.2
                ),
                count,
                value,
            )
        )

    ranked.sort(
        reverse=True
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
                value.replace(
                    ".",
                    ",",
                )
                + "%"
            ),
            query=build_refined_query(
                original_query=original_query,
                query_term=(
                    value.replace(
                        ".",
                        ",",
                    )
                    + "%"
                ),
            ),
            count=count,
        )
        for (
            _usefulness,
            _preferred_value,
            count,
            value,
        ) in ranked[
            :option_limit
        ]
    )

    if len(options) < 2:
        return None

    return FacetGroup(
        facet_type=FacetType.FAT_PERCENT,
        title="Жирность",
        options=options,
    )


async def build_product_facets(
    *,
    session: AsyncSession,
    query: str,
    candidates_limit: int = 100,
    group_limit: int = 2,
    option_limit: int = 5,
) -> FacetSearchResult:
    """
    Главная точка входа Facet Engine.

    Алгоритм:

    1. определяет базовый продукт;
    2. находит релевантных кандидатов;
    3. анализирует только разрешённые фасеты;
    4. показывает только реально встречающиеся
       и полезные варианты;
    5. сохраняет исходный запрос при уточнении.

    Facet Engine не строит варианты
    из случайных слов базы.
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

    safe_group_limit = max(
        1,
        min(
            group_limit,
            4,
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

    candidate_texts = [
        build_product_search_text(
            product=product,
            brand=brand,
            category=category,
        )
        for product, brand, category
        in rows
    ]

    candidates_count = len(
        candidate_texts
    )

    schema = FACET_SCHEMAS.get(
        product_type,
        (),
    )

    candidate_groups: list[
        FacetGroup
    ] = []

    for (
        facet_type,
        group_title,
        definitions,
    ) in schema:
        group = build_facet_group(
            original_query=cleaned_query,
            facet_type=facet_type,
            group_title=group_title,
            definitions=definitions,
            candidate_texts=candidate_texts,
            candidates_count=candidates_count,
            option_limit=safe_option_limit,
        )

        if group is not None:
            candidate_groups.append(
                group
            )

    if product_type == "молоко":
        fat_group = extract_fat_percentages(
            original_query=cleaned_query,
            candidate_texts=candidate_texts,
            candidates_count=candidates_count,
            option_limit=safe_option_limit,
        )

        if fat_group is not None:
            candidate_groups.append(
                fat_group
            )

    candidate_groups.sort(
        key=lambda group: (
            len(group.options),
            sum(
                option.count
                for option in group.options
            ),
        ),
        reverse=True,
    )

    return FacetSearchResult(
        original_query=cleaned_query,
        normalized_query=normalized_query,
        product_type=product_type,
        groups=tuple(
            candidate_groups[
                :safe_group_limit
            ]
        ),
        candidates_count=candidates_count,
    )


def flatten_facet_options(
    result: FacetSearchResult,
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    Превращает группы фасетов в компактный
    список кнопок для текущей клавиатуры.

    Позже интерфейс сможет показывать
    отдельные секции по группам.
    """

    safe_limit = max(
        1,
        min(
            limit,
            8,
        ),
    )

    options: list[
        FacetOption
    ] = []

    for group in result.groups:
        options.extend(
            group.options
        )

    options.sort(
        key=lambda option: (
            option.count,
            option.title,
        ),
        reverse=True,
    )

    return [
        {
            "title": option.title,
            "query": option.query,
            "count": option.count,
            "facet_type": (
                option.facet_type.value
            ),
            "facet_key": option.key,
        }
        for option in options[
            :safe_limit
        ]
  ]
