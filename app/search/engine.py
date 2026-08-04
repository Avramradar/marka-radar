from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.search.intent_groups import (
    IntentGroup,
    get_intent_groups,
)
from app.search.suggestions import (
    get_search_suggestions,
)


class SearchMode(StrEnum):
    INTENTS = "intents"
    PRODUCTS = "products"
    EMPTY = "empty"


@dataclass
class SearchEngineResult:
    mode: SearchMode
    query: str
    intent_groups: list[IntentGroup]
    product_suggestions: list[dict[str, Any]]


async def run_search_engine(
    session: AsyncSession,
    query: str,
    *,
    intent_limit: int = 8,
    suggestion_limit: int = 8,
) -> SearchEngineResult:
    """
    Главная точка входа для поиска MarkaRadar.

    Сначала пытается построить уточняющие группы.
    Если групп недостаточно, возвращает товары.
    Если ничего нет, возвращает пустой результат.
    """

    cleaned_query = query.strip()

    if not cleaned_query:
        return SearchEngineResult(
            mode=SearchMode.EMPTY,
            query="",
            intent_groups=[],
            product_suggestions=[],
        )

    intent_groups = await get_intent_groups(
        session=session,
        query=cleaned_query,
        limit=intent_limit,
        products_limit=80,
    )

    if len(intent_groups) >= 3:
        return SearchEngineResult(
            mode=SearchMode.INTENTS,
            query=cleaned_query,
            intent_groups=intent_groups,
            product_suggestions=[],
        )

    product_suggestions = await get_search_suggestions(
        session=session,
        query=cleaned_query,
        limit=suggestion_limit,
    )

    if product_suggestions:
        return SearchEngineResult(
            mode=SearchMode.PRODUCTS,
            query=cleaned_query,
            intent_groups=[],
            product_suggestions=product_suggestions,
        )

    return SearchEngineResult(
        mode=SearchMode.EMPTY,
        query=cleaned_query,
        intent_groups=[],
        product_suggestions=[],
    )
