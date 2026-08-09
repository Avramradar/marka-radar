from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote_plus, urljoin

import aiohttp
from bs4 import BeautifulSoup

from app.integrations.providers.base import (
    ExternalCatalogProvider,
    ExternalProduct,
    ExternalSearchResult,
    clean_external_text,
    normalize_external_keywords,
)


logger = logging.getLogger(__name__)


DIXY_BASE_URL = "https://dixy.ru"

REQUEST_TIMEOUT_SECONDS = 18
MAX_CONCURRENT_DETAILS = 4
MAX_SEARCH_PAGES = 3


PACKAGE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(кг|kg|г|гр|g|л|l|мл|ml)"
    r"(?!\w)",
    re.IGNORECASE,
)


ARTICLE_PATTERN = re.compile(
    r"(?<!\d)(\d{7,14})(?!\d)"
)


GENERIC_QUERY_TOKENS = {
    "кофе",
    "чай",
    "молоко",
    "сметана",
    "кефир",
    "йогурт",
    "сыр",
    "масло",
    "вода",
    "сок",
    "пицца",
    "сельдь",
    "рыба",
    "мясо",
    "колбаса",
    "сосиски",
    "макароны",
    "рис",
    "гречка",
    "мука",
    "сахар",
    "соль",
    "хлеб",
    "батон",
    "печенье",
    "шоколад",
    "конфеты",
    "мороженое",
    "пельмени",
    "вареники",
    "творог",
    "сливки",
    "яйца",
    "яйцо",
    "майонез",
    "кетчуп",
    "соус",
    "продукт",
    "продукты",
    "напиток",
    "напитки",
}


GENERIC_MODIFIER_TOKENS = {
    "растворимый",
    "растворимое",
    "молотый",
    "молотое",
    "зерновой",
    "зерновое",
    "зернах",
    "зёрнах",
    "черный",
    "чёрный",
    "зеленый",
    "зелёный",
    "питьевой",
    "питьевое",
    "пастеризованный",
    "пастеризованное",
    "ультрапастеризованный",
    "ультрапастеризованное",
    "безлактозный",
    "безлактозное",
    "замороженный",
    "замороженная",
    "замороженное",
    "сливочный",
    "сливочная",
    "средний",
    "средняя",
    "жирности",
    "гост",
    "бзмж",
}


CATEGORY_HINTS: dict[
    str,
    tuple[str, ...],
] = {
    "молоко": (
        "молоко",
        "молочные продукты",
    ),
    "сметана": (
        "сметана",
        "молочные продукты",
    ),
    "кефир": (
        "кефир",
        "молочные продукты",
    ),
    "йогурт": (
        "йогурт",
        "молочные продукты",
    ),
    "творог": (
        "творог",
        "молочные продукты",
    ),
    "сыр": (
        "сыр",
        "сыры",
        "молочные продукты",
    ),
    "масло": (
        "масло",
        "сливочное масло",
    ),
    "кофе": (
        "кофе",
        "чай кофе какао",
    ),
    "чай": (
        "чай",
        "чай кофе какао",
    ),
    "вода": (
        "вода",
        "напитки",
    ),
    "сок": (
        "сок",
        "соки",
        "напитки",
    ),
    "пицца": (
        "пицца",
        "замороженные продукты",
    ),
    "сельдь": (
        "сельдь",
        "рыба",
    ),
}


SUBTYPE_RULES: tuple[
    tuple[str, tuple[str, ...]],
    ...
] = (
    (
        "Молотый",
        (
            "молотый",
            "молотая",
            "ground",
        ),
    ),
    (
        "В зёрнах",
        (
            "в зернах",
            "в зёрнах",
            "зерновой",
            "зерновая",
            "beans",
        ),
    ),
    (
        "Растворимый",
        (
            "растворимый",
            "instant",
        ),
    ),
    (
        "В капсулах",
        (
            "в капсулах",
            "капсулы",
            "капсульный",
        ),
    ),
    (
        "Пастеризованное",
        (
            "пастеризован",
        ),
    ),
    (
        "Ультрапастеризованное",
        (
            "ультрапастеризован",
        ),
    ),
    (
        "Безлактозное",
        (
            "безлактоз",
            "без лактозы",
        ),
    ),
    (
        "Стерилизованное",
        (
            "стерилизован",
        ),
    ),
    (
        "Замороженное",
        (
            "заморожен",
        ),
    ),
)


