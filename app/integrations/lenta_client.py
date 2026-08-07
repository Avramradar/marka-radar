import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)

LENTA_BASE_URL = "https://lenta.com"
LENTA_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36 MarkaRadar/1.0"
)
REQUEST_TIMEOUT_SECONDS = 15
CACHE_TTL_SECONDS = 10 * 60
MAX_CATEGORY_PAGES = 2
MAX_CONCURRENT_DETAIL_REQUESTS = 4


@dataclass(slots=True, frozen=True)
class LentaCatalogProduct:
    source_id: str
    name: str
    url: str
    image_url: str | None
    brand: str | None
    category: str | None
    package_value: Decimal | None
    package_unit: str | None
    subtype: str | None
    description: str | None
    score: float


CATEGORY_ROUTES: dict[str, tuple[str, ...]] = {
    "кофе": (
        "/catalog/kofe-243/",
        "кофе",
        "coffee",
    ),
    "молоко": (
        "/catalog/moloko-128/",
        "молоко",
        "milk",
    ),
    "чай": (
        "/catalog/chajj-250/",
        "чай",
        "tea",
    ),
    "кефир": (
        "/catalog/kefir-18658/",
        "кефир",
        "kefir",
    ),
    "йогурт": (
        "/catalog/gustye-jjogurty-i-tvorozhki-19407/",
        "йогурт",
        "йогурты",
        "yogurt",
        "yoghurt",
    ),
    "вода": (
        "/catalog/voda-5/",
        "вода",
        "water",
    ),
    "пицца": (
        "/catalog/picca-22565/",
        "пицца",
        "pizza",
    ),
    "сельдь": (
        "/catalog/seld-1183/",
        "сельдь",
        "селедка",
        "селёдка",
        "herring",
    ),
}

PACKAGE_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(кг|kg|г|гр|g|л|l|мл|ml)(?!\w)",
    re.IGNORECASE,
)

SUBTYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Молотый", ("молотый", "ground coffee")),
    ("В зёрнах", ("зерновой", "в зернах", "в зёрнах", "coffee beans")),
    ("Растворимый", ("растворимый", "instant coffee")),
    ("В капсулах", ("капсульный", "в капсулах", "капсулы")),
    ("Пастеризованное", ("пастеризованное", "пастеризованный")),
    ("Ультрапастеризованное", ("ультрапастеризованное", "ультрапастеризованный")),
    ("Безлактозное", ("безлактозное", "без лактозы")),
    ("Слабосолёная", ("слабосоленая", "слабосолёная", "малосольная")),
    ("Филе", ("филе", "филе-кусочки", "филе кусочки")),
)


