from __future__ import annotations

from app.integrations.providers.base import (
    ExternalCatalogProvider,
)
from app.integrations.providers.openfoodfacts import (
    OpenFoodFactsProvider,
)


def build_default_providers(
) -> tuple[
    ExternalCatalogProvider,
    ...
]:
    """ Возвращает провайдеры MarkaRadar в порядке приоритета. Сейчас подключён только OpenFoodFacts. Позже сюда без изменения остальной архитектуры добавятся: LentaProvider() PerekrestokProvider() MetroProvider() Порядок важен: более стабильные и структурированные источники должны идти раньше. """

    return (
        OpenFoodFactsProvider(),
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
