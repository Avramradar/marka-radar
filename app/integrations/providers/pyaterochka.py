from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin

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


PYATEROCHKA_BASE_URL = "https://5ka.ru"

REQUEST_TIMEOUT_SECONDS = 18
MAX_CONCURRENT_DETAILS = 4
MAX_CATEGORY_PAGES = 3


PACKAGE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(кг|kg|г|гр|g|л|l|мл|ml)"
    r"(?!\w)",
    re.IGNORECASE,
)


# Официальные страницы категорий 5ka.ru.
# Мы не зависим от закрытого API: сначала выбираем
# подходящую публичную категорию, затем извлекаем
# ссылки /product/... и проверяем релевантность.
CATEGORY_URLS: dict[str, tuple[str, ...]] = {
    "сметана": (
        "/catalog/smetana--251C39012/",
        "/catalog/smetana--251C51980/",
        "/catalog/tvorog-smetana--251C51979/",
    ),
    "молоко": (
        "/catalog/moloko--251C13165/",
        "/catalog/moloko-i-kefir--251C13513/",
        "/catalog/molochnye-produkty-yaytsa--251C51940/",
    ),
    "кефир": (
        "/catalog/kefir--251C51995/",
        "/catalog/kefir-ryazhenka--251C51994/",
        "/catalog/kefir-ayran--251C39015/",
    ),
    "сыр": (
        "/catalog/syr--251C39876/",
        "/catalog/syry--251C39635/",
        "/catalog/syr-i-tofu--251C13475/",
    ),
    "кофе": (
        "/catalog/chay-kofe-kakao--251C52956/",
        "/catalog/kofe-v-zyornakh--251C53140/",
        "/catalog/kofe-molotyy--251C53141/",
        "/catalog/rastvorimyy-kofe--251C53139/",
        "/catalog/kofe-v-kapsulakh--251C53142/",
    ),
    "чай": (
        "/catalog/chay--251C13516/",
        "/catalog/chay-kofe-kakao--251C52956/",
        "/catalog/chyornyy-chay--251C53143/",
        "/catalog/travyanoy-fruktovyy-chay--251C53145/",
    ),
}


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
    "макароны",
    "рис",
    "гречка",
    "мука",
    "сахар",
    "соль",
    "хлеб",
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
)


