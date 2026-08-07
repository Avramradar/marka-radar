
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.integrations.openfoodfacts_client import (
    OPENFOODFACTS_BASE_URL,
    OPENFOODFACTS_USER_AGENT,
    OpenFoodFactsProduct,
    build_normalized_product,
)


logger = logging.getLogger(__name__)


REQUEST_TIMEOUT_SECONDS = 15
CACHE_TTL_SECONDS = 15 * 60

# Open Food Facts ограничивает поисковые запросы.
# Поэтому:
# - не используем этот клиент для search-as-you-type;
# - кэшируем ответы;
# - один пользовательский запрос = максимум один HTTP search.
MAX_RESULTS = 12

SEARCH_URL = (
    f"{OPENFOODFACTS_BASE_URL}"
    "/cgi/search.pl"
)


@dataclass( slots=True, frozen=True, )
class OpenFoodFactsSearchResult:
    """ Один результат полнотекстового поиска OFF. product: Нормализованный товар, совместимый с существующим OpenFoodFacts adapter. relevance_score: Локальная оценка соответствия пользовательскому запросу от 0 до 1. """

    product: OpenFoodFactsProduct
    relevance_score: float


def clean_text( value: Any, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def normalize_text( value: Any, ) -> str:
    return (
        clean_text(
            value
        )
        .lower()
        .replace(
            "ё",
            "е",
        )
    )


def tokenize( value: Any, ) -> list[str]:
    return [
        token
        for token in re.findall(
            r"[a-zа-я0-9]+",
            normalize_text(
                value
            ),
        )
        if len(token) >= 2
    ]


def product_search_text( product: OpenFoodFactsProduct, ) -> str:
    """ Собирает текст, по которому локально проверяем релевантность ответа OFF. """

    parts: list[str] = [
        product.product_name or "",
        product.product_name_ru or "",
        product.product_name_en or "",
        product.generic_name or "",
        product.generic_name_ru or "",
        product.generic_name_en or "",
        product.brands or "",
        *product.brands_tags,
        *product.categories,
        *product.categories_tags,
        *product.categories_tags_ru,
        *product.categories_tags_en,
    ]

    return normalize_text(
        " ".join(
            part
            for part in parts
            if part
        )
    )


def calculate_relevance( *, query: str, product: OpenFoodFactsProduct, ) -> float:
    """ Оценивает насколько ответ OFF похож на пользовательский запрос. Это дополнительный фильтр поверх полнотекстового поиска OFF. """

    query_normalized = normalize_text(
        query
    )

    query_tokens = tokenize(
        query
    )

    if not query_tokens:
        return 0.0

    text = product_search_text(
        product
    )

    if not text:
        return 0.0

    text_tokens = set(
        tokenize(
            text
        )
    )

    exact_matches = sum(
        1
        for token in query_tokens
        if token in text_tokens
    )

    partial_matches = sum(
        1
        for token in query_tokens
        if (
            token not in text_tokens
            and any(
                (
                    token in candidate
                    or candidate in token
                )
                for candidate in text_tokens
                if (
                    len(candidate) >= 4
                    and len(token) >= 4
                )
            )
        )
    )

    coverage = (
        exact_matches
        + partial_matches * 0.5
    ) / len(
        query_tokens
    )

    phrase_bonus = (
        0.20
        if (
            query_normalized
            and query_normalized in text
        )
        else 0.0
    )

    image_bonus = (
        0.08
        if (
            product.image_front_url
            or product.image_url
            or product.image_front_small_url
        )
        else 0.0
    )

    brand_bonus = (
        0.07
        if (
            product.brands
            or product.brands_tags
        )
        else 0.0
    )

    name_bonus = (
        0.05
        if product.product_name
        else 0.0
    )

    return min(
        1.0,
        (
            coverage
            + phrase_bonus
            + image_bonus
            + brand_bonus
            + name_bonus
        ),
    )


def product_quality_key( result: OpenFoodFactsSearchResult, ) -> tuple[
    float,
    int,
    int,
    int,
    int,
]:
    """ При одинаковой релевантности выше ставим более полную карточку. """

    product = result.product

    return (
        result.relevance_score,
        int(
            bool(
                product.image_front_url
                or product.image_url
                or product.image_front_small_url
            )
        ),
        int(
            bool(
                product.brands
                or product.brands_tags
            )
        ),
        int(
            bool(
                product.quantity
                or product.product_quantity
            )
        ),
        int(
            bool(
                product.categories
                or product.categories_tags
            )
        ),
    )


class OpenFoodFactsSearchClient:
    """ Полнотекстовый поиск Open Food Facts. Важно: Open Food Facts рекомендует не использовать search endpoint для поиска "по мере набора". Этот клиент вызывается только после отправки пользователем готового запроса. """

    def __init__( self, *, timeout_seconds: int = ( REQUEST_TIMEOUT_SECONDS ), ) -> None:
        self.timeout_seconds = max(
            5,
            min(
                int(
                    timeout_seconds
                ),
                30,
            ),
        )

        self._cache: dict[
            str,
            tuple[
                float,
                list[
                    OpenFoodFactsSearchResult
                ],
            ],
        ] = {}

    def _get_cached( self, query: str, ) -> list[
        OpenFoodFactsSearchResult
    ] | None:
        key = normalize_text(
            query
        )

        cached = self._cache.get(
            key
        )

        if cached is None:
            return None

        created_at, values = cached

        if (
            time.monotonic()
            - created_at
            > CACHE_TTL_SECONDS
        ):
            self._cache.pop(
                key,
                None,
            )

            return None

        return list(
            values
        )

    def _save_cache( self, query: str, values: list[ OpenFoodFactsSearchResult ], ) -> None:
        self._cache[
            normalize_text(
                query
            )
        ] = (
            time.monotonic(),
            list(
                values
            ),
        )

    @staticmethod
    def _fields() -> str:
        """ Поля, достаточные для MarkaRadar: название, бренд, фото, упаковка, категория, описание/состав. """

        return ",".join(
            (
                "code",

                "product_name",
                "product_name_ru",
                "product_name_en",
                "abbreviated_product_name",

                "generic_name",
                "generic_name_ru",
                "generic_name_en",

                "brands",
                "brands_tags",

                "quantity",
                "product_quantity",
                "product_quantity_unit",
                "serving_size",

                "packaging",
                "packaging_tags",

                "image_url",
                "image_front_url",
                "image_front_small_url",

                "categories",
                "categories_tags",
                "categories_tags_ru",
                "categories_tags_en",

                "labels",
                "labels_tags",

                "ingredients_text",
                "ingredients_text_ru",
                "ingredients_text_en",

                "countries",
                "countries_tags",

                "stores",
                "completeness",
            )
        )

    async def _request( self, *, query: str, page_size: int, ) -> list[
        dict[
            str,
            Any,
        ]
    ]:
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds
        )

        headers = {
            "User-Agent": (
                OPENFOODFACTS_USER_AGENT
            ),
            "Accept": (
                "application/json"
            ),
        }

        params = {
            "search_terms": query,
            "search_simple": "1",
            "action": "process",
            "json": "1",
            "page": "1",
            "page_size": str(
                page_size
            ),
            "fields": self._fields(),
            "lc": "ru",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                async with session.get(
                    SEARCH_URL,
                    params=params,
                    allow_redirects=True,
                ) as response:
                    if response.status == 429:
                        logger.info(
                            "OpenFoodFacts search "
                            "rate limited for query=%r",
                            query,
                        )

                        return []

                    if (
                        response.status
                        >= 500
                    ):
                        logger.info(
                            "OpenFoodFacts search "
                            "temporary error: "
                            "status=%s query=%r",
                            response.status,
                            query,
                        )

                        return []

                    response.raise_for_status()

                    payload = (
                        await response.json(
                            content_type=None
                        )
                    )

        except asyncio.TimeoutError:
            logger.info(
                "OpenFoodFacts search "
                "timeout for query=%r",
                query,
            )

            return []

        except aiohttp.ClientError as error:
            logger.info(
                "OpenFoodFacts search "
                "request failed for query=%r: %s",
                query,
                error.__class__.__name__,
            )

            return []

        except Exception:
            logger.exception(
                "Unexpected OpenFoodFacts "
                "search error for query=%r",
                query,
            )

            return []

        products = payload.get(
            "products"
        )

        if not isinstance(
            products,
            list,
        ):
            return []

        return [
            product
            for product in products
            if isinstance(
                product,
                dict,
            )
        ]

    async def search( self, query: str, *, limit: int = 8, require_image: bool = False, ) -> list[
        OpenFoodFactsSearchResult
    ]:
        """ Ищет товары по обычному текстовому запросу. Пример: await client.search( "кофе poetti", limit=8, ) Возвращает нормализованные товары OFF, уже отсортированные по релевантности и полноте карточки. """

        cleaned_query = clean_text(
            query
        )

        if not cleaned_query:
            return []

        # Односимвольные/слишком короткие запросы
        # не отправляем во внешний поиск.
        if len(
            normalize_text(
                cleaned_query
            )
        ) < 3:
            return []

        safe_limit = max(
            1,
            min(
                int(
                    limit
                ),
                MAX_RESULTS,
            ),
        )

        cached = self._get_cached(
            cleaned_query
        )

        if cached is not None:
            values = cached

            if require_image:
                values = [
                    item
                    for item in values
                    if (
                        item.product.image_front_url
                        or item.product.image_url
                        or item.product.image_front_small_url
                    )
                ]

            return values[
                :safe_limit
            ]

        # Берём чуть больше результатов,
        # потому что часть карточек OFF
        # может оказаться пустой или нерелевантной.
        request_limit = min(
            max(
                safe_limit * 3,
                12,
            ),
            30,
        )

        raw_products = (
            await self._request(
                query=cleaned_query,
                page_size=request_limit,
            )
        )

        results: list[
            OpenFoodFactsSearchResult
        ] = []

        seen_codes: set[
            str
        ] = set()

        for raw_product in raw_products:
            raw_code = clean_text(
                raw_product.get(
                    "code"
                )
            )

            code = "".join(
                character
                for character in raw_code
                if character.isdigit()
            )

            if len(code) < 8:
                continue

            if code in seen_codes:
                continue

            seen_codes.add(
                code
            )

            try:
                product = (
                    build_normalized_product(
                        barcode=code,
                        product=raw_product,
                    )
                )
            except Exception:
                logger.exception(
                    "Failed to normalize "
                    "OpenFoodFacts search "
                    "product code=%s",
                    code,
                )

                continue

            if not product.product_name:
                continue

            relevance = (
                calculate_relevance(
                    query=cleaned_query,
                    product=product,
                )
            )

            # Слабые совпадения не импортируем,
            # чтобы не засорять каноническую БД.
            if relevance < 0.42:
                continue

            if (
                require_image
                and not (
                    product.image_front_url
                    or product.image_url
                    or product.image_front_small_url
                )
            ):
                continue

            results.append(
                OpenFoodFactsSearchResult(
                    product=product,
                    relevance_score=relevance,
                )
            )

        results.sort(
            key=product_quality_key,
            reverse=True,
        )

        self._save_cache(
            cleaned_query,
            results,
        )

        selected = results[
            :safe_limit
        ]

        logger.info(
            "OpenFoodFacts text search: "
            "query=%r results=%s with_images=%s",
            cleaned_query,
            len(
                selected
            ),
            sum(
                1
                for item in selected
                if (
                    item.product.image_front_url
                    or item.product.image_url
                    or item.product.image_front_small_url
                )
            ),
        )

        return selected
