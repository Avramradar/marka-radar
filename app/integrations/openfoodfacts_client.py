from dataclasses import dataclass
from typing import Any

import aiohttp


OPENFOODFACTS_BASE_URL = (
    "https://world.openfoodfacts.org"
)

OPENFOODFACTS_USER_AGENT = (
    "MarkaRadar/1.0 "
    "(product-data-integration)"
)

REQUEST_TIMEOUT_SECONDS = 15


@dataclass(slots=True, frozen=True)
class OpenFoodFactsProduct:
    """
    Нормализованный ответ Open Food Facts.
    """

    barcode: str
    product_name: str | None
    brands: str | None
    generic_name: str | None

    quantity: str | None

    image_url: str | None
    image_front_url: str | None

    categories: tuple[str, ...]
    categories_tags: tuple[str, ...]

    labels: tuple[str, ...]

    ingredients_text: str | None

    raw: dict[str, Any]


def clean_string(
    value: Any,
) -> str | None:
    if value is None:
        return None

    cleaned = " ".join(
        str(value).strip().split()
    )

    return cleaned or None


def clean_string_list(
    value: Any,
) -> tuple[str, ...]:
    if not value:
        return ()

    if isinstance(
        value,
        (list, tuple),
    ):
        result = [
            clean_string(item)
            for item in value
        ]
    else:
        result = [
            clean_string(item)
            for item
            in str(value).split(",")
        ]

    return tuple(
        item
        for item in result
        if item
    )


def pick_product_name(
    product: dict[str, Any],
) -> str | None:
    """
    Пытается выбрать наиболее информативное
    название товара.
    """

    candidates = (
        product.get(
            "product_name_ru"
        ),
        product.get(
            "product_name"
        ),
        product.get(
            "generic_name_ru"
        ),
        product.get(
            "generic_name"
        ),
    )

    for value in candidates:
        cleaned = clean_string(
            value
        )

        if cleaned:
            return cleaned

    return None


class OpenFoodFactsClient:
    """
    Асинхронный клиент Open Food Facts.

    Используется только для чтения.
    """

    def __init__(
        self,
        *,
        timeout_seconds: int = (
            REQUEST_TIMEOUT_SECONDS
        ),
    ) -> None:
        self.timeout_seconds = max(
            3,
            min(
                timeout_seconds,
                60,
            ),
        )

    async def get_product(
        self,
        barcode: str,
    ) -> OpenFoodFactsProduct | None:
        """
        Получает товар по штрихкоду.

        Возвращает None, если товар
        отсутствует в Open Food Facts.
        """

        normalized_barcode = "".join(
            character
            for character in str(barcode)
            if character.isdigit()
        )

        if len(
            normalized_barcode
        ) < 8:
            return None

        url = (
            f"{OPENFOODFACTS_BASE_URL}"
            f"/api/v2/product/"
            f"{normalized_barcode}.json"
        )

        fields = (
            "code,"
            "product_name,"
            "product_name_ru,"
            "generic_name,"
            "generic_name_ru,"
            "brands,"
            "quantity,"
            "image_url,"
            "image_front_url,"
            "categories,"
            "categories_tags,"
            "labels,"
            "labels_tags,"
            "ingredients_text,"
            "ingredients_text_ru"
        )

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds
        )

        headers = {
            "User-Agent": (
                OPENFOODFACTS_USER_AGENT
            ),
            "Accept": "application/json",
        }

        params = {
            "fields": fields,
            "lc": "ru",
            "product_type": "food",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as http_session:
                async with http_session.get(
                    url,
                    params=params,
                    allow_redirects=True,
                ) as response:
                    if response.status == 404:
                        return None

                    response.raise_for_status()

                    payload = (
                        await response.json()
                    )

        except (
            aiohttp.ClientError,
            TimeoutError,
        ):
            return None

        status = int(
            payload.get(
                "status",
                0,
            )
            or 0
        )

        product = payload.get(
            "product"
        )

        if (
            status != 1
            or not isinstance(
                product,
                dict,
            )
        ):
            return None

        categories = clean_string_list(
            product.get(
                "categories"
            )
        )

        categories_tags = (
            clean_string_list(
                product.get(
                    "categories_tags"
                )
            )
        )

        labels = clean_string_list(
            product.get(
                "labels"
            )
        )

        if not labels:
            labels = clean_string_list(
                product.get(
                    "labels_tags"
                )
            )

        ingredients_text = (
            clean_string(
                product.get(
                    "ingredients_text_ru"
                )
            )
            or clean_string(
                product.get(
                    "ingredients_text"
                )
            )
        )

        return OpenFoodFactsProduct(
            barcode=normalized_barcode,
            product_name=(
                pick_product_name(
                    product
                )
            ),
            brands=clean_string(
                product.get(
                    "brands"
                )
            ),
            generic_name=(
                clean_string(
                    product.get(
                        "generic_name_ru"
                    )
                )
                or clean_string(
                    product.get(
                        "generic_name"
                    )
                )
            ),
            quantity=clean_string(
                product.get(
                    "quantity"
                )
            ),
            image_url=clean_string(
                product.get(
                    "image_url"
                )
            ),
            image_front_url=clean_string(
                product.get(
                    "image_front_url"
                )
            ),
            categories=categories,
            categories_tags=(
                categories_tags
            ),
            labels=labels,
            ingredients_text=(
                ingredients_text
            ),
            raw=product,
                  )
