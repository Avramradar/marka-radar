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
    """ Экран, который должен увидеть пользователь. Search Pipeline возвращает не сырой список товаров, а готовое решение для интерфейса. """

    EMPTY = "empty"
    BARCODE_PRODUCT = "barcode_product"
    INTENTS = "intents"
    FAMILIES = "families"
    DECISION = "decision"
    NOT_FOUND = "not_found"


@dataclass(slots=True)
class SearchPipelineProduct:
    """ Конкретный товар, найденный по штрихкоду. """

    product: Any
    brand: Any
    category: Any


@dataclass(slots=True)
class SearchPipelineResult:
    """ Полный результат поискового конвейера. Обработчики Telegram должны смотреть только на поле screen. """

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


# ------------------------------------------------------------------
# ЭВРИСТИКА "ШИРОКИЙ ИЛИ КОНКРЕТНЫЙ ЗАПРОС"
# ------------------------------------------------------------------
#
# Раньше любое выражение из 1-2 слов считалось широким.
# Поэтому:
#
# "Сметана Чабан"
# "Кофе Poetti"
#
# ошибочно уходили в FAMILIES/INTENTS.
#
# Теперь:
#
# "сметана" -> широкий
# "кофе" -> широкий
# "растворимый кофе" -> широкий
# "молотый кофе" -> широкий
#
# но:
#
# "сметана чабан" -> конкретный
# "кофе poetti" -> конкретный
# "poetti leggenda" -> конкретный
# "простоквашино 15%" -> конкретный
# ------------------------------------------------------------------

BROAD_PRODUCT_WORDS = {
    "кофе",
    "чай",
    "молоко",
    "сметана",
    "кефир",
    "йогурт",
    "сыр",
    "масло",
    "вода",
    "сок",
    "пицца",
    "сельдь",
    "рыба",
    "мясо",
    "колбаса",
    "макароны",
    "рис",
    "гречка",
    "мука",
    "сахар",
    "соль",
    "хлеб",
    "печенье",
    "шоколад",
    "конфеты",
    "мороженое",
    "пельмени",
    "вареники",
    "творог",
    "сливки",
    "яйца",
    "яйцо",
}

BROAD_MODIFIER_WORDS = {
    "растворимый",
    "растворимое",
    "молотый",
    "молотое",
    "зерновой",
    "зерновое",
    "зернах",
    "зёрнах",
    "черный",
    "чёрный",
    "зеленый",
    "зелёный",
    "питьевой",
    "питьевое",
    "пастеризованный",
    "пастеризованное",
    "ультрапастеризованный",
    "ультрапастеризованное",
    "безлактозный",
    "безлактозное",
    "замороженный",
    "замороженная",
    "замороженное",
    "слабосоленый",
    "слабосолёный",
    "слабосоленая",
    "слабосолёная",
    "филе",
    "минеральная",
    "питьевая",
    "газированная",
    "негазированная",
    "сливочное",
    "сливочный",
    "твердый",
    "твёрдый",
    "мягкий",
}


def clean_pipeline_query( query: str, ) -> str:
    """ Безопасная первичная очистка запроса. """

    return " ".join(
        str(query or "")
        .strip()
        .split()
    )


def normalize_pipeline_token( value: str, ) -> str:
    """ Лёгкая нормализация слова для эвристики широкого запроса. """

    return (
        str(value or "")
        .strip()
        .lower()
        .replace(
            "ё",
            "е",
        )
        .strip(
            ".,:;!?()[]{}\"'«»"
        )
    )


def is_possible_barcode( query: str, ) -> bool:
    """ Проверяет, похож ли запрос на штрихкод. Поддерживаем длину 8-14 цифр. """

    cleaned = clean_pipeline_query(
        query
    )

    return (
        cleaned.isdigit()
        and 8 <= len(cleaned) <= 14
    )