class DixyProvider(
    ExternalCatalogProvider
):
    """
    Провайдер публичного каталога Дикси.

    Основная задача провайдера:

        пользовательский запрос
        ↓
        публичные страницы dixy.ru
        ↓
        ссылки на товары
        ↓
        карточки товаров
        ↓
        ExternalProduct
        ↓
        Product Merge Engine

    Провайдер намеренно не считает цену
    универсальной характеристикой товара,
    поскольку ассортимент и цены магазина
    могут зависеть от выбранного адреса.
    """

    provider_name = "dixy"

    def __init__(
        self,
        *,
        timeout_seconds: int = (
            REQUEST_TIMEOUT_SECONDS
        ),
    ) -> None:
        self.timeout_seconds = max(
            5,
            min(
                int(timeout_seconds),
                45,
            ),
        )

    @staticmethod
    def _normalize(
        value: Any,
    ) -> str:
        return " ".join(
            str(value or "")
            .strip()
            .lower()
            .replace(
                "ё",
                "е",
            )
            .split()
        )

    @classmethod
    def _tokens(
        cls,
        value: Any,
    ) -> list[str]:
        return [
            token
            for token in re.findall(
                r"[a-zа-я0-9]+",
                cls._normalize(
                    value
                ),
                flags=re.IGNORECASE,
            )
            if len(token) >= 2
        ]

    @staticmethod
    def _token_matches(
        query_token: str,
        candidate_token: str,
    ) -> bool:
        if (
            query_token
            == candidate_token
        ):
            return True

        if (
            len(query_token) < 4
            or len(candidate_token) < 4
        ):
            return False

        return (
            query_token in candidate_token
            or candidate_token in query_token
        )

    @classmethod
    def _query_anchor_tokens(
        cls,
        query: str,
    ) -> list[str]:
        generic = {
            cls._normalize(
                value
            )
            for value in (
                GENERIC_QUERY_TOKENS
                | GENERIC_MODIFIER_TOKENS
            )
        }

        return [
            token
            for token in cls._tokens(
                query
            )
            if token not in generic
        ]

    @classmethod
    def _query_product_type(
        cls,
        query: str,
    ) -> str | None:
        tokens = cls._tokens(
            query
        )

        known_types = {
            cls._normalize(
                value
            )
            for value
            in GENERIC_QUERY_TOKENS
        }

        for token in tokens:
            if token in known_types:
                return token

        return None

    @classmethod
    def _has_all_anchor_tokens(
        cls,
        *,
        query: str,
        candidate_text: str,
    ) -> bool:
        anchors = cls._query_anchor_tokens(
            query
        )

        if not anchors:
            return True

        candidate_tokens = cls._tokens(
            candidate_text
        )

        if not candidate_tokens:
            return False

        for anchor in anchors:
            if not any(
                cls._token_matches(
                    anchor,
                    candidate_token,
                )
                for candidate_token
                in candidate_tokens
            ):
                return False

        return True

    @classmethod
    def _query_token_coverage(
        cls,
        *,
        query: str,
        candidate_text: str,
    ) -> float:
        query_tokens = cls._tokens(
            query
        )

        candidate_tokens = cls._tokens(
            candidate_text
        )

        if (
            not query_tokens
            or not candidate_tokens
        ):
            return 0.0

        matched = 0

        for query_token in query_tokens:
            if any(
                cls._token_matches(
                    query_token,
                    candidate_token,
                )
                for candidate_token
                in candidate_tokens
            ):
                matched += 1

        return (
            matched
            / len(query_tokens)
        )

    @classmethod
    def _is_relevant_candidate(
        cls,
        *,
        query: str,
        candidate_text: str,
    ) -> bool:
        if not cls._has_all_anchor_tokens(
            query=query,
            candidate_text=candidate_text,
        ):
            return False

        query_tokens = cls._tokens(
            query
        )

        if not query_tokens:
            return False

        coverage = (
            cls._query_token_coverage(
                query=query,
                candidate_text=(
                    candidate_text
                ),
            )
        )

        if len(query_tokens) == 1:
            return coverage >= 1.0

        return coverage >= 0.50

    @classmethod
    def _score_name(
        cls,
        *,
        query: str,
        name: str,
    ) -> float:
        query_tokens = cls._tokens(
            query
        )

        name_tokens = cls._tokens(
            name
        )

        if (
            not query_tokens
            or not name_tokens
        ):
            return 0.0

        exact = 0
        partial = 0

        for query_token in query_tokens:
            if query_token in name_tokens:
                exact += 1
                continue

            if any(
                cls._token_matches(
                    query_token,
                    name_token,
                )
                for name_token
                in name_tokens
            ):
                partial += 1

        coverage = (
            exact
            + 0.55 * partial
        ) / len(query_tokens)

        query_normalized = (
            cls._normalize(
                query
            )
        )

        name_normalized = (
            cls._normalize(
                name
            )
        )

        phrase_bonus = (
            0.18
            if (
                query_normalized
                and query_normalized
                in name_normalized
            )
            else 0.0
        )

        anchor_bonus = 0.0

        anchors = (
            cls._query_anchor_tokens(
                query
            )
        )

        if (
            anchors
            and cls._has_all_anchor_tokens(
                query=query,
                candidate_text=name,
            )
        ):
            anchor_bonus = 0.20

        return min(
            1.0,
            coverage
            + phrase_bonus
            + anchor_bonus,
        )

    @classmethod
    def _parse_package(
        cls,
        text: str,
    ) -> tuple[
        Decimal | None,
        str | None,
    ]:
        matches = list(
            PACKAGE_PATTERN.finditer(
                text
            )
        )

        if not matches:
            return None, None

        match = matches[-1]

        raw_value = (
            match.group(1)
            .replace(
                ",",
                ".",
            )
        )

        raw_unit = (
            match.group(2)
            .lower()
        )

        try:
            value = Decimal(
                raw_value
            )
        except (
            InvalidOperation,
            ValueError,
        ):
            return None, None

        unit_map = {
            "g": "г",
            "гр": "г",
            "г": "г",
            "kg": "кг",
            "кг": "кг",
            "ml": "мл",
            "мл": "мл",
            "l": "л",
            "л": "л",
        }

        return (
            value,
            unit_map.get(
                raw_unit
            ),
        )

    @classmethod
    def _detect_subtype(
        cls,
        text: str,
    ) -> str | None:
        normalized = cls._normalize(
            text
        )

        for (
            title,
            terms,
        ) in SUBTYPE_RULES:
            if any(
                cls._normalize(
                    term
                )
                in normalized
                for term in terms
            ):
                return title

        return None

    @staticmethod
    def _clean_url(
        url: str,
    ) -> str:
        return (
            url.split(
                "#",
                1,
            )[0]
            .split(
                "?",
                1,
            )[0]
        )

    @classmethod
    def _extract_source_id(
        cls,
        url: str,
    ) -> str:
        clean_url = cls._clean_url(
            url
        ).rstrip("/")

        last_part = (
            clean_url.rsplit(
                "/",
                1,
            )[-1]
        )

        matches = (
            ARTICLE_PATTERN.findall(
                last_part
            )
        )

        if matches:
            return matches[-1]

        return last_part

    @classmethod
    def _looks_like_product_url(
        cls,
        url: str,
    ) -> bool:
        normalized = cls._normalize(
            url
        )

        if "/product/" in normalized:
            return True

        if "/catalog/" not in normalized:
            return False

        clean_url = cls._clean_url(
            url
        ).rstrip("/")

        last_part = (
            clean_url.rsplit(
                "/",
                1,
            )[-1]
        )

        return bool(
            ARTICLE_PATTERN.search(
                last_part
            )
        )

    async def _fetch_text(
        self,
        *,
        session: aiohttp.ClientSession,
        url: str,
    ) -> str | None:
        for attempt in range(2):
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                ) as response:
                    logger.info(
                        "Dixy HTTP: status=%s url=%s",
                        response.status,
                        url,
                    )

                    if response.status == 404:
                        return None

                    if response.status in {
                        401,
                        403,
                        429,
                        500,
                        502,
                        503,
                        504,
                    }:
                        if attempt == 0:
                            await asyncio.sleep(
                                0.8
                            )
                            continue

                        return None

                    response.raise_for_status()

                    return await response.text()

            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                TimeoutError,
            ):
                if attempt == 0:
                    await asyncio.sleep(
                        0.4
                    )
                    continue

                logger.exception(
                    "Dixy request failed: %s",
                    url,
                )

                return None

        return None

    @staticmethod
    def _json_ld_values(
        soup: BeautifulSoup,
    ) -> list[dict[str, Any]]:
        result: list[
            dict[str, Any]
        ] = []

        for script in soup.find_all(
            "script",
            attrs={
                "type": "application/ld+json",
            },
        ):
            raw = (
                script.string
                or script.get_text(
                    strip=True
                )
            )

            if not raw:
                continue

            try:
                data = json.loads(
                    raw
                )
            except Exception:
                continue

            values = (
                data
                if isinstance(
                    data,
                    list,
                )
                else [
                    data
                ]
            )

            for value in values:
                if not isinstance(
                    value,
                    dict,
                ):
                    continue

                result.append(
                    value
                )

                graph = value.get(
                    "@graph"
                )

                if isinstance(
                    graph,
                    list,
                ):
                    result.extend(
                        item
                        for item in graph
                        if isinstance(
                            item,
                            dict,
                        )
                    )

        return result

    @classmethod
    def _json_ld_product(
        cls,
        soup: BeautifulSoup,
    ) -> dict[str, Any]:
        for value in cls._json_ld_values(
            soup
        ):
            product_type = value.get(
                "@type"
            )

            if product_type == "Product":
                return value

            if (
                isinstance(
                    product_type,
                    list,
                )
                and "Product"
                in product_type
            ):
                return value

        return {}

    @classmethod
    def _extract_label_value(
        cls,
        *,
        soup: BeautifulSoup,
        labels: tuple[str, ...],
    ) -> str | None:
        normalized_labels = {
            cls._normalize(
                label
            )
            for label in labels
        }

        strings = list(
            soup.stripped_strings
        )

        for index, raw_value in enumerate(
            strings
        ):
            normalized_value = (
                cls._normalize(
                    raw_value
                )
            )

            if (
                normalized_value
                in normalized_labels
                and index + 1
                < len(strings)
            ):
                candidate = (
                    strings[
                        index + 1
                    ]
                    .strip()
                )

                if (
                    candidate
                    and len(candidate)
                    <= 500
                ):
                    return candidate

            for label in labels:
                normalized_label = (
                    cls._normalize(
                        label
                    )
                )

                prefixes = (
                    normalized_label + " ",
                    normalized_label + ":",
                )

                if any(
                    normalized_value.startswith(
                        prefix
                    )
                    for prefix in prefixes
                ):
                    candidate = (
                        str(raw_value)
                        [len(label):]
                        .strip(
                            " :—-"
                        )
                    )

                    if candidate:
                        return candidate

        return None

    @classmethod
    def _extract_brand(
        cls,
        *,
        soup: BeautifulSoup,
        product_ld: dict[str, Any],
    ) -> str | None:
        raw_brand = product_ld.get(
            "brand"
        )

        if isinstance(
            raw_brand,
            dict,
        ):
            value = clean_external_text(
                raw_brand.get(
                    "name"
                )
            )

            if value:
                return value

        if isinstance(
            raw_brand,
            str,
        ):
            value = clean_external_text(
                raw_brand
            )

            if value:
                return value

        return cls._extract_label_value(
            soup=soup,
            labels=(
                "Торговая марка",
                "Бренд",
                "Марка",
            ),
        )

    @classmethod
    def _extract_category(
        cls,
        *,
        soup: BeautifulSoup,
        product_ld: dict[str, Any],
        fallback: str | None,
    ) -> str | None:
        value = clean_external_text(
            product_ld.get(
                "category"
            )
        )

        if value:
            return value

        value = cls._extract_label_value(
            soup=soup,
            labels=(
                "Тип товара",
                "Категория",
                "Тип",
            ),
        )

        if value:
            return value

        return clean_external_text(
            fallback
        )

    @classmethod
    def _extract_description(
        cls,
        *,
        soup: BeautifulSoup,
        product_ld: dict[str, Any],
    ) -> str | None:
        description = (
            clean_external_text(
                product_ld.get(
                    "description"
                )
            )
        )

        if description:
            return description[:1500]

        value = cls._extract_label_value(
            soup=soup,
            labels=(
                "Описание",
            ),
        )

        if value:
            return value[:1500]

        meta = soup.find(
            "meta",
            attrs={
                "name": "description",
            },
        )

        if (
            meta is not None
            and meta.get(
                "content"
            )
        ):
            value = clean_external_text(
                meta.get(
                    "content"
                )
            )

            if value:
                return value[:1500]

        return None

    @classmethod
    def _extract_image(
        cls,
        *,
        soup: BeautifulSoup,
        product_ld: dict[str, Any],
    ) -> str | None:
        image = product_ld.get(
            "image"
        )

        if isinstance(
            image,
            str,
        ):
            return urljoin(
                DIXY_BASE_URL,
                image,
            )

        if (
            isinstance(
                image,
                list,
            )
            and image
        ):
            return urljoin(
                DIXY_BASE_URL,
                str(
                    image[0]
                ),
            )

        if isinstance(
            image,
            dict,
        ):
            raw_url = (
                image.get(
                    "url"
                )
                or image.get(
                    "contentUrl"
                )
            )

            if raw_url:
                return urljoin(
                    DIXY_BASE_URL,
                    str(raw_url),
                )

        meta = soup.find(
            "meta",
            attrs={
                "property": "og:image",
            },
        )

        if (
            meta is not None
            and meta.get(
                "content"
            )
        ):
            return urljoin(
                DIXY_BASE_URL,
                str(
                    meta.get(
                        "content"
                    )
                ),
            )

        return None

    @classmethod
    def _extract_barcode(
        cls,
        *,
        soup: BeautifulSoup,
        product_ld: dict[str, Any],
    ) -> str | None:
        for key in (
            "gtin13",
            "gtin12",
            "gtin14",
            "gtin8",
            "gtin",
        ):
            raw = product_ld.get(
                key
            )

            if not raw:
                continue

            digits = "".join(
                char
                for char in str(raw)
                if char.isdigit()
            )

            if 8 <= len(digits) <= 14:
                return digits

        raw = cls._extract_label_value(
            soup=soup,
            labels=(
                "Штрихкод",
                "EAN",
                "GTIN",
            ),
        )

        if not raw:
            return None

        digits = "".join(
            char
            for char in raw
            if char.isdigit()
        )

        if 8 <= len(digits) <= 14:
            return digits

        return None

    @classmethod
    def _extract_product_links(
        cls,
        *,
        html: str,
        query: str,
    ) -> list[
        tuple[
            float,
            str,
            str,
        ]
    ]:
        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        candidates: list[
            tuple[
                float,
                str,
                str,
            ]
        ] = []

        seen: set[str] = set()

        for anchor in soup.find_all(
            "a",
            href=True,
        ):
            href = str(
                anchor.get(
                    "href"
                )
                or ""
            )

            url = urljoin(
                DIXY_BASE_URL,
                href,
            )

            url = cls._clean_url(
                url
            )

            if not cls._looks_like_product_url(
                url
            ):
                continue

            if url in seen:
                continue

            seen.add(
                url
            )

            text = " ".join(
                anchor.stripped_strings
            ).strip()

            image = anchor.find(
                "img"
            )

            if (
                not text
                and image is not None
            ):
                text = str(
                    image.get(
                        "alt"
                    )
                    or image.get(
                        "title"
                    )
                    or ""
                ).strip()

            if len(text) < 4:
                parent = anchor.parent
                hops = 0

                while (
                    parent is not None
                    and hops < 4
                    and len(text) < 4
                ):
                    text = " ".join(
                        parent.stripped_strings
                    ).strip()

                    parent = parent.parent
                    hops += 1

            text = re.split(
                (
                    r"\s+В корзину"
                    r"|\s+Купить"
                    r"|\s+Нет в наличии"
                    r"|\s+\d[\d\s,.]*[₽р]"
                ),
                text,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()

            if len(text) < 4:
                text = (
                    cls._extract_source_id(
                        url
                    )
                )

            score = cls._score_name(
                query=query,
                name=text,
            )

            candidates.append(
                (
                    score,
                    text,
                    url,
                )
            )

        candidates.sort(
            key=lambda item: (
                item[0],
                len(item[1]),
            ),
            reverse=True,
        )

        return candidates

    @classmethod
    def _search_urls(
        cls,
        query: str,
    ) -> list[str]:
        encoded = quote_plus(
            query
        )

        return [
            (
                f"{DIXY_BASE_URL}/search/"
                f"?q={encoded}"
            ),
            (
                f"{DIXY_BASE_URL}/catalog/"
                f"?q={encoded}"
            ),
            (
                f"{DIXY_BASE_URL}/search"
                f"?q={encoded}"
            ),
        ]

    async def _load_product(
        self,
        *,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        query: str,
        preliminary_name: str,
        url: str,
        preliminary_score: float,
        fallback_category_name: str | None,
    ) -> tuple[
        float,
        ExternalProduct,
    ] | None:
        async with semaphore:
            html = await self._fetch_text(
                session=session,
                url=url,
            )

        if not html:
            return None

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        product_ld = (
            self._json_ld_product(
                soup
            )
        )

        h1 = soup.find(
            "h1"
        )

        name = (
            clean_external_text(
                product_ld.get(
                    "name"
                )
            )
            or (
                clean_external_text(
                    h1.get_text(
                        " ",
                        strip=True,
                    )
                )
                if h1 is not None
                else None
            )
            or preliminary_name
        )

        if not name:
            return None

        brand_name = self._extract_brand(
            soup=soup,
            product_ld=product_ld,
        )

        category_name = (
            self._extract_category(
                soup=soup,
                product_ld=product_ld,
                fallback=(
                    fallback_category_name
                ),
            )
        )

        candidate_text = " ".join(
            value
            for value in (
                brand_name,
                name,
                category_name,
            )
            if value
        )

        if not self._is_relevant_candidate(
            query=query,
            candidate_text=candidate_text,
        ):
            logger.info(
                "Dixy product rejected: "
                "query=%r name=%r brand=%r "
                "category=%r anchors=%r",
                query,
                name,
                brand_name,
                category_name,
                self._query_anchor_tokens(
                    query
                ),
            )

            return None

        package_value, package_unit = (
            self._parse_package(
                name
            )
        )

        if package_value is None:
            package_text = (
                self._extract_label_value(
                    soup=soup,
                    labels=(
                        "Вес",
                        "Объем",
                        "Объём",
                        "Вес, объем",
                        "Вес, объём",
                        "Масса",
                    ),
                )
            )

            if package_text:
                (
                    package_value,
                    package_unit,
                ) = self._parse_package(
                    package_text
                )

        subtype = (
            self._extract_label_value(
                soup=soup,
                labels=(
                    "Вид",
                    "Подтип",
                ),
            )
            or self._detect_subtype(
                name
            )
        )

        description = (
            self._extract_description(
                soup=soup,
                product_ld=product_ld,
            )
        )

        image_url = self._extract_image(
            soup=soup,
            product_ld=product_ld,
        )

        barcode = self._extract_barcode(
            soup=soup,
            product_ld=product_ld,
        )

        article = (
            clean_external_text(
                product_ld.get(
                    "sku"
                )
            )
            or self._extract_label_value(
                soup=soup,
                labels=(
                    "Артикул",
                    "Код товара",
                ),
            )
        )

        source_id = (
            article
            or self._extract_source_id(
                url
            )
        )

        keyword_values: list[str] = []

        keyword_values.extend(
            self._tokens(
                name
            )
        )

        if brand_name:
            keyword_values.append(
                brand_name
            )

        if category_name:
            keyword_values.append(
                category_name
            )

        if subtype:
            keyword_values.append(
                subtype
            )

        score = self._score_name(
            query=query,
            name=candidate_text,
        )

        external_categories: list[str] = []

        if category_name:
            external_categories.append(
                category_name
            )

        if fallback_category_name:
            external_categories.append(
                fallback_category_name
            )

        for category_hint in (
            CATEGORY_HINTS.get(
                fallback_category_name or "",
                (),
            )
        ):
            external_categories.append(
                category_hint
            )

        normalized_categories: list[str] = []
        seen_categories: set[str] = set()

        for value in external_categories:
            cleaned = (
                clean_external_text(
                    value
                )
            )

            if not cleaned:
                continue

            key = self._normalize(
                cleaned
            )

            if key in seen_categories:
                continue

            seen_categories.add(
                key
            )

            normalized_categories.append(
                cleaned
            )

        product = ExternalProduct(
            provider=self.provider_name,
            source_id=str(
                source_id
            ),
            name=name,
            brand_name=brand_name,
            barcode=barcode,
            category_name=category_name,
            external_category_values=tuple(
                normalized_categories
            ),
            package_value=package_value,
            package_unit=package_unit,
            subtype=clean_external_text(
                subtype
            ),
            description=description,
            image_url=image_url,
            source_url=url,
            keywords=(
                normalize_external_keywords(
                    keyword_values
                )
            ),
            raw={
                "relevance_score": score,
                "article": article,
            },
        )

        logger.info(
            "Dixy product accepted: "
            "source_id=%s name=%r brand=%r "
            "category=%r barcode=%r "
            "package=%r %r score=%.2f",
            product.source_id,
            product.name,
            product.brand_name,
            product.category_name,
            product.barcode,
            product.package_value,
            product.package_unit,
            score,
        )

        return (
            max(
                preliminary_score,
                score,
            ),
            product,
        )

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> ExternalSearchResult:
        """
        Ищет товары в публичном каталоге Дикси.

        Если конкретная поисковая страница
        временно защищена или недоступна,
        провайдер корректно возвращает
        unavailable/пустой результат и
        не ломает остальные источники.
        """

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

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0.0.0 "
                "Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": (
                "ru-RU,ru;q=0.9,en;q=0.5"
            ),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

        fallback_category_name = (
            self._query_product_type(
                cleaned_query
            )
        )

        candidates_by_url: dict[
            str,
            tuple[
                float,
                str,
                str,
            ]
        ] = {}

        successful_pages = 0

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                search_urls = (
                    self._search_urls(
                        cleaned_query
                    )
                )

                for search_url in search_urls:
                    for page_number in range(
                        1,
                        MAX_SEARCH_PAGES + 1,
                    ):
                        if page_number == 1:
                            page_url = search_url
                        else:
                            separator = (
                                "&"
                                if "?" in search_url
                                else "?"
                            )

                            page_url = (
                                f"{search_url}"
                                f"{separator}"
                                f"page={page_number}"
                            )

                        html = await self._fetch_text(
                            session=session,
                            url=page_url,
                        )

                        if not html:
                            continue

                        successful_pages += 1

                        page_candidates = (
                            self._extract_product_links(
                                html=html,
                                query=cleaned_query,
                            )
                        )

                        logger.info(
                            "Dixy search page: "
                            "query=%r url=%s "
                            "candidates=%s",
                            cleaned_query,
                            page_url,
                            len(page_candidates),
                        )

                        for (
                            score,
                            name,
                            url,
                        ) in page_candidates:
                            previous = (
                                candidates_by_url.get(
                                    url
                                )
                            )

                            if (
                                previous is None
                                or score
                                > previous[0]
                            ):
                                candidates_by_url[
                                    url
                                ] = (
                                    score,
                                    name,
                                    url,
                                )

                candidates = list(
                    candidates_by_url.values()
                )

                candidates.sort(
                    key=lambda item: (
                        item[0],
                        len(item[1]),
                    ),
                    reverse=True,
                )

                candidates = candidates[
                    : max(
                        safe_limit * 5,
                        30,
                    )
                ]

                if not candidates:
                    logger.info(
                        "Dixy search: query=%r "
                        "successful_pages=%s "
                        "candidates=0",
                        cleaned_query,
                        successful_pages,
                    )

                    return ExternalSearchResult(
                        provider=self.provider_name,
                        query=cleaned_query,
                        products=(),
                        attempted=True,
                        unavailable=(
                            successful_pages == 0
                        ),
                        error=(
                            "Dixy public catalog unavailable"
                            if successful_pages == 0
                            else None
                        ),
                    )

                semaphore = asyncio.Semaphore(
                    MAX_CONCURRENT_DETAILS
                )

                tasks = [
                    self._load_product(
                        session=session,
                        semaphore=semaphore,
                        query=cleaned_query,
                        preliminary_name=name,
                        url=url,
                        preliminary_score=score,
                        fallback_category_name=(
                            fallback_category_name
                        ),
                    )
                    for (
                        score,
                        name,
                        url,
                    ) in candidates
                ]

                loaded = await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )

        except Exception as error:
            logger.exception(
                "Dixy provider search failed: "
                "query=%r",
                cleaned_query,
            )

            return ExternalSearchResult(
                provider=self.provider_name,
                query=cleaned_query,
                products=(),
                attempted=True,
                unavailable=True,
                error=str(
                    error
                ),
            )

        scored_products: list[
            tuple[
                float,
                ExternalProduct,
            ]
        ] = []

        rejected_count = 0

        for item in loaded:
            if isinstance(
                item,
                Exception,
            ):
                logger.warning(
                    "Dixy detail load failed: %r",
                    item,
                )
                continue

            if item is None:
                rejected_count += 1
                continue

            score, product = item

            candidate_text = " ".join(
                value
                for value in (
                    product.brand_name,
                    product.name,
                    product.category_name,
                )
                if value
            )

            if not self._is_relevant_candidate(
                query=cleaned_query,
                candidate_text=candidate_text,
            ):
                rejected_count += 1
                continue

            if score < 0.30:
                rejected_count += 1
                continue

            scored_products.append(
                (
                    score,
                    product,
                )
            )

        scored_products.sort(
            key=lambda item: (
                item[0],
                bool(
                    item[1].image_url
                ),
                bool(
                    item[1].brand_name
                ),
                bool(
                    item[1].barcode
                ),
                bool(
                    item[1].package_value
                ),
            ),
            reverse=True,
        )

        products: list[
            ExternalProduct
        ] = []

        seen_source_ids: set[str] = set()

        for (
            _score,
            product,
        ) in scored_products:
            if (
                product.source_id
                in seen_source_ids
            ):
                continue

            seen_source_ids.add(
                product.source_id
            )

            products.append(
                product
            )

            if len(products) >= safe_limit:
                break

        logger.info(
            "Dixy search finished: "
            "query=%r successful_pages=%s "
            "candidates=%s rejected=%s "
            "products=%s images=%s "
            "brands=%s barcodes=%s anchors=%r",
            cleaned_query,
            successful_pages,
            len(candidates),
            rejected_count,
            len(products),
            sum(
                1
                for product in products
                if product.image_url
            ),
            sum(
                1
                for product in products
                if product.brand_name
            ),
            sum(
                1
                for product in products
                if product.barcode
            ),
            self._query_anchor_tokens(
                cleaned_query
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

    async def get_product(
        self,
        source_id: str,
    ) -> ExternalProduct | None:
        """
        Прямой lookup по source_id пока
        не используется.

        Основной сценарий MarkaRadar:
        search() -> ExternalProduct ->
        Product Merge Engine.
        """

        return None
