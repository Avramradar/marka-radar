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

    raw содержит полный объект продукта, если
    понадобилось расширенное обогащение.
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
        str(value)
        .strip()
        .split()
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


def pick_first_string(
    product: dict[str, Any],
    keys: tuple[str, ...],
) -> str | None:
    """
    Возвращает первую непустую строку
    среди указанных ключей.
    """

    for key in keys:
        value = clean_string(
            product.get(
                key
            )
        )

        if value:
            return value

    return None


def collect_dynamic_names(
    product: dict[str, Any],
    *,
    prefix: str,
) -> list[str]:
    """
    Собирает языковые поля вроде:

        product_name_ru
        product_name_en
        product_name_fr
        product_name_it

    или generic_name_*.
    """

    values: list[str] = []
    seen: set[str] = set()

    for key, raw_value in product.items():
        if not key.startswith(
            prefix
        ):
            continue

        value = clean_string(
            raw_value
        )

        if not value:
            continue

        normalized_value = (
            value.lower()
        )

        if normalized_value in seen:
            continue

        seen.add(
            normalized_value
        )

        values.append(
            value
        )

    return values


def pick_product_name(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает наиболее информативное название.

    Сначала приоритетные языки, потом любые
    дополнительные product_name_*.
    """

    preferred = pick_first_string(
        product,
        (
            "product_name_ru",
            "product_name",
            "product_name_en",
            "abbreviated_product_name",
        ),
    )

    if preferred:
        return preferred

    dynamic_names = (
        collect_dynamic_names(
            product,
            prefix="product_name_",
        )
    )

    if dynamic_names:
        return dynamic_names[0]

    generic = pick_first_string(
        product,
        (
            "generic_name_ru",
            "generic_name",
            "generic_name_en",
        ),
    )

    if generic:
        return generic

    dynamic_generic_names = (
        collect_dynamic_names(
            product,
            prefix="generic_name_",
        )
    )

    if dynamic_generic_names:
        return dynamic_generic_names[0]

    return None


def pick_generic_name(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает generic name.
    """

    preferred = pick_first_string(
        product,
        (
            "generic_name_ru",
            "generic_name",
            "generic_name_en",
        ),
    )

    if preferred:
        return preferred

    dynamic_names = (
        collect_dynamic_names(
            product,
            prefix="generic_name_",
        )
    )

    if dynamic_names:
        return dynamic_names[0]

    return None


def pick_ingredients_text(
    product: dict[str, Any],
) -> str | None:
    """
    Выбирает наиболее подходящий состав.
    """

    preferred = pick_first_string(
        product,
        (
            "ingredients_text_ru",
            "ingredients_text",
            "ingredients_text_en",
        ),
    )

    if preferred:
        return preferred

    values = (
        collect_dynamic_names(
            product,
            prefix="ingredients_text_",
        )
    )

    if values:
        return values[0]

    return None


def product_is_sparse(
    product: dict[str, Any],
) -> bool:
    """
    Определяет слишком бедную карточку OFF.

    Если есть только название и фотография,
    пробуем получить полный объект продукта.
    """

    meaningful_fields = (
        clean_string(
            product.get(
                "brands"
            )
        ),
        clean_string(
            product.get(
                "quantity"
            )
        ),
        clean_string(
            product.get(
                "product_quantity"
            )
        ),
        clean_string(
            product.get(
                "generic_name"
            )
        ),
        clean_string(
            product.get(
                "categories"
            )
        ),
        clean_string(
            product.get(
                "ingredients_text"
            )
        ),
    )

    list_fields = (
        clean_string_list(
            product.get(
                "brands_tags"
            )
        ),
        clean_string_list(
            product.get(
                "categories_tags"
            )
        ),
    )

    meaningful_count = sum(
        bool(value)
        for value in meaningful_fields
    )

    meaningful_count += sum(
        bool(value)
        for value in list_fields
    )

    return meaningful_count <= 1


def merge_product_payloads(
    first: dict[str, Any],
    second: dict[str, Any],
) -> dict[str, Any]:
    """
    Объединяет сокращённый и полный ответы OFF.

    Более полный ответ имеет приоритет,
    но непустые значения первого не теряются.
    """

    merged = dict(
        first
    )

    for key, value in second.items():
        if value is None:
            continue

        if value == "":
            continue

        if value == []:
            continue

        merged[key] = value

    return merged


def build_normalized_product(
    *,
    barcode: str,
    product: dict[str, Any],
) -> OpenFoodFactsProduct:
    """
    Преобразует сырой объект OFF
    в структуру MarkaRadar.
    """

    product_name_ru = clean_string(
        product.get(
            "product_name_ru"
        )
    )

    product_name_en = clean_string(
        product.get(
            "product_name_en"
        )
    )

    generic_name_ru = clean_string(
        product.get(
            "generic_name_ru"
        )
    )

    generic_name_en = clean_string(
        product.get(
            "generic_name_en"
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
        barcode=barcode,

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

        completeness=clean_float(
            product.get(
                "completeness"
            )
        ),

        raw=product,
    )


class OpenFoodFactsClient:
    """
    Асинхронный клиент Open Food Facts.

    Алгоритм:

    1. делает быстрый запрос только нужных полей;
    2. если карточка слишком бедная —
       получает полный объект товара;
    3. объединяет результаты;
    4. возвращает нормализованный объект.
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

    def build_url(
        self,
        barcode: str,
    ) -> str:
        return (
            f"{OPENFOODFACTS_BASE_URL}"
            f"/api/v2/product/"
            f"{barcode}.json"
        )

    async def request_product(
        self,
        *,
        http_session: aiohttp.ClientSession,
        barcode: str,
        fields: str | None,
    ) -> dict[str, Any] | None:
        """
        Выполняет один запрос в OpenFoodFacts.
        """

        params: dict[str, str] = {
            "lc": "ru",
            "product_type": "food",
        }

        if fields:
            params["fields"] = fields

        try:
            async with http_session.get(
                self.build_url(
                    barcode
                ),
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

        return product

    async def get_product(
        self,
        barcode: str,
    ) -> OpenFoodFactsProduct | None:
        """
        Получает товар по штрихкоду.
        """

        normalized_barcode = "".join(
            character
            for character in str(
                barcode
            )
            if character.isdigit()
        )

        if not (
            8
            <= len(
                normalized_barcode
            )
            <= 14
        ):
            return None

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

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as http_session:

            #
            # Первый запрос:
            # быстрый и компактный.
            #

            product = await self.request_product(
                http_session=http_session,
                barcode=normalized_barcode,
                fields=fields,
            )

            if product is None:
                return None

            #
            # Если карточка слишком бедная,
            # запрашиваем полный продукт.
            #
            # Для хорошо заполненных карточек
            # второго HTTP-запроса не будет.
            #

            if product_is_sparse(
                product
            ):
                full_product = (
                    await self.request_product(
                        http_session=http_session,
                        barcode=normalized_barcode,
                        fields=None,
                    )
                )

                if full_product:
                    product = (
                        merge_product_payloads(
                            product,
                            full_product,
                        )
                    )

        return build_normalized_product(
            barcode=normalized_barcode,
            product=product,
        )
