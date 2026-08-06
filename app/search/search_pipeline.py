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

    Search Pipeline возвращает не сырой список
    товаров, а готовое решение для интерфейса.
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
    Полный результат поискового конвейера.

    Обработчики Telegram должны смотреть
    на поле screen и показывать выбранный экран.
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
    """

    return " ".join(
        query.strip().split()
    )


def is_possible_barcode(
    query: str,
) -> bool:
    """
    Проверяет, похож ли запрос на штрихкод.

    Поддерживаются распространённые длины:
    EAN-8, UPC-A, EAN-13 и внутренние коды.
    """

    return (
        query.isdigit()
        and 8 <= len(query) <= 14
    )


def is_broad_query(
    query: str,
) -> bool:
    """
    Определяет широкий запрос.

    Примеры:

    молоко
    кофе
    растворимый кофе
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
    Решает, нужно ли показывать фасеты.

    Фасеты используются для широкого запроса,
    когда нет подтверждённого лидера.
    """

    if not is_broad_query(
        query
    ):
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
    Формирует ключ сортировки семейств.
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
    для удаления дублей.
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

    Семейства остаются только резервным
    механизмом для категорий, которые пока
    не поддерживаются Facet Engine.
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
    Решает, нужно ли показывать семейства.

    Семейства используются только тогда,
    когда Facet Engine не построил понятных
    пользовательских уточнений.
    """

    if facet_options:
        return False

    if not is_broad_query(
        query
    ):
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


def build_empty_result(
    *,
    original_query: str,
) -> SearchPipelineResult:
    """
    Создаёт результат для пустого запроса.
    """

    return SearchPipelineResult(
        screen=SearchPipelineScreen.EMPTY,
        original_query=original_query,
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


def build_not_found_result(
    *,
    original_query: str,
    normalized_query: str,
    explanation: str,
) -> SearchPipelineResult:
    """
    Создаёт результат отсутствия товаров.
    """

    return SearchPipelineResult(
        screen=SearchPipelineScreen.NOT_FOUND,
        original_query=original_query,
        normalized_query=normalized_query,
        barcode_product=None,
        intent_groups=[],
        families=[],
        decision=None,
        corrected_query=None,
        explanation=explanation,
    )


async def run_search_pipeline(
    *,
    session: AsyncSession,
    query: str,
    intent_limit: int = 6,
    family_limit: int = 6,
    decision_candidates_limit: int = 20,
    allow_refinements: bool = True,
) -> SearchPipelineResult:
    """
    Единственная точка входа поиска MarkaRadar.

    Порядок:

    1. очистка запроса;
    2. точный поиск по штрихкоду;
    3. Decision Search;
    4. Facet Engine для широкого запроса;
    5. семейства только как резерв;
    6. выбор пользовательского экрана.

    Оптимизация:

    - семейства не ищутся, если найдены фасеты;
    - фасеты анализируют не более 30 кандидатов;
    - после отключения уточнений не запускаются
      ни Facet Engine, ни Family Search;
    - тяжёлые этапы не выполняются без причины.
    """

    cleaned_query = clean_pipeline_query(
        query
    )

    if not cleaned_query:
        return build_empty_result(
            original_query=query,
        )

    # Штрихкод имеет высший приоритет.
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

    safe_decision_limit = max(
        5,
        min(
            decision_candidates_limit,
            50,
        ),
    )

    # Decision Search — главный источник
    # товаров, оценок и уровня доверия.
    decision = await run_decision_search(
        session=session,
        query=cleaned_query,
        candidates_limit=safe_decision_limit,
        alternatives_limit=3,
        insufficient_limit=3,
        other_limit=8,
    )

    # После выбранного пользователем уточнения
    # можно сразу переходить к результатам.
    #
    # В этом режиме Facet Engine и Family Search
    # вообще не запускаются.
    if not allow_refinements:
        if decision.has_results:
            return SearchPipelineResult(
                screen=SearchPipelineScreen.DECISION,
                original_query=query,
                normalized_query=cleaned_query,
                barcode_product=None,
                intent_groups=[],
                families=[],
                decision=decision,
                corrected_query=None,
                explanation=(
                    "Уточнение применено. "
                    "MarkaRadar показывает "
                    "наиболее подходящие товары "
                    "с учётом оценок и доверия."
                ),
            )

        return build_not_found_result(
            original_query=query,
            normalized_query=cleaned_query,
            explanation=(
                "По выбранному уточнению "
                "подходящих товаров не найдено."
            ),
        )

    facet_options: list[
        dict[str, Any]
    ] = []

    families: list[
        dict[str, Any]
    ] = []

    # Подтверждённый лидер можно показать сразу.
    # Не выполняем дополнительный анализ фасетов.
    if (
        decision.has_results
        and decision.has_confirmed_choice
    ):
        return SearchPipelineResult(
            screen=SearchPipelineScreen.DECISION,
            original_query=query,
            normalized_query=cleaned_query,
            barcode_product=None,
            intent_groups=[],
            families=[],
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найден подтверждённый вариант "
                "с достаточным уровнем доверия."
            ),
        )

    # Для конкретного запроса дополнительные
    # уровни уточнения чаще всего не нужны.
    #
    # Исключение: запрос из двух слов может быть
    # частью рекурсивного фасетного сценария,
    # например «молоко ультрапастеризованное».
    query_is_broad = is_broad_query(
        cleaned_query
    )

    # Facet Engine запускается только тогда,
    # когда запрос ещё может требовать уточнения.
    if query_is_broad:
        facet_candidates_limit = max(
            safe_decision_limit,
            20,
        )

        facet_candidates_limit = min(
            facet_candidates_limit,
            30,
        )

        facet_result = await build_product_facets(
            session=session,
            query=cleaned_query,
            candidates_limit=(
                facet_candidates_limit
            ),
            group_limit=1,
            option_limit=5,
        )

        facet_options = flatten_facet_options(
            facet_result,
            limit=intent_limit,
        )

    # Семейства выполняют отдельный поиск,
    # поэтому запускаем их только тогда,
    # когда Facet Engine ничего не дал.
    if (
        query_is_broad
        and not facet_options
    ):
        raw_families = (
            await find_product_families(
                session=session,
                query=cleaned_query,
                limit=max(
                    family_limit * 2,
                    10,
                ),
            )
        )

        families = prepare_families(
            raw_families,
            limit=family_limit,
        )

    # Для широкого запроса без лидера
    # показываем один следующий уровень фасетов.
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
            families=[],
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос широкий. Выберите "
                "следующий параметр, чтобы "
                "точнее сравнить товары."
            ),
        )

    # Семейства используются только
    # для пока неподдерживаемых категорий.
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

    # Если товары найдены, показываем решение,
    # даже когда оценок пока недостаточно.
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

    return build_not_found_result(
        original_query=query,
        normalized_query=cleaned_query,
        explanation=(
            "Подходящих товаров не найдено."
        ),
    )
