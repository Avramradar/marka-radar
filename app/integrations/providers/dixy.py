from __future__ import annotations

import logging

from app.integrations.providers.base import (
    ExternalCatalogProvider,
    ExternalProduct,
    ExternalSearchResult,
    clean_external_text,
)


logger = logging.getLogger(__name__)


DIXY_BASE_URL = "https://dixy.ru"


class DixyProvider(
    ExternalCatalogProvider
):
    """
    Провайдер публичного каталога Дикси.

    На первом этапе это минимальный каркас.

    Задачи следующего этапа:

    - получение публичного каталога;
    - поиск товаров;
    - извлечение названия;
    - бренда;
    - категории;
    - веса / объёма;
    - изображения;
    - ссылки на источник;
    - передача результата в ExternalProduct.

    Цены и наличие не считаются глобальными,
    поскольку ассортимент Дикси может зависеть
    от выбранного магазина / региона.
    """

    provider_name = "dixy"

    async def search(
        self,
        query: str,
        *,
        limit: int = 8,
    ) -> ExternalSearchResult:
        """
        Поиск товаров Дикси.

        Пока возвращает пустой результат.
        Это позволяет безопасно подключить
        провайдер к общей архитектуре.
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

        logger.info(
            "Dixy search stub: "
            "query=%r limit=%s",
            cleaned_query,
            safe_limit,
        )

        return ExternalSearchResult(
            provider=self.provider_name,
            query=cleaned_query,
            products=(),
            attempted=True,
            unavailable=False,
            error=None,
        )

    async def get_product(
        self,
        source_id: str,
    ) -> ExternalProduct | None:
        """
        Прямой lookup пока не используется.

        Основная цепочка MarkaRadar
        будет работать через search().
        """

        return None