def is_broad_query( query: str, ) -> bool:
    """ Определяет, действительно ли запрос широкий. Одно слово: кофе сметана молоко считаем широким. Два слова считаем широкими только тогда, когда это сочетание типа продукта и общей характеристики: растворимый кофе молотый кофе черный чай питьевое молоко Брендовые/конкретные запросы: Сметана Чабан Кофе Poetti Poetti Leggenda широкими НЕ считаются и сразу идут к конкретным товарам через Decision Search. """

    cleaned = clean_pipeline_query(
        query
    )

    if not cleaned:
        return False

    tokens = [
        normalize_pipeline_token(
            token
        )
        for token in cleaned.split()
        if normalize_pipeline_token(
            token
        )
    ]

    if not tokens:
        return False

    if len(tokens) == 1:
        return True

    if len(tokens) > 2:
        return False

    first, second = tokens

    product_words = {
        word.replace(
            "ё",
            "е",
        )
        for word in BROAD_PRODUCT_WORDS
    }

    modifier_words = {
        word.replace(
            "ё",
            "е",
        )
        for word in BROAD_MODIFIER_WORDS
    }

    # Тип + характеристика:
    # "кофе растворимый"
    if (
        first in product_words
        and second in modifier_words
    ):
        return True

    # Характеристика + тип:
    # "растворимый кофе"
    if (
        second in product_words
        and first in modifier_words
    ):
        return True

    # Два общих типа продукта тоже считаем
    # широким/неоднозначным запросом.
    if (
        first in product_words
        and second in product_words
    ):
        return True

    # Всё остальное из двух слов считаем
    # конкретным запросом.
    return False


def normalize_pipeline_phrase( value: object, ) -> str:
    """ Нормализует целую фразу для точного сравнения. Используется, в частности, чтобы отличать точное название бренда от широкого однословного запроса. """

    return " ".join(
        normalize_pipeline_token(token)
        for token in clean_pipeline_query(
            str(value or "")
        ).split()
        if normalize_pipeline_token(token)
    )


def decision_has_exact_brand_match( *, query: str, decision: DecisionSearchResult, ) -> bool:
    """ Возвращает True, если среди найденных Decision Search есть товар с брендом, который ТОЧНО совпадает с запросом. Это принципиально важно для запросов вроде "Доброфлот": одно слово само по себе ещё не означает широкий тип товара. Если это точное название бренда, пользователь уже сделал важное уточнение и должен сразу увидеть товары бренда. """

    normalized_query = normalize_pipeline_phrase(
        query
    )

    if not normalized_query:
        return False

    items: list[Any] = []

    best_choice = getattr(
        decision,
        "best_choice",
        None,
    )

    if best_choice is not None:
        items.append(
            best_choice
        )

    for attribute_name in (
        "alternatives",
        "insufficient_data",
        "other_products",
    ):
        attribute_value = getattr(
            decision,
            attribute_name,
            None,
        )

        if attribute_value:
            items.extend(
                list(attribute_value)
            )

    for item in items:
        brand_name = getattr(
            item,
            "brand_name",
            None,
        )

        if not brand_name:
            brand = getattr(
                item,
                "brand",
                None,
            )
            brand_name = getattr(
                brand,
                "name",
                "",
            )

        if (
            normalize_pipeline_phrase(
                brand_name
            )
            == normalized_query
        ):
            return True

    return False


def should_show_facets( *, query: str, facet_options: list[dict[str, Any]], decision: DecisionSearchResult, ) -> bool:
    """ Решает, нужно ли показывать фасеты. Они нужны только: - для действительно широкого запроса; - когда есть минимум два полезных варианта; - когда уже нет подтверждённого лидера. """

    if not is_broad_query(
        query
    ):
        return False

    if len(
        facet_options
    ) < 2:
        return False

    if decision.has_confirmed_choice:
        return False

    return True


def family_quality_key( family: dict[str, Any], ) -> tuple[
    float,
    int,
    str,
]:
    """ Ключ сортировки семейств. """

    return (
        float(
            family.get(
                "score",
                0.0,
            )
            or 0.0
        ),
        int(
            family.get(
                "products_count",
                0,
            )
            or 0
        ),
        str(
            family.get(
                "name",
                "",
            )
        ),
    )


def normalize_family_name( value: str, ) -> str:
    """ Нормализует название семейства для удаления дублей. """

    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace(
            "ё",
            "е",
        )
        .split()
    )


