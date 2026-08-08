from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass( slots=True, frozen=True, )
class ExternalProduct:
    """ Универсальная карточка товара из внешнего источника. Любой провайдер — OpenFoodFacts, Лента, Перекрёсток, Metro и т.д. — должен приводить свои данные к этой структуре. Важно: provider и source_id вместе должны позволять понять, откуда пришла карточка. """

    provider: str
    source_id: str

    name: str

    brand_name: str | None = None
    barcode: str | None = None

    category_name: str | None = None
    external_category_values: tuple[str, ...] = ()

    package_value: Decimal | None = None
    package_unit: str | None = None

    subtype: str | None = None
    description: str | None = None

    image_url: str | None = None
    source_url: str | None = None

    keywords: tuple[str, ...] = ()

    raw: dict[str, Any] = field(
        default_factory=dict,
    )


@dataclass( slots=True, frozen=True, )
class ExternalSearchResult:
    """ Результат поиска одного внешнего провайдера. """

    provider: str
    query: str

    products: tuple[
        ExternalProduct,
        ...
    ]

    attempted: bool = True
    unavailable: bool = False
    error: str | None = None

    @property
    def found_count( self, ) -> int:
        return len(
            self.products
        )

    @property
    def has_results( self, ) -> bool:
        return bool(
            self.products
        )


class ExternalCatalogProvider(
    ABC
):
    """ Общий интерфейс внешнего каталога MarkaRadar. Каждый источник реализует этот класс. Основная идея: запрос пользователя ↓ provider.search() ↓ ExternalProduct ↓ Product Merge Engine Search Pipeline не должен знать, как устроен конкретный магазин. """

    provider_name: str

    @abstractmethod
    async def search( self, query: str, *, limit: int = 8, ) -> ExternalSearchResult:
        """ Ищет товары по обычному текстовому запросу. Провайдер не должен сам сохранять товар в базу MarkaRadar. Он только возвращает ExternalProduct. """
        raise NotImplementedError

    async def get_by_barcode( self, barcode: str, ) -> ExternalProduct | None:
        """ Необязательный поиск по штрихкоду. """
        return None

    async def get_product( self, source_id: str, ) -> ExternalProduct | None:
        """ Необязательная загрузка полной карточки по идентификатору внешнего источника. """
        return None


def clean_external_text( value: Any, ) -> str | None:
    """ Базовая очистка текста для провайдеров. """

    if value is None:
        return None

    cleaned = " ".join(
        str(value)
        .strip()
        .split()
    )

    return cleaned or None


def normalize_external_barcode( value: Any, ) -> str | None:
    """ Оставляет в штрихкоде только цифры. """

    if value is None:
        return None

    barcode = "".join(
        character
        for character in str(
            value
        )
        if character.isdigit()
    )

    if not (
        8
        <= len(
            barcode
        )
        <= 14
    ):
        return None

    return barcode


def normalize_external_keywords( values: tuple[str, ...] | list[str], ) -> tuple[str, ...]:
    """ Удаляет пустые значения и дубли, сохраняя исходный порядок. """

    result: list[str] = []
    seen: set[str] = set()

    for value in values:
        cleaned = clean_external_text(
            value
        )

        if not cleaned:
            continue

        key = (
            cleaned
            .lower()
            .replace(
                "ё",
                "е",
            )
        )

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
