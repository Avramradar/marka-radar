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
    Нормализованный товар Open Food Facts.

    В raw сохраняется оригинальный объект,
    чтобы адаптер мог использовать дополнительные
    поля без постоянного изменения клиента.
    """

    barcode: str

    product_name: str | None
    product_name_ru: str | None
    product_name_en: str | None

    abbreviated_product_name: str | None

    brands: str | None
    brands_tags: tuple[str, ...]

    generic_name: str | None
    generic_name_ru: str | None
    generic_name_en: str | None

    quantity: str | None
    product_quantity: str | None
    product_quantity_unit: str | None
    serving_size: str | None

    packaging: str | None
    packaging_tags: tuple[str, ...]

    image_url: str | None
    image_front_url: str | None
    image_front_small_url: str | None

    categories: tuple[str, ...]
    categories_tags: tuple[str, ...]
    categories_tags_ru: tuple[str, ...]
    categories_tags_en: tuple[str, ...]

    labels: tuple[str, ...]
    labels_tags: tuple[str, ...]

    ingredients_text: str | None

    countries: str | None
    countries_tags: tuple[str, ...]

    stores: str | None

    completeness: float | None

    raw: dict[str, Any]


def clean_string(
    value: Any,
) -> str | None:
    """
    Приводит значение к чистой строке.
    """

    if value is None:
        return None

    cleaned = " ".join(
        str(value).strip().split()
    )

    return cleaned or None


def clean_string_list(
    value: Any,
) -> tuple[str, ...]:
    """
    Приводит список или строку
    к tuple[str, ...].
    """

    if not value:
        return ()

    if isinstance(
        value,
        (list, tuple, set),
    ):
        values = value

    else:
        values = str(
            value
        ).split(",")

    result: list[str] = []
    seen: set[str] = set()

    for item in values:
        cleaned = clean_string(
            item
        )

        if not cleaned:
            continue

        key = cleaned.lower()

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            cleaned
        )

    return tuple(
        result
    )


def clean_float(
    value: Any,
) -> float | None:
    """
    Безопасно преобразует значение в float.
    """

    if value is None:
        return None

    try:
        return float(
            value
        )
    except (
        TypeError,
        ValueError,
    ):
        return None


def pick_product_name(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает наиболее информативное название.

    Приоритет:

    1. русское название;
    2. основное название;
    3. английское название;
    4. сокращённое название;
    5. русское generic name;
    6. generic name;
    7. английское generic name.
    """

    candidates = (
        product.get(
            "product_name_ru"
        ),
        product.get(
            "product_name"
        ),
        product.get(
            "product_name_en"
        ),
        product.get(
            "abbreviated_product_name"
        ),
        product.get(
            "generic_name_ru"
        ),
        product.get(
            "generic_name"
        ),
        product.get(
            "generic_name_en"
        ),
    )

    for value in candidates:
        cleaned = clean_string(
            value
        )

        if cleaned:
            return cleaned

    return None