class LentaClient:
    """ Клиент публичного каталога lenta.com. Не использует скрытые мобильные API. Поиск строится через серверные HTML-страницы категорий, после чего наиболее релевантные карточки уточняются через публичные страницы /product/. """

    def __init__( self, *, timeout_seconds: int = REQUEST_TIMEOUT_SECONDS, ) -> None:
        self.timeout_seconds = max(
            5,
            min(timeout_seconds, 30),
        )
        self._cache: dict[str, tuple[float, str]] = {}

    @staticmethod
    def _normalize(value: Any) -> str:
        return " ".join(
            str(value or "")
            .strip()
            .lower()
            .replace("ё", "е")
            .split()
        )

    @classmethod
    def _tokens(cls, value: str) -> list[str]:
        return [
            token
            for token in re.findall(
                r"[a-zа-я0-9]+",
                cls._normalize(value),
            )
            if len(token) >= 2
        ]

    def _detect_route( self, query: str, ) -> tuple[str, str] | None:
        normalized_query = self._normalize(query)
        query_tokens = set(self._tokens(query))

        best: tuple[int, str, str] | None = None

        for category_name, values in CATEGORY_ROUTES.items():
            route = values[0]
            aliases = values[1:]

            for alias in aliases:
                normalized_alias = self._normalize(alias)
                alias_tokens = set(self._tokens(alias))

                score = 0

                if normalized_alias == normalized_query:
                    score = 100
                elif normalized_alias in normalized_query:
                    score = 50 + len(alias_tokens)
                elif (
                    alias_tokens
                    and alias_tokens <= query_tokens
                ):
                    score = 40 + len(alias_tokens)

                if (
                    score
                    and (
                        best is None
                        or score > best[0]
                    )
                ):
                    best = (
                        score,
                        category_name,
                        route,
                    )

        if best is None:
            return None

        return (
            best[1],
            best[2],
        )

    async def _fetch_text( self, *, session: aiohttp.ClientSession, url: str, ) -> str | None:
        cached = self._cache.get(url)
        now = time.monotonic()

        if (
            cached
            and now - cached[0] <= CACHE_TTL_SECONDS
        ):
            return cached[1]

        for attempt in range(2):
            try:
                async with session.get(
                    url,
                    allow_redirects=True,
                ) as response:
                    if response.status == 404:
                        return None

                    if (
                        response.status == 429
                        and attempt == 0
                    ):
                        await asyncio.sleep(0.8)
                        continue

                    response.raise_for_status()

                    text = await response.text()

                    self._cache[url] = (
                        now,
                        text,
                    )

                    return text

            except (
                aiohttp.ClientError,
                TimeoutError,
            ):
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue

                logger.exception(
                    "Lenta request failed: %s",
                    url,
                )

                return None

        return None

    @staticmethod
    def _extract_source_id( url: str, ) -> str:
        match = re.search(
            r"-(\d+)/?$",
            url,
        )

        if match:
            return match.group(1)

        return (
            url.rstrip("/")
            .rsplit("/", 1)[-1]
        )

    @classmethod
    def _parse_package( cls, text: str, ) -> tuple[Decimal | None, str | None]:
        matches = list(
            PACKAGE_PATTERN.finditer(text)
        )

        if not matches:
            return None, None

        match = matches[-1]

        raw_value = (
            match.group(1)
            .replace(",", ".")
        )

        raw_unit = (
            match.group(2)
            .lower()
        )

        try:
            value = Decimal(raw_value)
        except Exception:
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
            unit_map.get(raw_unit),
        )

    @classmethod
    def _detect_subtype( cls, text: str, ) -> str | None:
        normalized = cls._normalize(text)

        for title, terms in SUBTYPE_RULES:
            if any(
                cls._normalize(term) in normalized
                for term in terms
            ):
                return title

        return None

    @classmethod
    def _score_name( cls, *, query: str, name: str, ) -> float:
        query_tokens = cls._tokens(query)
        name_tokens = cls._tokens(name)

        if (
            not query_tokens
            or not name_tokens
        ):
            return 0.0

        name_set = set(name_tokens)

        matched = sum(
            1
            for token in query_tokens
            if token in name_set
        )

        partial = sum(
            1
            for token in query_tokens
            if (
                token not in name_set
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
            matched
            + 0.5 * partial
        ) / len(query_tokens)

        phrase_bonus = (
            0.25
            if (
                cls._normalize(query)
                in cls._normalize(name)
            )
            else 0.0
        )

        return min(
            1.0,
            coverage + phrase_bonus,
        )

    @classmethod
    def _extract_list_candidates( cls, *, html: str, query: str, category_name: str, ) -> list[
        tuple[
            float,
            str,
            str,
            str | None,
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
                str | None,
            ]
        ] = []

        seen_urls: set[str] = set()

        for anchor in soup.find_all(
            "a",
            href=True,
        ):
            href = str(
                anchor.get("href")
                or ""
            )

            if "/product/" not in href:
                continue

            url = urljoin(
                LENTA_BASE_URL,
                href,
            )

            if url in seen_urls:
                continue

            text = " ".join(
                anchor.stripped_strings
            ).strip()

            image = anchor.find("img")

            if (
                not text
                and image is not None
            ):
                text = str(
                    image.get("alt")
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
                    r"\s+Цена за 1"
                    r"|\s+С Картой"
                    r"|\s+В корзину"
                    r"|\s+\d+[\s\u00a0]*руб"
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

            if score < 0.34:
                continue

            image_url: str | None = None

            if image is not None:
                for attr in (
                    "src",
                    "data-src",
                    "data-original",
                ):
                    value = image.get(attr)

                    if value:
                        image_url = urljoin(
                            LENTA_BASE_URL,
                            str(value),
                        )
                        break

            seen_urls.add(url)

            candidates.append(
                (
                    score,
                    text,
                    url,
                    image_url,
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

    @staticmethod
    def _json_ld_products( soup: BeautifulSoup, ) -> list[dict[str, Any]]:
        result: list[
            dict[str, Any]
        ] = []

        for script in soup.find_all(
            "script",
            attrs={
                "type": "application/ld+json"
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
                data = json.loads(raw)
            except Exception:
                continue

            values = (
                data
                if isinstance(data, list)
                else [data]
            )

            for value in values:
                if not isinstance(
                    value,
                    dict,
                ):
                    continue

                if (
                    value.get("@type")
                    == "Product"
                ):
                    result.append(value)

                graph = value.get("@graph")

                if isinstance(
                    graph,
                    list,
                ):
                    result.extend(
                        item
                        for item in graph
                        if (
                            isinstance(
                                item,
                                dict,
                            )
                            and item.get(
                                "@type"
                            )
                            == "Product"
                        )
                    )

        return result

    @classmethod
    def _extract_label_value( cls, *, soup: BeautifulSoup, label: str, ) -> str | None:
        normalized_label = cls._normalize(
            label
        )

        strings = list(
            soup.stripped_strings
        )

        for index, value in enumerate(
            strings
        ):
            normalized_value = (
                cls._normalize(
                    value
                )
            )

            if (
                normalized_value
                == normalized_label
                and index + 1 < len(strings)
            ):
                candidate = (
                    strings[index + 1]
                    .strip()
                )

                if (
                    candidate
                    and len(candidate) <= 160
                ):
                    return candidate

            if normalized_value.startswith(
                normalized_label + " "
            ):
                candidate = (
                    value[
                        len(label):
                    ]
                    .strip(" :—-")
                )

                if candidate:
                    return candidate

        return None

    @classmethod
    def _extract_section( cls, *, soup: BeautifulSoup, heading: str, max_length: int = 1500, ) -> str | None:
        normalized_heading = cls._normalize(
            heading
        )

        for tag in soup.find_all(
            [
                "h2",
                "h3",
                "h4",
            ]
        ):
            if (
                cls._normalize(
                    tag.get_text(
                        " ",
                        strip=True,
                    )
                )
                != normalized_heading
            ):
                continue

            texts: list[str] = []

            node = (
                tag.find_next_sibling()
            )

            while node is not None:
                if (
                    getattr(
                        node,
                        "name",
                        None,
                    )
                    in {
                        "h2",
                        "h3",
                        "h4",
                    }
                ):
                    break

                chunk = " ".join(
                    node.stripped_strings
                ).strip()

                if chunk:
                    texts.append(
                        chunk
                    )

                if (
                    sum(
                        len(item)
                        for item in texts
                    )
                    >= max_length
                ):
                    break

                node = (
                    node.find_next_sibling()
                )

            result = " ".join(
                texts
            ).strip()

            if result:
                return result[
                    :max_length
                ]

        return None

    async def _load_detail( self, *, session: aiohttp.ClientSession, semaphore: asyncio.Semaphore, query: str, category_name: str, preliminary_name: str, url: str, preliminary_image: str | None, preliminary_score: float, ) -> LentaCatalogProduct | None:
        async with semaphore:
            html = await self._fetch_text(
                session=session,
                url=url,
            )

        if not html:
            (
                package_value,
                package_unit,
            ) = self._parse_package(
                preliminary_name
            )

            return LentaCatalogProduct(
                source_id=(
                    self._extract_source_id(
                        url
                    )
                ),
                name=preliminary_name,
                url=url,
                image_url=(
                    preliminary_image
                ),
                brand=None,
                category=category_name,
                package_value=(
                    package_value
                ),
                package_unit=(
                    package_unit
                ),
                subtype=(
                    self._detect_subtype(
                        preliminary_name
                    )
                ),
                description=None,
                score=preliminary_score,
            )

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        json_ld = (
            self._json_ld_products(
                soup
            )
        )

        product_ld = (
            json_ld[0]
            if json_ld
            else {}
        )

        h1 = soup.find("h1")

        name = (
            str(
                product_ld.get(
                    "name"
                )
                or ""
            ).strip()
            or (
                h1.get_text(
                    " ",
                    strip=True,
                )
                if h1
                else ""
            )
            or preliminary_name
        )

        brand: str | None = None

        raw_brand = product_ld.get(
            "brand"
        )

        if isinstance(
            raw_brand,
            dict,
        ):
            brand = (
                str(
                    raw_brand.get(
                        "name"
                    )
                    or ""
                ).strip()
                or None
            )

        elif isinstance(
            raw_brand,
            str,
        ):
            brand = (
                raw_brand.strip()
                or None
            )

        if not brand:
            brand = (
                self._extract_label_value(
                    soup=soup,
                    label="Бренд",
                )
            )

        image_url = (
            preliminary_image
        )

        raw_image = product_ld.get(
            "image"
        )

        if isinstance(
            raw_image,
            str,
        ):
            image_url = raw_image

        elif (
            isinstance(
                raw_image,
                list,
            )
            and raw_image
        ):
            image_url = str(
                raw_image[0]
            )

        if not image_url:
            meta = soup.find(
                "meta",
                attrs={
                    "property": "og:image"
                },
            )

            if (
                meta
                and meta.get(
                    "content"
                )
            ):
                image_url = urljoin(
                    LENTA_BASE_URL,
                    str(
                        meta.get(
                            "content"
                        )
                    ),
                )

        description = (
            str(
                product_ld.get(
                    "description"
                )
                or ""
            ).strip()
            or None
        )

        if not description:
            description = (
                self._extract_section(
                    soup=soup,
                    heading="Описание",
                )
            )

        (
            package_value,
            package_unit,
        ) = self._parse_package(name)

        subtype = self._detect_subtype(
            name
        )

        score = self._score_name(
            query=query,
            name=name,
        )

        if (
            brand
            and (
                self._normalize(
                    brand
                )
                in self._normalize(
                    query
                )
            )
        ):
            score = min(
                1.0,
                score + 0.12,
            )

        return LentaCatalogProduct(
            source_id=(
                self._extract_source_id(
                    url
                )
            ),
            name=name,
            url=url,
            image_url=image_url,
            brand=brand,
            category=category_name,
            package_value=package_value,
            package_unit=package_unit,
            subtype=subtype,
            description=description,
            score=max(
                preliminary_score,
                score,
            ),
        )

    async def search( self, query: str, *, limit: int = 8, ) -> list[LentaCatalogProduct]:
        cleaned_query = " ".join(
            str(query or "")
            .strip()
            .split()
        )

        if not cleaned_query:
            return []

        route = self._detect_route(
            cleaned_query
        )

        if route is None:
            logger.info(
                "Lenta: no category route "
                "for query=%r",
                cleaned_query,
            )
            return []

        (
            category_name,
            category_route,
        ) = route

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
                LENTA_USER_AGENT
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": (
                "ru-RU,ru;q=0.9,"
                "en;q=0.6"
            ),
        }

        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
        ) as session:
            candidates: list[
                tuple[
                    float,
                    str,
                    str,
                    str | None,
                ]
            ] = []

            seen: set[str] = set()

            for page in range(
                1,
                MAX_CATEGORY_PAGES + 1,
            ):
                page_url = urljoin(
                    LENTA_BASE_URL,
                    category_route,
                )

                if page > 1:
                    page_url = (
                        page_url.rstrip("/")
                        + f"/page/{page}"
                    )

                html = await self._fetch_text(
                    session=session,
                    url=page_url,
                )

                if not html:
                    continue

                for candidate in (
                    self._extract_list_candidates(
                        html=html,
                        query=cleaned_query,
                        category_name=(
                            category_name
                        ),
                    )
                ):
                    if candidate[2] in seen:
                        continue

                    seen.add(
                        candidate[2]
                    )

                    candidates.append(
                        candidate
                    )

            if not candidates:
                return []

            candidates.sort(
                key=lambda item: item[0],
                reverse=True,
            )

            detail_candidates = candidates[
                :max(
                    safe_limit * 2,
                    safe_limit,
                )
            ]

            semaphore = asyncio.Semaphore(
                MAX_CONCURRENT_DETAIL_REQUESTS
            )

            tasks = [
                self._load_detail(
                    session=session,
                    semaphore=semaphore,
                    query=cleaned_query,
                    category_name=(
                        category_name
                    ),
                    preliminary_name=name,
                    url=url,
                    preliminary_image=image,
                    preliminary_score=score,
                )
                for (
                    score,
                    name,
                    url,
                    image,
                ) in detail_candidates
            ]

            loaded = await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )

        products: list[
            LentaCatalogProduct
        ] = []

        for item in loaded:
            if isinstance(
                item,
                Exception,
            ):
                logger.warning(
                    "Lenta detail parse failed: %r",
                    item,
                )
                continue

            if item is None:
                continue

            if item.score < 0.34:
                continue

            products.append(
                item
            )

        products.sort(
            key=lambda item: (
                item.score,
                bool(item.image_url),
                bool(item.brand),
                len(item.name),
            ),
            reverse=True,
        )

        return products[
            :safe_limit
        ]
