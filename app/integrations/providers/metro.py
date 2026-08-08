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


METRO_BASE_URL = "https://online.metro-cc.ru"
METRO_SEARCH_URL = (
    "https://online.metro-cc.ru/search?q={query}"
)

REQUEST_TIMEOUT_SECONDS = 18
MAX_CONCURRENT_DETAILS = 4


PACKAGE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d+(?:[.,]\d+)?)\s*"
    r"(кг|kg|г|гр|g|л|l|мл|ml)"
    r"(?!\w)",
    re.IGNORECASE,
)


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


# Общие слова не должны сами по себе делать карточку
# релевантной конкретному запросу.
#
# Например:
#
# "Сметана Чабан"
#
# слово "сметана" описывает тип продукта.
# Главный уточняющий токен здесь — "чабан".
#
# Если METRO вернул "Сметана Простоквашино",
# такой товар не должен импортироваться.
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
}


class MetroProvider(
    ExternalCatalogProvider
):
    """ Провайдер публичного каталога METRO. Схема: /search?q=... ↓ ссылки /products/... ↓ карточки товара ↓ проверка релевантности ↓ ExternalProduct Важное правило: конкретный запрос не должен превращаться в импорт любых товаров того же типа. Например: "Сметана Чабан" не должен импортировать: "Сметана Простоквашино" "Сметана Экомилк" "Сметана Parmalat" """

    provider_name = "metro"

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
        """ Безопасное совпадение одного токена. Точное совпадение всегда принимается. Частичное совпадение разрешаем только для слов длиной >= 4, чтобы поддерживать небольшие различия форм. """

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
        """ Возвращает уточняющие токены запроса. Примеры: "сметана чабан" -> ["чабан"] "кофе poetti" -> ["poetti"] "poetti leggenda" -> ["poetti", "leggenda"] "растворимый кофе" -> [] Если anchor есть, карточка обязана содержать его. """

        generic = {
            cls._normalize(
                token
            )
            for token in (
                GENERIC_QUERY_TOKENS
                | GENERIC_MODIFIER_TOKENS
            )
        }

        result: list[str] = []

        for token in cls._tokens(
            query
        ):
            normalized_token = cls._normalize(
                token
            )

            if normalized_token in generic:
                continue

            result.append(
                normalized_token
            )

        return result

    @classmethod
    def _query_token_coverage( cls, *, query: str, candidate_text: str, ) -> float:
        """ Доля значимых слов запроса, найденных в карточке. """

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
        """ Для конкретного запроса требует, чтобы все уточняющие слова присутствовали в карточке. Это главный фильтр от нерелевантного импорта. """

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
        """ Финальная проверка релевантности. Правила: 1. Все anchor-токены конкретного запроса обязаны присутствовать. 2. Для многословного запроса должно совпасть не меньше половины токенов. 3. Для широкого запроса без anchor достаточно обычного совпадения типа. """

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

        anchors = cls._query_anchor_tokens(
            query
        )

        if len(query_tokens) == 1:
            return coverage >= 1.0

        if anchors:
            return coverage >= 0.50

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

        name_set = set(
            name_tokens
        )

        exact = sum(
            1
            for token in query_tokens
            if token in name_set
        )

        partial = sum(
            1
            for token in query_tokens
            if (
                token not in name_set
                and len(token) >= 4
                and any(
                    (
                        token in name_token
                        or name_token in token
                    )
                    for name_token in name_tokens
                    if len(name_token) >= 4
                )
            )
        )

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

        if cls._has_all_anchor_tokens(
            query=query,
            candidate_text=name,
        ):
            anchors = cls._query_anchor_tokens(
                query
            )

            if anchors:
                anchor_bonus = 0.20

        return min(
            1.0,
            coverage
            + phrase_bonus
            + anchor_bonus,
        )

    @staticmethod
    def _extract_source_id( url: str, ) -> str:
        slug = (
            url.split(
                "?",
                1,
            )[0]
            .rstrip("/")
            .rsplit(
                "/",
                1,
            )[-1]
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
        """ Делает публичный HTML-запрос к METRO. 401/403/429/5xx считаются временной недоступностью источника. """

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
                            "METRO temporary response: "
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
                    "METRO request failed: %s",
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
    def _extract_search_links( cls, *, html: str, query: str, limit: int, ) -> list[
        tuple[
            float,
            str,
            str,
        ]
    ]:
        """ Извлекает уникальные ссылки /products/ из поисковой страницы. На этом раннем этапе фильтр мягкий: окончательная проверка выполняется после загрузки полной карточки. """

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

        seen: set[
            str
        ] = set()

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

            if "/products/" not in href:
                continue

            url = urljoin(
                METRO_BASE_URL,
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

                    parent = (
                        parent.parent
                    )

                    hops += 1

            text = re.split(
                (
                    r"\s+В корзину"
                    r"|\s+В торговом центре"
                    r"|\s+Товара "
                    r"|\s+\d[\d\s]*[₽р]"
                ),
                text,
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0].strip()

            if len(text) < 4:
                continue

            score = cls._score_name(
                query=query,
                name=text,
            )

            # Здесь оставляем более мягкий порог,
            # потому что полная карточка может
            # содержать бренд, которого нет в
            # тексте ссылки поисковой выдачи.
            if score < 0.20:
                continue

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

        # Берём небольшой запас, чтобы после
        # строгой проверки осталось до limit
        # действительно релевантных карточек.
        return candidates[
            : max(
                limit * 3,
                limit,
            )
        ]

    @classmethod
    def _extract_label_value( cls, *, soup: BeautifulSoup, labels: tuple[ str, ... ], ) -> str | None:
        """ Ищет значение характеристики после подписи вроде "Бренд". """

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
                    <= 200
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
    def _extract_description( cls, soup: BeautifulSoup, product_ld: dict[ str, Any ], ) -> str | None:
        raw = clean_external_text(
            product_ld.get(
                "description"
            )
        )

        if raw:
            return raw

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
                return value

        for heading in soup.find_all(
            [
                "h2",
                "h3",
                "h4",
            ]
        ):
            title = cls._normalize(
                heading.get_text(
                    " ",
                    strip=True,
                )
            )

            if title not in {
                "описание",
                "состав",
            }:
                continue

            values: list[
                str
            ] = []

            node = (
                heading.find_next_sibling()
            )

            while node is not None:
                if getattr(
                    node,
                    "name",
                    None,
                ) in {
                    "h2",
                    "h3",
                    "h4",
                }:
                    break

                text = " ".join(
                    node.stripped_strings
                ).strip()

                if text:
                    values.append(
                        text
                    )

                if sum(
                    len(item)
                    for item in values
                ) >= 1200:
                    break

                node = (
                    node.find_next_sibling()
                )

            description = " ".join(
                values
            ).strip()

            if description:
                return description[
                    :1200
                ]

        return None

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
                METRO_BASE_URL,
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
                METRO_BASE_URL,
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
                METRO_BASE_URL,
                str(
                    meta.get(
                        "content"
                    )
                ),
            )

        return None

    async def _load_product( self, *, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, query: str, preliminary_name: str, url: str, preliminary_score: float, ) -> tuple[
        float,
        ExternalProduct,
    ] | None:
        async with semaphore:
            html = await self._fetch_text(
                session=session,
                url=url,
            )

        if not html:
            # Без полной страницы мы не можем
            # надёжно проверить бренд конкретного
            # запроса. Поэтому предварительную
            # карточку принимаем только если
            # сама строка выдачи уже релевантна.
            if not self._is_relevant_candidate(
                query=query,
                candidate_text=preliminary_name,
            ):
                return None

            package_value, package_unit = (
                self._parse_package(
                    preliminary_name
                )
            )

            product = ExternalProduct(
                provider=self.provider_name,
                source_id=(
                    self._extract_source_id(
                        url
                    )
                ),
                name=preliminary_name,
                package_value=package_value,
                package_unit=package_unit,
                subtype=(
                    self._detect_subtype(
                        preliminary_name
                    )
                ),
                source_url=url,
                keywords=(
                    normalize_external_keywords(
                        self._tokens(
                            preliminary_name
                        )
                    )
                ),
                raw={},
            )

            return (
                preliminary_score,
                product,
            )

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

        raw_brand = product_ld.get(
            "brand"
        )

        brand_name: str | None = None

        if isinstance(
            raw_brand,
            dict,
        ):
            brand_name = (
                clean_external_text(
                    raw_brand.get(
                        "name"
                    )
                )
            )

        elif isinstance(
            raw_brand,
            str,
        ):
            brand_name = (
                clean_external_text(
                    raw_brand
                )
            )

        if not brand_name:
            brand_name = (
                self._extract_label_value(
                    soup=soup,
                    labels=(
                        "Бренд",
                    ),
                )
            )

        category_name = (
            self._extract_label_value(
                soup=soup,
                labels=(
                    "Тип",
                    "Категория",
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

        # КЛЮЧЕВОЙ ФИЛЬТР:
        # конкретный запрос обязан совпадать
        # с полной карточкой.
        if not self._is_relevant_candidate(
            query=query,
            candidate_text=candidate_text,
        ):
            logger.info(
                "METRO product rejected by relevance: "
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
            raw_weight = (
                self._extract_label_value(
                    soup=soup,
                    labels=(
                        "Вес, объем",
                        "Вес, объём",
                    ),
                )
            )

            raw_unit = (
                self._extract_label_value(
                    soup=soup,
                    labels=(
                        "Единица измерения",
                        "Ед. изм.",
                    ),
                )
            )

            if raw_weight:
                package_value, parsed_unit = (
                    self._parse_package(
                        (
                            f"{raw_weight} "
                            f"{raw_unit or ''}"
                        )
                    )
                )

                package_unit = (
                    package_unit
                    or parsed_unit
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

        image_url = (
            self._extract_image(
                soup=soup,
                product_ld=product_ld,
            )
        )

        description = (
            self._extract_description(
                soup,
                product_ld,
            )
        )

        sku = (
            self._extract_label_value(
                soup=soup,
                labels=(
                    "Артикул",
                ),
            )
        )

        source_id = (
            clean_external_text(
                sku
            )
            or self._extract_source_id(
                url
            )
        )

        keyword_values: list[
            str
        ] = []

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
            source_id=source_id,
            name=name,
            brand_name=brand_name,
            barcode=None,
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
        """ Ищет товары в публичном каталоге METRO. После загрузки карточек применяется строгий relevance-фильтр. """

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

        search_url = (
            METRO_SEARCH_URL.format(
                query=quote_plus(
                    cleaned_query
                )
            )
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

        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers=headers,
            ) as session:
                html = await self._fetch_text(
                    session=session,
                    url=search_url,
                )

                if not html:
                    return ExternalSearchResult(
                        provider=self.provider_name,
                        query=cleaned_query,
                        products=(),
                        attempted=True,
                        unavailable=True,
                        error=(
                            "METRO search page unavailable"
                        ),
                    )

                candidates = (
                    self._extract_search_links(
                        html=html,
                        query=cleaned_query,
                        limit=safe_limit,
                    )
                )

                if not candidates:
                    logger.info(
                        "METRO search: query=%r "
                        "candidates=0",
                        cleaned_query,
                    )

                    return ExternalSearchResult(
                        provider=self.provider_name,
                        query=cleaned_query,
                        products=(),
                        attempted=True,
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
                "METRO provider search failed: "
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
                    "METRO detail load failed: %r",
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

            # Повторная защита перед импортом.
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
                    item[1].package_value
                ),
            ),
            reverse=True,
        )

        products: list[
            ExternalProduct
        ] = []

        seen_source_ids: set[
            str
        ] = set()

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

            if len(
                products
            ) >= safe_limit:
                break

        logger.info(
            "METRO search: query=%r "
            "candidates=%s rejected=%s "
            "products=%s with_images=%s "
            "anchors=%r",
            cleaned_query,
            len(candidates),
            rejected_count,
            len(products),
            sum(
                1
                for product in products
                if product.image_url
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

    async def get_product( self, source_id: str, ) -> ExternalProduct | None:
        """ Прямой lookup METRO пока не используется. Основная цепочка работает через search(). """

        return None
