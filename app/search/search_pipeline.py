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
from app.search.facet_engine import (
    build_product_facets,
    flatten_facet_options,
)
from app.search.family_search import (
    find_product_families,
)


class SearchPipelineScreen(StrEnum):
    """
    Экран, который должен увидеть пользователь.
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
    """

    product: Any
    brand: Any
    category: Any


@dataclass(slots=True)
class SearchPipelineResult:
    """
    Результат единого поискового конвейера.
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
    Выполняет первичную очистку запроса.
    """

    return " ".join(
        query.strip().split()
    )


def is_possible_barcode(
    query: str,
) -> bool:
    """
    Проверяет, похож ли запрос на штрихкод.
    """

    return (
        query.isdigit()
        and 8 <= len(query) <= 14
    )


def is_broad_query(
    query: str,
) -> bool:
    """
    Широкий запрос обычно состоит
    из одного или двух слов.

    Примеры:
    молоко
    растворимый кофе
    сельдь
    """

    return len(
        query.split()
    ) <= 2


def should_show_facets(
    *,
    query: str,
    facet_options: list[dict[str, Any]],
    decision: DecisionSearchResult,
) -> bool:
    """
    Решает, нужно ли сначала показать уточнения.

    Фасеты показываются только для широкого запроса,
    если нет подтверждённого лучшего выбора.
    """

    if not is_broad_query(query):
        return False

    if len(facet_options) < 2:
        return False

    if decision.has_confirmed_choice:
        return False

    return True


def family_quality_key(
    family: dict[str, Any],
) -> tuple[float, int, str]:
    """
    Сортирует семейства по качеству совпадения.
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
    Нормализует семейство для удаления дублей.
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
    Удаляет пустые и повторяющиеся семейства.

    Семейства временно остаются резервным способом
    уточнения для категорий без Facet Engine.
    """

    safe_limit = max(
        1,
        min(
            limit,
            8,
        ),
    )

    prepared: list[
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

        products_count = int(
            family.get(
                "products_count",
                0,
            )
        )

        if not normalized_name:
            continue

        if products_count <= 0:
            continue

        if normalized_name in seen_names:
            continue

        seen_names.add(
            normalized_name
        )

        prepared.append(
            family
        )

        if len(prepared) >= safe_limit:
            break

    return prepared


def should_show_families(
    *,
    query: str,
    families: list[dict[str, Any]],
    facet_options: list[dict[str, Any]],
    decision: DecisionSearchResult,
) -> bool:
    """
    Семейства используются только как резерв.

    Если Facet Engine уже построил понятные кнопки,
    семейства не показываются.
    """

    if facet_options:
        return False

    if not is_broad_query(query):
        return False

    if len(families) < 2:
        return False

    if decision.has_confirmed_choice:
        return False

    return True


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

    Порядок:

    1. очистка запроса;
    2. точный поиск по штрихкоду;
    3. поиск и оценка товарных кандидатов;
    4. построение управляемых фасетов;
    5. резервное получение семейств;
    6. выбор наиболее полезного экрана.

    Старые автоматически сгенерированные
    intent-группы здесь больше не используются.
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

    # Штрихкод всегда имеет высший приоритет.
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

    # Decision Search ищет кандидатов
    # и оценивает их с помощью Trust Engine.
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

    # Facet Engine строит только разрешённые,
    # понятные человеку уточнения.
    facet_result = await build_product_facets(
        session=session,
        query=cleaned_query,
        candidates_limit=max(
            decision_candidates_limit,
            50,
        ),
        group_limit=2,
        option_limit=5,
    )

    facet_options = flatten_facet_options(
        facet_result,
        limit=intent_limit,
    )

    # Семейства остаются только резервом
    # для продуктов, которых ещё нет
    # в контролируемой схеме Facet Engine.
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

    # Конкретный запрос сразу ведёт к товарам.
    if (
        decision.has_results
        and not is_broad_query(cleaned_query)
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=facet_options,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос достаточно конкретный. "
                "MarkaRadar сразу показывает "
                "подходящие товары и их оценки."
            ),
        )

    # Если есть подтверждённый лидер,
    # не заставляем пользователя уточнять запрос.
    if (
        decision.has_results
        and decision.has_confirmed_choice
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=facet_options,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найден подтверждённый вариант "
                "с достаточным уровнем доверия."
            ),
        )

    # Для широкого запроса без лидера
    # показываем управляемые фасеты.
    if should_show_facets(
        query=cleaned_query,
        facet_options=facet_options,
        decision=decision,
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.INTENTS,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=facet_options,
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос широкий. Выберите "
                "понятное уточнение, чтобы "
                "сравнить подходящие товары."
            ),
        )

    # Резерв для пока неподдерживаемых категорий.
    if should_show_families(
        query=cleaned_query,
        families=families,
        facet_options=facet_options,
        decision=decision,
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.FAMILIES,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=[],
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найдено несколько видов "
                "продукта для уточнения."
            ),
        )

    # Если товары найдены, показываем честный
    # экран решения даже при недостатке оценок.
    if decision.has_results:
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=facet_options,
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
