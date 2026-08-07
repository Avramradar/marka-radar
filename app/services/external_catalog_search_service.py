import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.lenta_adapter import import_lenta_search
from app.search.decision_search import DecisionSearchResult


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExternalCatalogSearchResult:
    attempted: bool
    provider: str | None
    imported_count: int


def _normalize(value: object) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in _normalize(value).split()
        if len(token) >= 2
    }


def local_decision_is_good_enough( *, query: str, decision: DecisionSearchResult | None, ) -> bool:
    """ Если локальная база уже имеет конкретную карточку с фото и хорошим совпадением по словам запроса, внешний ритейл не вызываем. """

    if decision is None or not decision.has_results:
        return False

    query_tokens = _tokens(query)
    if not query_tokens:
        return True

    items = []
    if decision.best_choice is not None:
        items.append(decision.best_choice)
    items.extend(decision.alternatives)
    items.extend(decision.insufficient_data)
    items.extend(decision.other_products[:5])

    for item in items:
        product = item.product
        brand = item.brand

        search_text = _normalize(
            f"{getattr(product, 'name', '')} {getattr(brand, 'name', '')}"
        )
        matched = sum(1 for token in query_tokens if token in search_text)
        coverage = matched / max(len(query_tokens), 1)

        if coverage >= 0.75 and bool(getattr(product, "image_url", None)):
            return True

    return False


def should_try_external_catalog( *, query: str, decision: DecisionSearchResult | None, ) -> bool:
    cleaned = " ".join(str(query or "").strip().split())
    if not cleaned or cleaned.isdigit():
        return False

    # Одно широкое слово вроде "кофе" не должно каждый раз тянуть сотни
    # внешних карточек. Внешний каталог включаем для конкретных запросов.
    if len(cleaned.split()) < 2:
        return False

    return not local_decision_is_good_enough(query=cleaned, decision=decision)


async def enrich_catalog_for_query( *, session: AsyncSession, query: str, decision: DecisionSearchResult | None, limit: int = 8, ) -> ExternalCatalogSearchResult:
    if not should_try_external_catalog(query=query, decision=decision):
        return ExternalCatalogSearchResult(
            attempted=False,
            provider=None,
            imported_count=0,
        )

    try:
        result = await import_lenta_search(
            session=session,
            query=query,
            limit=limit,
            commit=False,
        )
    except Exception:
        logger.exception("Lenta catalog enrichment failed for query=%r", query)
        await session.rollback()
        return ExternalCatalogSearchResult(
            attempted=True,
            provider="lenta",
            imported_count=0,
        )

    if result.imported <= 0:
        await session.rollback()
        return ExternalCatalogSearchResult(
            attempted=True,
            provider="lenta",
            imported_count=0,
        )

    await session.commit()

    logger.info(
        "External catalog enrichment: provider=lenta query=%r found=%s imported=%s",
        query,
        result.found,
        result.imported,
    )

    return ExternalCatalogSearchResult(
        attempted=True,
        provider="lenta",
        imported_count=result.imported,
  )
