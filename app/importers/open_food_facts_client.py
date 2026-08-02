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
        timeout_seconds: int = 60,
        retries: int = 6,
    ) -> None:
        self.timeout = aiohttp.ClientTimeout(
            total=timeout_seconds,
            connect=20,
            sock_read=40,
        )
        self.retries = retries

        self.headers = {
            "User-Agent": (
                "MarkaRadar/0.1 "
                "(https://github.com/Avramradar/marka-radar)"
            ),
            "Accept": "application/json",
            "Accept-Language": "ru,en;q=0.8",
        }

    async def _request(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{path}"
        last_error: Exception | None = None

        async with aiohttp.ClientSession(
            timeout=self.timeout,
            headers=self.headers,
        ) as session:
            for attempt in range(1, self.retries + 1):
                try:
                    logger.info(
                        "Open Food Facts: попытка %s из %s",
                        attempt,
                        self.retries,
                    )

                    async with session.get(
                        url,
                        params=params,
                        allow_redirects=True,
                    ) as response:
                        response_text = await response.text()

                        if response.status == 200:
                            try:
                                data = await response.json(
                                    content_type=None,
                                )
                            except Exception as error:
                                raise OpenFoodFactsError(
                                    "Ответ невозможно разобрать как JSON. "
                                    f"Начало ответа: {response_text[:300]}"
                                ) from error

                            if not isinstance(data, dict):
                                raise OpenFoodFactsError(
                                    "Open Food Facts вернул "
                                    "не объект JSON"
                                )

                            return data

                        if response.status in {
                            429,
                            500,
                            502,
                            503,
                            504,
                        }:
                            retry_after_header = (
                                response.headers.get("Retry-After")
                            )

                            if retry_after_header:
                                try:
                                    delay = int(
                                        retry_after_header
                                    )
                                except ValueError:
                                    delay = attempt * 10
                            else:
                                delay = attempt * 10

                            delay = min(delay, 60)

                            error = OpenFoodFactsError(
                                f"HTTP {response.status}. "
                                f"Ответ: {response_text[:300]}"
                            )
                            last_error = error

                            logger.warning(
                                "Open Food Facts временно недоступен: "
                                "HTTP %s. Повтор через %s секунд. "
                                "Ответ: %s",
                                response.status,
                                delay,
                                response_text[:200],
                            )

                            if attempt < self.retries:
                                await asyncio.sleep(delay)
                                continue

                            break

                        raise OpenFoodFactsError(
                            f"HTTP {response.status}. "
                            f"Ответ: {response_text[:500]}"
                        )

                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    OpenFoodFactsError,
                ) as error:
                    last_error = error

                    logger.warning(
                        "Ошибка запроса Open Food Facts, "
                        "попытка %s из %s: %r",
                        attempt,
                        self.retries,
                        error,
                    )

                    if attempt < self.retries:
                        delay = min(attempt * 10, 60)
                        await asyncio.sleep(delay)

        raise OpenFoodFactsError(
            "Не удалось получить данные из Open Food Facts. "
            f"Последняя ошибка: {last_error!r}"
        ) from last_error

    async def search_products(
        self,
        *,
        page: int = 1,
        page_size: int = 100,
        country: str = "russia",
    ) -> list[dict[str, Any]]:
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
        cleaned_barcode = barcode.strip()

        if not cleaned_barcode.isdigit():
            raise ValueError(
                "Штрихкод должен состоять из цифр"
            )

        data = await self._request(
            f"/api/v2/product/{cleaned_barcode}",
            params={
                "fields": self.SEARCH_FIELDS,
            },
        )

        if data.get("status") != 1:
            return None

        product = data.get("product")

        if not isinstance(product, dict):
            return None

        return product
