from __future__ import annotations

import logging

from app.integrations.openfoodfacts_adapter import (
    build_description,
    build_keywords,
    build_subtype,
    choose_brand,
    choose_name,
    parse_structured_quantity,
    unique_values,
)
from app.integrations.openfoodfacts_client import (
    OPENFOODFACTS_BASE_URL,
    OpenFoodFactsClient,
    OpenFoodFactsProduct,
)
from app.integrations.openfoodfacts_search_client import (
    OpenFoodFactsSearchClient,
)
from app.integrations.providers.base import (
    ExternalCatalogProvider,
    ExternalProduct,
    ExternalSearchResult,
    clean_external_text,
    normalize_external_barcode,
    normalize_external_keywords,
)


logger = logging.getLogger(__name__)


class OpenFoodFactsProvider(
    ExternalCatalogProvider
):
    """ Провайдер Open Food Facts для новой универсальной архитектуры MarkaRadar. Он умеет: - искать товары по названию; - получать товар по штрихкоду; - приводить результат к ExternalProduct. В базу данных этот класс ничего не сохраняет. За сохранение отвечает отдельный сервис импорта. """

    provider_name = "openfoodfacts"

    def __init__( self, ) -> None:
        self._barcode_client = (
            OpenFoodFactsClient()
        )

        self._search_client = (
            OpenFoodFactsSearchClient()
        )

    @staticmethod
    def _build_source_url( barcode: str, ) -> str:
        return (
            f"{OPENFOODFACTS_BASE_URL}"
            f"/product/{barcode}"
        )

    @staticmethod
    def _convert_product( product: OpenFoodFactsProduct, ) -> ExternalProduct | None:
        """ Преобразует старую модель OFF в новый универсальный ExternalProduct. """

        name = choose_name(
            product
        )

        if not name:
            return None

        barcode = normalize_external_barcode(
            product.barcode
        )

        if not barcode:
            return None

        brand_name = clean_external_text(
            choose_brand(
                product
            )
        )

        package_value, package_unit = (
            parse_structured_quantity(
                product
            )
        )

        category_values = unique_values(
            (
                *product.categories,
                *product.categories_tags,
                *product.categories_tags_ru,
                *product.categories_tags_en,
            )
        )

        category_name = (
            category_values[0]
            if category_values
            else None
        )

        image_url = (
            product.image_front_url
            or product.image_url
            or product.image_front_small_url
        )

        keyword_text = build_keywords(
            product
        )

        keyword_values: list[str] = []

        if keyword_text:
            keyword_values.extend(
                value.strip()
                for value
                in keyword_text.split(",")
                if value.strip()
            )

        keyword_values.extend(
            category_values
        )

        if brand_name:
            keyword_values.append(
                brand_name
            )

        keywords = (
            normalize_external_keywords(
                keyword_values
            )
        )

        return ExternalProduct(
            provider=(
                OpenFoodFactsProvider
                .provider_name
            ),
            source_id=barcode,
            name=name,
            brand_name=brand_name,
            barcode=barcode,
            category_name=category_name,
            external_category_values=tuple(
                category_values
            ),
            package_value=package_value,
            package_unit=package_unit,
            subtype=clean_external_text(
                build_subtype(
                    product
                )
            ),
            description=clean_external_text(
                build_description(
                    product
                )
            ),
            image_url=clean_external_text(
                image_url
            ),
            source_url=(
                OpenFoodFactsProvider
                ._build_source_url(
                    barcode
                )
            ),
            keywords=keywords,
            raw=dict(
                product.raw
            ),
        )

    async def search( self, query: str, *, limit: int = 8, ) -> ExternalSearchResult:
        """ Полнотекстовый поиск OFF. Если OFF временно ничего не отдаёт, возвращается пустой ExternalSearchResult, а не исключение наружу. """

        cleaned_query = (
            clean_external_text(
                query
            )
            or ""
        )

        if not cleaned_query:
            return ExternalSearchResult(
                provider=self.provider_name,
                query="",
                products=(),
                attempted=False,
            )

        safe_limit = max(
            1,
            min(
                int(limit),
                12,
            ),
        )

        try:
            results = (
                await self._search_client.search(
                    cleaned_query,
                    limit=safe_limit,
                    require_image=False,
                )
            )

        except Exception as error:
            logger.exception(
                "OpenFoodFacts provider "
                "search failed: query=%r",
                cleaned_query,
            )

            return ExternalSearchResult(
                provider=self.provider_name,
                query=cleaned_query,
                products=(),
                attempted=True,
                unavailable=True,
                error=str(error),
            )

        products: list[
            ExternalProduct
        ] = []

        seen_source_ids: set[
            str
        ] = set()

        for result in results:
            converted = (
                self._convert_product(
                    result.product
                )
            )

            if converted is None:
                continue

            if (
                converted.source_id
                in seen_source_ids
            ):
                continue

            seen_source_ids.add(
                converted.source_id
            )

            products.append(
                converted
            )

            if len(
                products
            ) >= safe_limit:
                break

        logger.info(
            "Provider search: "
            "provider=%s query=%r products=%s "
            "with_images=%s",
            self.provider_name,
            cleaned_query,
            len(products),
            sum(
                1
                for product
                in products
                if product.image_url
            ),
        )

        return ExternalSearchResult(
            provider=self.provider_name,
            query=cleaned_query,
            products=tuple(
                products
            ),
            attempted=True,
            unavailable=False,
            error=None,
        )

    async def get_by_barcode( self, barcode: str, ) -> ExternalProduct | None:
        """ Получает одну карточку OFF по штрихкоду. """

        normalized_barcode = (
            normalize_external_barcode(
                barcode
            )
        )

        if not normalized_barcode:
            return None

        try:
            product = (
                await self
                ._barcode_client
                .get_product(
                    normalized_barcode
                )
            )

        except Exception:
            logger.exception(
                "OpenFoodFacts provider "
                "barcode lookup failed: %s",
                normalized_barcode,
            )

            return None

        if product is None:
            return None

        return self._convert_product(
            product
        )

    async def get_product( self, source_id: str, ) -> ExternalProduct | None:
        """ Для OFF source_id равен штрихкоду, поэтому используем тот же путь. """

        return await self.get_by_barcode(
            source_id
        )
