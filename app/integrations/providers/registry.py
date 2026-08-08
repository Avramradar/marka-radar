from __future__ import annotations

from app.integrations.providers.base import (
    ExternalCatalogProvider,
)
from app.integrations.providers.metro import (
    MetroProvider,
)
from app.integrations.providers.openfoodfacts import (
    OpenFoodFactsProvider,
)


def build_default_providers(
) -> tuple[
    ExternalCatalogProvider,
    ...
]:
    """ Возвращает внешние каталоги MarkaRadar в порядке приоритета. Порядок сейчас такой: 1. OpenFoodFacts Хорош для структурированных данных и штрихкодов, но текстовый поиск иногда нестабилен. 2. METRO Используется как второй источник для текстового поиска и наполнения карточек товара. Если первый провайдер ничего не нашёл, ExternalCatalogService автоматически перейдёт к следующему. """

    return (
        OpenFoodFactsProvider(),
        MetroProvider(),
    )


def get_provider_names( providers: tuple[ ExternalCatalogProvider, ... ], ) -> tuple[
    str,
    ...
]:
    """ Возвращает имена подключённых провайдеров. """

    return tuple(
        provider.provider_name
        for provider in providers
    )


def get_provider_by_name( providers: tuple[ ExternalCatalogProvider, ... ], provider_name: str, ) -> ExternalCatalogProvider | None:
    """ Ищет конкретный провайдер по имени. """

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
