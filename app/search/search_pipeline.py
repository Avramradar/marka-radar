from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.product_repository import (
    search_products,
)
from app.search.decision_search import (
    DecisionSearchResult,
    run_decision_search,
)
from app.search.engine import (
    run_search_engine,
)
from app.search.family_search import (
    find_product_families,
)
from app.search.human_intents import (
    prepare_human_intents,
)


class SearchPipelineScreen(StrEnum):
    """
    Экран, который должен увидеть пользователь.

    Pipeline возвращает не просто товары,
    а готовое решение для интерфейса.
    """

    EMPTY = "empty"
    BARCODE_PRODUCT = "barcode_product"
    INTENTS = "intents"
    FAMILIES = "families"
    DECISION = "decision"
    NOT_FOUND = "not_found"


@dataclass(slots=True)
class SearchPipelineProduct:
    """
    Конкретный товар, найденный по штрихкоду.

    Используется отдельно, потому что поиск
    по штрихкоду не требует уточнений.
    """

    product: Any
    brand: Any
    category: Any


@dataclass(slots=True)
class SearchPipelineResult:
    """
    Полный результат поискового конвейера.

    Обработчик Telegram должен смотреть только
    на поле screen и показывать соответствующий
    пользовательский экран.
    """

    screen: SearchPipelineScreen

    original_query: str
    normalized_query: str

    barcode_product: SearchPipelineProduct | None

    intent_groups: list[dict[str, Any]]
    families: list[dict[str, Any]]

    decision: DecisionSearchResult | None

    corrected_query: str | None
    explanation: str | None

    @property
    def has_results(self) -> bool:
        return self.screen not in {
            SearchPipelineScreen.EMPTY,
            SearchPipelineScreen.NOT_FOUND,
        }

    @property
    def is_barcode_result(self) -> bool:
        return (
            self.screen
            == SearchPipelineScreen.BARCODE_PRODUCT
        )

    @property
    def should_show_intents(self) -> bool:
        return (
            self.screen
            == SearchPipelineScreen.INTENTS
        )

    @property
    def should_show_families(self) -> bool:
        return (
            self.screen
            == SearchPipelineScreen.FAMILIES
        )

    @property
    def should_show_decision(self) -> bool:
        return (
            self.screen
            == SearchPipelineScreen.DECISION
        )


def clean_pipeline_query(
    query: str,
) -> str:
    """
    Выполняет безопасную первичную очистку запроса.

    Полная нормализация и исправление опечаток
    позднее будут выполняться Query Parser.
    """

    return " ".join(
        query.strip().split()
    )


def is_possible_barcode(
    query: str,
) -> bool:
    """
    Проверяет, похож ли запрос на штрихкод.

    Не каждое число является штрихкодом.
    Поддерживаются распространённые длины:
    EAN-8, UPC-A, EAN-13 и внутренние коды.
    """

    return (
        query.isdigit()
        and 8 <= len(query) <= 14
    )


def should_use_intents(
    *,
    query: str,
    intent_groups: list[dict[str, Any]],
) -> bool:
    """
    Решает, нужно ли показывать уточняющие варианты.

    Уточнения показываются только для короткого
    и широкого запроса.

    Конкретный запрос не должен заставлять
    пользователя проходить лишние уровни.
    """

    query_words = query.split()

    if len(query_words) > 2:
        return False

    if len(intent_groups) < 2:
        return False

    return True


def should_use_families(
    *,
    query: str,
    families: list[dict[str, Any]],
) -> bool:
    """
    Решает, нужно ли показывать семейства.

    Семейства используются как компактные фильтры,
    а не как обязательный лабиринт поиска.
    """

    query_words = query.split()

    if len(query_words) > 2:
        return False

    if len(families) < 2:
        return False

    meaningful_families = [
        family
        for family in families
        if int(
            family.get(
                "products_count",
                0,
            )
        ) > 0
    ]

    return len(meaningful_families) >= 2