def prepare_families( families: list[dict[str, Any]], *, limit: int = 6, ) -> list[dict[str, Any]]:
    """ Удаляет: - пустые семейства; - семейства без товаров; - дубли; - слишком длинную выдачу. """

    safe_limit = max(
        1,
        min(
            int(limit),
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
            or ""
        ).strip()

        normalized_name = (
            normalize_family_name(
                family_name
            )
        )

        products_count = int(
            family.get(
                "products_count",
                0,
            )
            or 0
        )

        if not normalized_name:
            continue

        if products_count <= 0:
            continue

        if (
            normalized_name
            in seen_names
        ):
            continue

        seen_names.add(
            normalized_name
        )

        prepared.append(
            family
        )

        if (
            len(prepared)
            >= safe_limit
        ):
            break

    return prepared


def should_show_families( *, query: str, families: list[dict[str, Any]], facet_options: list[dict[str, Any]], decision: DecisionSearchResult, ) -> bool:
    """ Семейства — только резервный механизм. Главное изменение: конкретный брендовый запрос вроде "Сметана Чабан" никогда не должен уходить в FAMILIES только из-за того, что в нём два слова. """

    if facet_options:
        return False

    if not is_broad_query(
        query
    ):
        return False

    if len(
        families
    ) < 2:
        return False

    if decision.has_confirmed_choice:
        return False

    return True


async def find_barcode_product( *, session: AsyncSession, barcode: str, ) -> SearchPipelineProduct | None:
    """ Ищет точный товар по штрихкоду только в локальной базе MarkaRadar. Внешнее обогащение OpenFoodFacts выполняется уровнем выше — в search.py. """

    rows = await search_products(
        session=session,
        query=barcode,
        limit=1,
    )

    if not rows:
        return None

    product, brand, category = (
        rows[0]
    )

    stored_barcode = str(
        product.barcode or ""
    ).strip()

    if stored_barcode != barcode:
        return None

    return SearchPipelineProduct(
        product=product,
        brand=brand,
        category=category,
    )


def build_empty_result( *, original_query: str, ) -> SearchPipelineResult:
    """ Формирует результат пустого запроса. """

    return SearchPipelineResult(
        screen=(
            SearchPipelineScreen.EMPTY
        ),
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


def build_not_found_result( *, original_query: str, normalized_query: str, explanation: str, ) -> SearchPipelineResult:
    """ Формирует результат отсутствия товаров. """

    return SearchPipelineResult(
        screen=(
            SearchPipelineScreen.NOT_FOUND
        ),
        original_query=original_query,
        normalized_query=normalized_query,
        barcode_product=None,
        intent_groups=[],
        families=[],
        decision=None,
        corrected_query=None,
        explanation=explanation,
    )


def build_barcode_result( *, original_query: str, normalized_query: str, barcode_product: SearchPipelineProduct, ) -> SearchPipelineResult:
    """ Формирует результат точного поиска по штрихкоду. """

    return SearchPipelineResult(
        screen=(
            SearchPipelineScreen
            .BARCODE_PRODUCT
        ),
        original_query=original_query,
        normalized_query=normalized_query,
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


async def run_search_pipeline( *, session: AsyncSession, query: str, intent_limit: int = 6, family_limit: int = 6, decision_candidates_limit: int = 20, allow_refinements: bool = True, ) -> SearchPipelineResult:
    """ Единый Search Pipeline MarkaRadar. Последовательность: 1. очищаем запрос; 2. проверяем локальный штрихкод; 3. запускаем Decision Search; 4. для действительно широкого запроса при необходимости запускаем Facet Engine; 5. если фасетов нет — Family Search; 6. конкретные брендовые запросы сразу переходят к Decision; 7. выбираем пользовательский экран. OpenFoodFacts/METRO здесь НЕ вызываются. Внешнее обогащение находится уровнем выше в search.py. """

    cleaned_query = (
        clean_pipeline_query(
            query
        )
    )

    if not cleaned_query:
        return build_empty_result(
            original_query=query,
        )

    #
    # 1. BARCODE
    #

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
            return build_barcode_result(
                original_query=query,
                normalized_query=(
                    cleaned_query
                ),
                barcode_product=(
                    barcode_product
                ),
            )

    #
    # 2. DECISION SEARCH
    #

    safe_decision_limit = max(
        5,
        min(
            int(
                decision_candidates_limit
            ),
            50,
        ),
    )

    decision = await run_decision_search(
        session=session,
        query=cleaned_query,
        candidates_limit=(
            safe_decision_limit
        ),
        alternatives_limit=3,
        insufficient_limit=3,
        other_limit=8,
    )

    #
    # 3. ПОСЛЕ УТОЧНЕНИЯ
    #

    if not allow_refinements:
        if decision.has_results:
            return SearchPipelineResult(
                screen=(
                    SearchPipelineScreen
                    .DECISION
                ),
                original_query=query,
                normalized_query=(
                    cleaned_query
                ),
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
            normalized_query=(
                cleaned_query
            ),
            explanation=(
                "По выбранному уточнению "
                "подходящих товаров не найдено."
            ),
        )

    #
    # 4. ПОДТВЕРЖДЁННЫЙ ЛИДЕР
    #

    if (
        decision.has_results
        and decision.has_confirmed_choice
    ):
        return SearchPipelineResult(
            screen=(
                SearchPipelineScreen
                .DECISION
            ),
            original_query=query,
            normalized_query=(
                cleaned_query
            ),
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

    #
    # 5. КОНКРЕТНЫЙ ЗАПРОС -> СРАЗУ DECISION
    #
    # Это главное исправление для:
    #
    # Сметана Чабан
    # Кофе Poetti
    # Poetti Leggenda
    #
    # Если Decision Search уже нашёл товары,
    # не отправляем пользователя в семейства.
    #

    query_is_broad = (
        is_broad_query(
            cleaned_query
        )
        and not decision_has_exact_brand_match(
            query=cleaned_query,
            decision=decision,
        )
    )

    if (
        decision.has_results
        and not query_is_broad
    ):
        return SearchPipelineResult(
            screen=(
                SearchPipelineScreen
                .DECISION
            ),
            original_query=query,
            normalized_query=(
                cleaned_query
            ),
            barcode_product=None,
            intent_groups=[],
            families=[],
            decision=decision,
            corrected_query=None,
            explanation=(
                "Найдены товары, наиболее "
                "точно соответствующие запросу."
            ),
        )

    #
    # 6. FACETS
    #

    facet_options: list[
        dict[str, Any]
    ] = []

    families: list[
        dict[str, Any]
    ] = []

    if query_is_broad:
        facet_candidates_limit = max(
            safe_decision_limit,
            20,
        )

        facet_candidates_limit = min(
            facet_candidates_limit,
            30,
        )

        facet_result = (
            await build_product_facets(
                session=session,
                query=cleaned_query,
                candidates_limit=(
                    facet_candidates_limit
                ),
                group_limit=1,
                option_limit=5,
            )
        )

        facet_options = (
            flatten_facet_options(
                facet_result,
                limit=intent_limit,
            )
        )

    #
    # 7. FAMILY SEARCH
    #

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

    #
    # 8. ПОКАЗАТЬ FACETS
    #

    if should_show_facets(
        query=cleaned_query,
        facet_options=facet_options,
        decision=decision,
    ):
        return SearchPipelineResult(
            screen=(
                SearchPipelineScreen
                .INTENTS
            ),
            original_query=query,
            normalized_query=(
                cleaned_query
            ),
            barcode_product=None,
            intent_groups=facet_options,
            families=[],
            decision=decision,
            corrected_query=None,
            explanation=(
                "Запрос широкий. "
                "Выберите следующий параметр, "
                "чтобы точнее сравнить товары."
            ),
        )

    #
    # 9. ПОКАЗАТЬ FAMILIES
    #

    if should_show_families(
        query=cleaned_query,
        families=families,
        facet_options=facet_options,
        decision=decision,
    ):
        return SearchPipelineResult(
            screen=(
                SearchPipelineScreen
                .FAMILIES
            ),
            original_query=query,
            normalized_query=(
                cleaned_query
            ),
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

    #
    # 10. ПОКАЗАТЬ DECISION
    #

    if decision.has_results:
        return SearchPipelineResult(
            screen=(
                SearchPipelineScreen
                .DECISION
            ),
            original_query=query,
            normalized_query=(
                cleaned_query
            ),
            barcode_product=None,
            intent_groups=(
                facet_options
            ),
            families=families,
            decision=decision,
            corrected_query=None,
            explanation=(
                "Подходящие товары найдены, "
                "но данных для уверенной "
                "рекомендации пока недостаточно."
            ),
        )

    #
    # 11. NOT FOUND
    #

    return build_not_found_result(
        original_query=query,
        normalized_query=(
            cleaned_query
        ),
        explanation=(
            "Подходящих товаров не найдено."
        ),
    )
