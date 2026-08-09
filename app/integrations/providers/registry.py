from __future__ import annotations

from app.integrations.providers.base import (
    ExternalCatalogProvider,
)
from app.integrations.providers.dixy import (
    DixyProvider,
)
from app.integrations.providers.metro import (
    MetroProvider,
)
from app.integrations.providers.openfoodfacts import (
    OpenFoodFactsProvider,
)
from app.integrations.providers.pyaterochka import (
    PyaterochkaProvider,
)


def build_default_providers(
) -> tuple[
    ExternalCatalogProvider,
    ...
]:
    """
    Возвращает внешние каталоги MarkaRadar
    в порядке приоритета.

    Сейчас:

    1. OpenFoodFacts
       Структурированные данные и штрихкоды.

    2. METRO
       Публичный каталог с хорошими карточками.

    3. Пятёрочка
       Дополнительный источник российских товаров.

    4. Дикси
       Новый источник товарных карточек.
    """

    return (
        OpenFoodFactsProvider(),
        MetroProvider(),
        PyaterochkaProvider(),
        DixyProvider(),
    )


def get_provider_names(
    providers: tuple[
        ExternalCatalogProvider,
        ...
    ],
) -> tuple[
    str,
    ...
]:
    """
    Возвращает имена подключённых провайдеров.
    """

    return tuple(
        provider.provider_name
        for provider in providers
    )


def get_provider_by_name(
    providers: tuple[
        ExternalCatalogProvider,
        ...
    ],
    provider_name: str,
) -> ExternalCatalogProvider | None:
    """
    Ищет конкретный провайдер по имени.
    """

    normalized_name = (
        str(provider_name or "")
        .strip()
        .lower()
    )

    if not normalized_name:
        return None

    for provider in providers:
        if (
            provider.provider_name
            .strip()
            .lower()
            == normalized_name
        ):
            return provider

    return None