def pick_generic_name(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает generic name.
    """

    candidates = (
        product.get(
            "generic_name_ru"
        ),
        product.get(
            "generic_name"
        ),
        product.get(
            "generic_name_en"
        ),
    )

    for value in candidates:
        cleaned = clean_string(
            value
        )

        if cleaned:
            return cleaned

    return None


def pick_ingredients_text(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает наиболее подходящий текст состава.
    """

    candidates = (
        product.get(
            "ingredients_text_ru"
        ),
        product.get(
            "ingredients_text"
        ),
        product.get(
            "ingredients_text_en"
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

    Пока используем API v2 для совместимости
    с текущим адаптером MarkaRadar.
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

        Возвращает None, если товар отсутствует
        или внешний сервис временно недоступен.
        """

        normalized_barcode = "".join(
            character
            for character in str(
                barcode
            )
            if character.isdigit()
        )

        if len(
            normalized_barcode
        ) < 8:
            return None

        url = (
            f"{OPENFOODFACTS_BASE_URL}"
            f"/api/v2/product/"
            f"{normalized_barcode}"
        )

        # Open Food Facts рекомендует
        # явно ограничивать fields,
        # чтобы не тянуть весь объект товара.
        fields = ",".join(
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

        product = payload.get(
            "product"
        )

        if not isinstance(
            product,
            dict,
        ):
            return None

        # Некоторые ответы v2 содержат status,
        # некоторые клиенты лучше не должны
        # полностью зависеть от него.
        status = payload.get(
            "status"
        )

        if (
            status is not None
            and int(
                status or 0
            )
            != 1
        ):
            return None

        product_name_ru = (
            clean_string(
                product.get(
                    "product_name_ru"
                )
            )
        )

        product_name_en = (
            clean_string(
                product.get(
                    "product_name_en"
                )
            )
        )

        generic_name_ru = (
            clean_string(
                product.get(
                    "generic_name_ru"
                )
            )
        )

        generic_name_en = (
            clean_string(
                product.get(
                    "generic_name_en"
                )
            )
        )

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

        categories_tags_ru = (
            clean_string_list(
                product.get(
                    "categories_tags_ru"
                )
            )
        )

        categories_tags_en = (
            clean_string_list(
                product.get(
                    "categories_tags_en"
                )
            )
        )

        labels = clean_string_list(
            product.get(
                "labels"
            )
        )

        labels_tags = (
            clean_string_list(
                product.get(
                    "labels_tags"
                )
            )
        )

        brands_tags = (
            clean_string_list(
                product.get(
                    "brands_tags"
                )
            )
        )

        packaging_tags = (
            clean_string_list(
                product.get(
                    "packaging_tags"
                )
            )
        )

        countries_tags = (
            clean_string_list(
                product.get(
                    "countries_tags"
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

            product_name_ru=(
                product_name_ru
            ),

            product_name_en=(
                product_name_en
            ),

            abbreviated_product_name=(
                clean_string(
                    product.get(
                        "abbreviated_product_name"
                    )
                )
            ),

            brands=clean_string(
                product.get(
                    "brands"
                )
            ),

            brands_tags=(
                brands_tags
            ),

            generic_name=(
                pick_generic_name(
                    product
                )
            ),

            generic_name_ru=(
                generic_name_ru
            ),

            generic_name_en=(
                generic_name_en
            ),

            quantity=clean_string(
                product.get(
                    "quantity"
                )
            ),

            product_quantity=(
                clean_string(
                    product.get(
                        "product_quantity"
                    )
                )
            ),

            product_quantity_unit=(
                clean_string(
                    product.get(
                        "product_quantity_unit"
                    )
                )
            ),

            serving_size=(
                clean_string(
                    product.get(
                        "serving_size"
                    )
                )
            ),

            packaging=clean_string(
                product.get(
                    "packaging"
                )
            ),

            packaging_tags=(
                packaging_tags
            ),

            image_url=clean_string(
                product.get(
                    "image_url"
                )
            ),

            image_front_url=(
                clean_string(
                    product.get(
                        "image_front_url"
                    )
                )
            ),

            image_front_small_url=(
                clean_string(
                    product.get(
                        "image_front_small_url"
                    )
                )
            ),

            categories=categories,

            categories_tags=(
                categories_tags
            ),

            categories_tags_ru=(
                categories_tags_ru
            ),

            categories_tags_en=(
                categories_tags_en
            ),

            labels=labels,

            labels_tags=(
                labels_tags
            ),

            ingredients_text=(
                pick_ingredients_text(
                    product
                )
            ),

            countries=clean_string(
                product.get(
                    "countries"
                )
            ),

            countries_tags=(
                countries_tags
            ),

            stores=clean_string(
                product.get(
                    "stores"
                )
            ),

            completeness=(
                clean_float(
                    product.get(
                        "completeness"
                    )
                )
            ),

            raw=product,
        )
