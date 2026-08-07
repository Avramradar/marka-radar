import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.lenta_adapter import (
    import_lenta_search,
)
from app.integrations.openfoodfacts_search_adapter import (
    import_openfoodfacts_search,
)
from app.search.decision_search import (
    DecisionSearchResult,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ExternalCatalogSearchResult:
    attempted: bool
    provider: str | None
    imported_count: int


def _normalize( value: object, ) -> str:
    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def _tokens( value: str, ) -> set[str]:
    return {
        token
        for token in _normalize(
            value
        ).split()
        if len(token) >= 2
    }


def local_decision_is_good_enough( *, query: str, decision: DecisionSearchResult | None, ) -> bool:
    """ Если локальная база уже имеет конкретную карточку с фото и хорошим совпадением, внешний каталог не вызываем. """

    if (
        decision is None
        or not decision.has_results
    ):
        return False

    query_tokens = _tokens(
        query
    )

    if not query_tokens:
        return True

    items = []

    if decision.best_choice is not None:
        items.append(
            decision.best_choice
        )

    items.extend(
        decision.alternatives
    )

    items.extend(
        decision.insufficient_data
    )

    items.extend(
        decision.other_products[:5]
    )

    for item in items:
        product = item.product
        brand = item.brand

        search_text = _normalize(
            f"{getattr(product, 'name', '')} "
            f"{getattr(brand, 'name', '')}"
        )

        matched = sum(
            1
            for token in query_tokens
            if token in search_text
        )

        coverage = (
            matched
            / max(
                len(query_tokens),
                1,
            )
        )

        if (
            coverage >= 0.75
            and bool(
                getattr(
                    product,
                    "image_url",
                    None,
                )
            )
        ):
            return True

    return False


def should_try_external_catalog( *, query: str, decision: DecisionSearchResult | None, ) -> bool:
    cleaned = " ".join(
        str(query or "")
        .strip()
        .split()
    )

    if (
        not cleaned
        or cleaned.isdigit()
    ):
        return False

    # Один широкий термин ("кофе") пока не отправляем
    # во внешний полнотекстовый поиск, чтобы не засорять
    # базу десятками случайных карточек.
    if len(
        cleaned.split()
    ) < 2:
        return False

    return not local_decision_is_good_enough(
        query=cleaned,
        decision=decision,
    )


async def enrich_catalog_for_query( *, session: AsyncSession, query: str, decision: DecisionSearchResult | None, limit: int = 8, ) -> ExternalCatalogSearchResult:
    """ Обогащает каталог по обычному текстовому запросу. Порядок источников: 1. Open Food Facts Search Основной внешний источник для фото и структурированных товарных данных. 2. Lenta Parser Необязательный fallback. Если Лента блокирует GitHub Actions (401/403/429), поиск MarkaRadar продолжает работать. После успешного импорта выполняется один commit. Search Pipeline затем должен быть запущен повторно, чтобы увидеть обновлённые карточки. """

    if not should_try_external_catalog(
        query=query,
        decision=decision,
    ):
        return ExternalCatalogSearchResult(
            attempted=False,
            provider=None,
            imported_count=0,
        )

    safe_limit = max(
        1,
        min(
            int(limit),
            12,
        ),
    )

    attempted = True

    #
    # 1. OPEN FOOD FACTS SEARCH
    #

    try:
        off_result = await import_openfoodfacts_search(
            session=session,
            query=query,
            limit=safe_limit,
            commit=False,
        )

    except Exception:
        logger.exception(
            "OpenFoodFacts catalog enrichment "
            "failed for query=%r",
            query,
        )

        off_result = None

    if (
        off_result is not None
        and off_result.imported > 0
    ):
        await session.commit()

        logger.info(
            "External catalog enrichment: "
            "provider=openfoodfacts_search "
            "query=%r found=%s imported=%s "
            "with_images=%s",
            query,
            off_result.found,
            off_result.imported,
            off_result.with_images,
        )

        return ExternalCatalogSearchResult(
            attempted=attempted,
            provider="openfoodfacts_search",
            imported_count=off_result.imported,
        )

    #
    # 2. LENTA FALLBACK
    #

    try:
        lenta_result = await import_lenta_search(
            session=session,
            query=query,
            limit=safe_limit,
            commit=False,
        )

    except Exception:
        logger.exception(
            "Lenta catalog enrichment "
            "failed for query=%r",
            query,
        )

        return ExternalCatalogSearchResult(
            attempted=attempted,
            provider="lenta",
            imported_count=0,
        )

    if lenta_result.imported <= 0:
        logger.info(
            "External catalog enrichment: "
            "no useful external products "
            "for query=%r",
            query,
        )

        return ExternalCatalogSearchResult(
            attempted=attempted,
            provider=None,
            imported_count=0,
        )

    await session.commit()

    logger.info(
        "External catalog enrichment: "
        "provider=lenta query=%r "
        "found=%s imported=%s",
        query,
        lenta_result.found,
        lenta_result.imported,
    )

    return ExternalCatalogSearchResult(
        attempted=attempted,
        provider="lenta",
        imported_count=lenta_result.imported,
    )