def family_quality_key(
    family: dict[str, Any],
) -> tuple[float, int, str]:
    """
    Сортирует семейства по релевантности
    и числу товаров.
    """

    return (
        float(
            family.get(
                "score",
                0.0,
            )
        ),
        int(
            family.get(
                "products_count",
                0,
            )
        ),
        str(
            family.get(
                "name",
                "",
            )
        ),
    )


def normalize_family_name(
    value: str,
) -> str:
    """
    Нормализует название семейства
    для поиска дублей.
    """

    return " ".join(
        value
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def prepare_families(
    families: list[dict[str, Any]],
    *,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """
    Удаляет дубликаты и ограничивает число
    уточняющих семейств.

    Пользователь не должен видеть длинный
    технический список.
    """

    unique_families: list[
        dict[str, Any]
    ] = []

    seen_names: set[str] = set()

    sorted_families = sorted(
        families,
        key=family_quality_key,
        reverse=True,
    )

    for family in sorted_families:
        family_name = str(
            family.get(
                "name",
                "",
            )
        ).strip()

        normalized_name = normalize_family_name(
            family_name
        )

        if not normalized_name:
            continue

        if normalized_name in seen_names:
            continue

        products_count = int(
            family.get(
                "products_count",
                0,
            )
        )

        if products_count <= 0:
            continue

        seen_names.add(
            normalized_name
        )

        unique_families.append(
            family
        )

        if len(unique_families) >= limit:
            break

    return unique_families


def prepare_intent_groups(
    groups: list[Any],
    *,
    limit: int = 18,
) -> list[dict[str, Any]]:
    """
    Приводит группы старого Search Engine
    к единому техническому формату.

    На этом этапе варианты ещё не показываются
    пользователю. После этого они обязательно
    проходят через prepare_human_intents().
    """

    prepared_groups: list[
        dict[str, Any]
    ] = []

    seen_queries: set[str] = set()

    safe_limit = max(
        1,
        min(
            limit,
            50,
        ),
    )

    for group in groups:
        title = str(
            group.get(
                "title",
                "",
            )
        ).strip()

        group_query = str(
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

        normalized_group_query = " ".join(
            group_query
            .lower()
            .replace("ё", "е")
            .split()
        )

        if not title or not group_query:
            continue

        if normalized_group_query in seen_queries:
            continue

        if count <= 0:
            continue

        seen_queries.add(
            normalized_group_query
        )

        prepared_groups.append(
            {
                "title": title,
                "query": group_query,
                "count": count,
            }
        )

        if len(prepared_groups) >= safe_limit:
            break

    return prepared_groups


async def find_barcode_product(
    *,
    session: AsyncSession,
    barcode: str,
) -> SearchPipelineProduct | None:
    """
    Ищет точный товар по штрихкоду.
    """

    rows = await search_products(
        session=session,
        query=barcode,
        limit=1,
    )

    if not rows:
        return None

    product, brand, category = rows[0]

    if str(
        product.barcode or ""
    ) != barcode:
        return None

    return SearchPipelineProduct(
        product=product,
        brand=brand,
        category=category,
    )


async def run_search_pipeline(
    *,
    session: AsyncSession,
    query: str,
    intent_limit: int = 6,
    family_limit: int = 6,
    decision_candidates_limit: int = 20,
) -> SearchPipelineResult:
    """
    Единственная точка входа поиска MarkaRadar.

    Последовательность:

    1. очищает запрос;
    2. проверяет штрихкод;
    3. получает технические уточнения;
    4. превращает их в человеческие варианты;
    5. получает компактные семейства;
    6. запускает Decision Search;
    7. выбирает наиболее полезный экран.

    Главный принцип:

    Pipeline возвращает решение для пользователя,
    а не обычный список строк из базы данных.
    """

    cleaned_query = clean_pipeline_query(
        query
    )

    if not cleaned_query:
        return SearchPipelineResult(
            screen=SearchPipelineScreen.EMPTY,
            original_query=query,
            normalized_query="",
            barcode_product=None,
            intent_groups=[],
            families=[],
            decision=None,
            corrected_query=None,
            explanation=(
                "Поисковый запрос пуст."
            ),
        )

    # Точный штрихкод всегда имеет высший приоритет.
    if is_possible_barcode(
        cleaned_query
    ):
        barcode_product = (
            await find_barcode_product(
                session=session,
                barcode=cleaned_query,
            )
        )

        if barcode_product is not None:
            return SearchPipelineResult(
                screen=(
                    SearchPipelineScreen
                    .BARCODE_PRODUCT
                ),
                original_query=query,
                normalized_query=cleaned_query,
                barcode_product=barcode_product,
                intent_groups=[],
                families=[],
                decision=None,
                corrected_query=None,
                explanation=(
                    "Найден точный товар "
                    "по штрихкоду."
                ),
            )

    # Старый Search Engine временно используется
    # только как поставщик технических групп.
    #
    # Запрашиваем больше вариантов, чем покажем,
    # потому что часть из них будет удалена
    # как мусор, технические слова или дубли.
    technical_intent_limit = max(
        intent_limit * 3,
        18,
    )

    engine_result = await run_search_engine(
        session=session,
        query=cleaned_query,
        intent_limit=technical_intent_limit,
        suggestion_limit=8,
    )

    technical_intent_groups = (
        prepare_intent_groups(
            engine_result.intent_groups,
            limit=technical_intent_limit,
        )
    )

    # Пользователь никогда не должен видеть
    # технические названия из базы.
    #
    # На этом этапе:
    # - удаляются английские служебные слова;
    # - сокращаются длинные варианты;
    # - объединяются дубли;
    # - создаются понятные человеку кнопки.
    intent_groups = prepare_human_intents(
        original_query=cleaned_query,
        groups=technical_intent_groups,
        limit=intent_limit,
    )

    # Семейства являются только возможными
    # компактными фильтрами.
    raw_families = await find_product_families(
        session=session,
        query=cleaned_query,
        limit=max(
            family_limit * 2,
            10,
        ),
    )

    families = prepare_families(
        raw_families,
        limit=family_limit,
    )

    # Decision Search — главный источник результата.
    # Именно здесь учитываются оценки и доверие.
    decision = await run_decision_search(
        session=session,
        query=cleaned_query,
        candidates_limit=(
            decision_candidates_limit
        ),
        alternatives_limit=3,
        insufficient_limit=3,
        other_limit=10,
    )

    # Если найден подтверждённый лучший вариант,
    # сразу показываем решение.
    if (
        decision.has_results
        and decision.has_confirmed_choice
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=intent_groups,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найден подтверждённый вариант "
                "с достаточным уровнем доверия."
            ),
        )

    # Для конкретного запроса показываем товары
    # сразу, даже если оценок пока мало.
    query_words_count = len(
        cleaned_query.split()
    )

    if (
        decision.has_results
        and query_words_count >= 3
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=intent_groups,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос достаточно конкретный, "
                "поэтому показаны товары сразу."
            ),
        )

    # Для широкого запроса сначала предлагаем
    # несколько коротких человеческих уточнений.
    if should_use_intents(
        query=cleaned_query,
        intent_groups=intent_groups,
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.INTENTS,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=intent_groups,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос широкий. Небольшое "
                "уточнение поможет дать более "
                "полезную рекомендацию."
            ),
        )

    # Если человеческих уточнений недостаточно,
    # можно использовать компактные семейства.
    if should_use_families(
        query=cleaned_query,
        families=families,
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.FAMILIES,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=intent_groups,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найдено несколько понятных "
                "видов продукта."
            ),
        )

    # Если товары есть, но уверенного победителя нет,
    # всё равно показываем честный экран решения:
    # недостаточно данных, другие варианты и оценки.
    if decision.has_results:
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=intent_groups,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Подходящие товары найдены, "
                "но данных для уверенной "
                "рекомендации пока недостаточно."
            ),
        )

    return SearchPipelineResult(
        screen=SearchPipelineScreen.NOT_FOUND,
        original_query=query,
        normalized_query=cleaned_query,
        barcode_product=None,
        intent_groups=[],
        families=[],
        decision=None,
        corrected_query=None,
        explanation=(
            "Подходящих товаров не найдено."
        ),
    )
