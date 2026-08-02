import asyncio
import logging
from typing import Any

import aiohttp


logger = logging.getLogger(__name__)


class OpenFoodFactsError(RuntimeError):
    """Ошибка при обращении к Open Food Facts."""


class OpenFoodFactsClient:
    BASE_URL = "https://world.openfoodfacts.org"

    SEARCH_FIELDS = (
        "code,"
        "product_name,"
        "product_name_ru,"
        "generic_name,"
        "brands,"
        "categories,"
        "categories_tags,"
        "countries_tags,"
        "quantity,"
        "product_quantity,"
        "product_quantity_unit,"
        "image_front_url,"
        "image_front_small_url,"
        "image_url,"
        "last_modified_t,"
        "completeness"
    )

    def __init__(
        self,
        timeout_seconds: int = 30,
        retries: int = 3,
    ) -> None:
        self.timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
        )
        self.retries = retries

        # Open Food Facts требует собственный User-Agent.
        self.headers = {
            "User-Agent": (
                "MarkaRadar/0.1 "
                "(https://github.com/Avramradar/marka-radar)"
            ),
            "Accept": "application/json",
        }

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"

        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                async with aiohttp.ClientSession(
                    timeout=self.timeout,
                    headers=self.headers,
                ) as session:
                    async with session.get(
                        url,
                        params=params,
                    ) as response:
                        if response.status == 429:
                            retry_after = int(
                                response.headers.get(
                                    "Retry-After",
                                    "60",
                                )
                            )

                            logger.warning(
                                "Open Food Facts ограничил запросы. "
                                "Повтор через %s секунд.",
                                retry_after,
                            )

                            await asyncio.sleep(retry_after)
                            continue

                        if response.status >= 500:
                            raise OpenFoodFactsError(
                                "Сервер Open Food Facts вернул "
                                f"ошибку {response.status}"
                            )

                        if response.status != 200:
                            response_text = await response.text()

                            raise OpenFoodFactsError(
                                "Open Food Facts вернул "
                                f"HTTP {response.status}: "
                                f"{response_text[:300]}"
                            )

                        data = await response.json(
                            content_type=None,
                        )

                        if not isinstance(data, dict):
                            raise OpenFoodFactsError(
                                "Open Food Facts вернул "
                                "некорректный формат данных"
                            )

                        return data

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OpenFoodFactsError,
            ) as error:
                last_error = error

                logger.warning(
                    "Ошибка Open Food Facts. "
                    "Попытка %s из %s: %s",
                    attempt,
                    self.retries,
                    error,
                )

                if attempt < self.retries:
                    await asyncio.sleep(attempt * 2)

        raise OpenFoodFactsError(
            "Не удалось получить данные "
            "из Open Food Facts"
        ) from last_error

    async def search_products(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        country: str = "russia",
    ) -> list[dict[str, Any]]:
        """
        Загружает одну страницу товаров.

        Для первого теста используем максимум 100 товаров.
        """

        if page < 1:
            raise ValueError(
                "Номер страницы должен быть больше нуля"
            )

        if page_size < 1 or page_size > 100:
            raise ValueError(
                "Размер страницы должен быть от 1 до 100"
            )

        params = {
            "page": page,
            "page_size": page_size,
            "countries_tags_en": country,
            "fields": self.SEARCH_FIELDS,
            "sort_by": "unique_scans_n",
        }

        data = await self._request(
            "/api/v2/search",
            params=params,
        )

        products = data.get("products", [])

        if not isinstance(products, list):
            raise OpenFoodFactsError(
                "Поле products имеет неверный формат"
            )

        return [
            product
            for product in products
            if isinstance(product, dict)
        ]

    async def get_product(
        self,
        barcode: str,
    ) -> dict[str, Any] | None:
        """
        Загружает конкретный товар по штрихкоду.
        """

        cleaned_barcode = barcode.strip()

        if not cleaned_barcode.isdigit():
            raise ValueError(
                "Штрихкод должен состоять из цифр"
            )

        params = {
            "fields": self.SEARCH_FIELDS,
        }

        data = await self._request(
            f"/api/v2/product/{cleaned_barcode}",
            params=params,
        )

        if data.get("status") != 1:
            return None

        product = data.get("product")

        if not isinstance(product, dict):
            return None

        return product