class PyaterochkaProvider(
    ExternalCatalogProvider
):
    """ Провайдер публичного каталога Пятёрочки. Использует только публичные HTML-страницы 5ka.ru: /catalog/... ↓ /product/... ↓ ExternalProduct Ключевое правило: конкретный запрос обязан сохранять уточняющие слова. Например: "Сметана Чабан" не может вернуть: "Сметана Простоквашино" "Сметана Вкуснотеево" только потому, что совпало слово "сметана". """

    provider_name = "pyaterochka"

    def __init__( self, *, timeout_seconds: int = REQUEST_TIMEOUT_SECONDS, ) -> None:
        self.timeout_seconds = max(
            5,
            min(
                int(timeout_seconds),
                45,
            ),
        )

    @staticmethod
    def _normalize( value: Any, ) -> str:
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
    def _tokens( cls, value: Any, ) -> list[str]:
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
    def _token_matches( query_token: str, candidate_token: str, ) -> bool:
        if query_token == candidate_token:
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
    def _query_anchor_tokens( cls, query: str, ) -> list[str]:
        generic = {
            cls._normalize(
                token
            )
            for token in (
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
    def _query_product_type( cls, query: str, ) -> str | None:
        tokens = cls._tokens(
            query
        )

        known_types = {
            cls._normalize(
                value
            )
            for value in CATEGORY_URLS
        }

        for token in tokens:
            if token in known_types:
                return token

        return None

    @classmethod
    def _query_token_coverage( cls, *, query: str, candidate_text: str, ) -> float:
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
                for candidate_token in candidate_tokens
            ):
                matched += 1

        return (
            matched
            / len(query_tokens)
        )

    @classmethod
    def _has_all_anchor_tokens( cls, *, query: str, candidate_text: str, ) -> bool:
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
                for candidate_token in candidate_tokens
            ):
                return False

        return True

    @classmethod
    def _is_relevant_candidate( cls, *, query: str, candidate_text: str, ) -> bool:
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

        coverage = cls._query_token_coverage(
            query=query,
            candidate_text=candidate_text,
        )

        if len(query_tokens) == 1:
            return coverage >= 1.0

        return coverage >= 0.50

    @classmethod
    def _score_name( cls, *, query: str, name: str, ) -> float:
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
            exact_match = (
                query_token
                in name_tokens
            )

            if exact_match:
                exact += 1
                continue

            if (
                len(query_token) >= 4
                and any(
                    cls._token_matches(
                        query_token,
                        name_token,
                    )
                    for name_token in name_tokens
                )
            ):
                partial += 1

        coverage = (
            exact
            + 0.55 * partial
        ) / len(
            query_tokens
        )

        query_normalized = cls._normalize(
            query
        )

        name_normalized = cls._normalize(
            name
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

        anchors = cls._query_anchor_tokens(
            query
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
    def _catalog_urls_for_query( cls, query: str, ) -> list[str]:
        product_type = cls._query_product_type(
            query
        )

        relative_urls = (
            CATEGORY_URLS.get(
                product_type,
                (),
            )
            if product_type
            else ()
        )

        urls = [
            urljoin(
                PYATEROCHKA_BASE_URL,
                relative_url,
            )
            for relative_url in relative_urls
        ]

        # Если тип пока не поддержан отдельной
        # категорией, используем корень каталога.
        if not urls:
            urls.append(
                urljoin(
                    PYATEROCHKA_BASE_URL,
                    "/catalog/",
                )
            )

        return urls

    @staticmethod
    def _extract_source_id( url: str, ) -> str:
        path = (
            url.split(
                "?",
                1,
            )[0]
            .rstrip("/")
        )

        slug = path.rsplit(
            "/",
            1,
        )[-1]

        numeric_match = re.search(
            r"--(\d+)$",
            slug,
        )

        if numeric_match:
            return numeric_match.group(
                1
            )

        return slug

    @classmethod
    def _parse_package( cls, text: str, ) -> tuple[
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
    def _detect_subtype( cls, text: str, ) -> str | None:
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

    async def _fetch_text( self, *, session: aiohttp.ClientSession, url: str, ) -> str | None:
        for attempt in range(2):
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                ) as response:
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
                        logger.info(
                            "Pyaterochka temporary response: "
                            "status=%s url=%s",
                            response.status,
                            url,
                        )

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
                    "Pyaterochka request failed: %s",
                    url,
                )

                return None

        return None

    @staticmethod
    def _json_ld_values( soup: BeautifulSoup, ) -> list[
        dict[str, Any]
    ]:
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
    def _json_ld_product( cls, soup: BeautifulSoup, ) -> dict[
        str,
        Any
    ]:
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
    def _extract_product_links( cls, *, html: str, query: str, ) -> list[
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

            if "/product/" not in href:
                continue

            url = urljoin(
                PYATEROCHKA_BASE_URL,
                href,
            )

            clean_url = url.split(
                "?",
                1,
            )[0]

            if clean_url in seen:
                continue

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
                    r"|\s+Нет в наличии"
                    r"|\s+\d[\d\s]*[₽р]"
                ),
                text,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()

            if len(text) < 4:
                # URL всё равно может быть полезен:
                # окончательное имя возьмём со страницы.
                text = cls._extract_source_id(
                    clean_url
                )

            score = cls._score_name(
                query=query,
                name=text,
            )

            seen.add(
                clean_url
            )

            candidates.append(
                (
                    score,
                    text,
                    clean_url,
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
    def _extract_label_value( cls, *, soup: BeautifulSoup, labels: tuple[ str, ... ], ) -> str | None:
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
                    <= 300
                ):
                    return candidate

            for label in labels:
                normalized_label = (
                    cls._normalize(
                        label
                    )
                )

                if normalized_value.startswith(
                    normalized_label + " "
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
    def _extract_description( cls, *, soup: BeautifulSoup, product_ld: dict[ str, Any ], ) -> str | None:
        description = clean_external_text(
            product_ld.get(
                "description"
            )
        )

        if description:
            return description[
                :1500
            ]

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
            description = clean_external_text(
                meta.get(
                    "content"
                )
            )

            if description:
                return description[
                    :1500
                ]

        return cls._extract_label_value(
            soup=soup,
            labels=(
                "Описание",
            ),
        )

    @classmethod
    def _extract_image( cls, *, soup: BeautifulSoup, product_ld: dict[ str, Any ], ) -> str | None:
        image = product_ld.get(
            "image"
        )

        if isinstance(
            image,
            str,
        ):
            return urljoin(
                PYATEROCHKA_BASE_URL,
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
                PYATEROCHKA_BASE_URL,
                str(
                    image[0]
                ),
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
                PYATEROCHKA_BASE_URL,
                str(
                    meta.get(
                        "content"
                    )
                ),
            )

        return None

    @classmethod
    def _extract_brand( cls, *, soup: BeautifulSoup, product_ld: dict[ str, Any ], ) -> str | None:
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
                "Бренд",
                "Торговая марка",
            ),
        )

    @classmethod
    def _extract_barcode( cls, *, soup: BeautifulSoup, product_ld: dict[ str, Any ], ) -> str | None:
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

            if raw:
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

    async def _load_product( self, *, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, query: str, preliminary_name: str, url: str, preliminary_score: float, fallback_category_name: str | None, ) -> tuple[
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

        product_ld = self._json_ld_product(
            soup
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

        brand_name = self._extract_brand(
            soup=soup,
            product_ld=product_ld,
        )

        category_name = (
            clean_external_text(
                product_ld.get(
                    "category"
                )
            )
            or self._extract_label_value(
                soup=soup,
                labels=(
                    "Категория",
                    "Тип",
                ),
            )
            or fallback_category_name
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
                "Pyaterochka product rejected by relevance: "
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
                    ),
                )
            )

            if package_text:
                package_value, package_unit = (
                    self._parse_package(
                        package_text
                    )
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

        sku = (
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
            sku
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

        product = ExternalProduct(
            provider=self.provider_name,
            source_id=str(
                source_id
            ),
            name=name,
            brand_name=brand_name,
            barcode=barcode,
            category_name=category_name,
            external_category_values=(
                (
                    category_name,
                )
                if category_name
                else ()
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
            },
        )

        return (
            max(
                preliminary_score,
                score,
            ),
            product,
        )

    async def search( self, query: str, *, limit: int = 8, ) -> ExternalSearchResult:
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
                "(Linux; Android 13) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/124.0 Mobile Safari/537.36"
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
        }

        product_type = self._query_product_type(
            cleaned_query
        )

        catalog_urls = self._catalog_urls_for_query(
            cleaned_query
        )

        candidates_by_url: dict[
            str,
            tuple[
                float,
                str,
                str,
            ]
        ] = {}

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                for catalog_url in catalog_urls:
                    for page_number in range(
                        1,
                        MAX_CATEGORY_PAGES + 1,
                    ):
                        if page_number == 1:
                            page_url = catalog_url
                        else:
                            separator = (
                                "&"
                                if "?" in catalog_url
                                else "?"
                            )

                            page_url = (
                                f"{catalog_url}"
                                f"{separator}"
                                f"page={page_number}"
                            )

                        html = await self._fetch_text(
                            session=session,
                            url=page_url,
                        )

                        if not html:
                            continue

                        page_candidates = (
                            self._extract_product_links(
                                html=html,
                                query=cleaned_query,
                            )
                        )

                        if not page_candidates:
                            continue

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

                # Не грузим сотни карточек:
                # берём верхний запас, а затем
                # применяем строгий relevance-фильтр.
                candidates = candidates[
                    : max(
                        safe_limit * 4,
                        24,
                    )
                ]

                if not candidates:
                    logger.info(
                        "Pyaterochka search: query=%r "
                        "candidates=0 catalog_urls=%r",
                        cleaned_query,
                        catalog_urls,
                    )

                    return ExternalSearchResult(
                        provider=self.provider_name,
                        query=cleaned_query,
                        products=(),
                        attempted=True,
                        unavailable=False,
                        error=None,
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
                            product_type
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
                "Pyaterochka provider search failed: "
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
                    "Pyaterochka detail load failed: %r",
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
            "Pyaterochka search: query=%r "
            "candidates=%s rejected=%s "
            "products=%s with_images=%s "
            "with_barcodes=%s anchors=%r "
            "catalog_urls=%r",
            cleaned_query,
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
                if product.barcode
            ),
            self._query_anchor_tokens(
                cleaned_query
            ),
            catalog_urls,
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

    async def get_product( self, source_id: str, ) -> ExternalProduct | None:
        """ Прямой lookup по source_id пока не нужен. Основная цепочка MarkaRadar работает через search(). """

        return None
