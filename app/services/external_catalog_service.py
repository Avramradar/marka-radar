from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.providers.base import (
    ExternalCatalogProvider,
)
from app.integrations.providers.registry import (
    build_default_providers,
)
from app.services.provider_import_service import (
    ProviderBatchImportResult,
    search_and_import_provider,
)


logger = logging.getLogger(__name__)


@dataclass( slots=True, frozen=True, )
class ExternalCatalogServiceResult:
    """ Итог работы всех внешних провайдеров по одному пользовательскому запросу. """

    query: str

    providers_attempted: tuple[
        str,
        ...
    ]

    total_found: int
    total_imported: int
    total_skipped: int
    total_failed: int

    provider_results: tuple[
        ProviderBatchImportResult,
        ...
    ]

@property
    def enriched( self, ) -> bool:
        return (
            self.total_imported
            > 0
        )


class ExternalCatalogService:
    """ Единая точка работы MarkaRadar со всеми внешними товарными каталогами. Search Pipeline и handlers не должны знать, как устроены OpenFoodFacts, Лента, Перекрёсток или Metro. Они вызывают только этот сервис. Алгоритм: query ↓ providers registry ↓ provider.search() ↓ Provider Import Service ↓ Product Merge Engine ↓ MarkaRadar DB """

    def __init__( self, *, providers: tuple[ ExternalCatalogProvider, ... ] | None = None, ) -> None:
        self.providers = (
            providers
            if providers is not None
            else build_default_providers()
        )

    async def search_and_enrich( self, *, session: AsyncSession, query: str, limit_per_provider: int = 8, stop_after_success: bool = False, commit: bool = True, ) -> ExternalCatalogServiceResult:
        """ Ищет товары по всем подключённым источникам и импортирует полезные карточки. stop_after_success=False: пройти по всем провайдерам. stop_after_success=True: остановиться после первого источника, который реально импортировал товары. Пока провайдер один, разницы нет. Позже этот флаг пригодится для управления нагрузкой на Ленту / Перекрёсток / Metro. """

        cleaned_query = " ".join(
            str(query or "")
            .strip()
            .split()
        )

        if not cleaned_query:
            return ExternalCatalogServiceResult(
                query="",
                providers_attempted=(),
                total_found=0,
                total_imported=0,
                total_skipped=0,
                total_failed=0,
                provider_results=(),
            )

        safe_limit = max(
            1,
            min(
                int(limit_per_provider),
                20,
            ),
        )

        provider_results: list[
            ProviderBatchImportResult
        ] = []

        providers_attempted: list[
            str
        ] = []

        total_found = 0
        total_imported = 0
        total_skipped = 0
        total_failed = 0

        for provider in self.providers:
            providers_attempted.append(
                provider.provider_name
            )

            logger.info(
                "External catalog provider start: "
                "provider=%s query=%r",
                provider.provider_name,
                cleaned_query,
            )

            try:
                result = (
                    await search_and_import_provider(
                        session=session,
                        provider=provider,
                        query=cleaned_query,
                        limit=safe_limit,
                        commit=False,
                    )
                )

            except Exception:
                logger.exception(
                    "External provider failed: "
                    "provider=%s query=%r",
                    provider.provider_name,
                    cleaned_query,
                )

                continue

            provider_results.append(
                result
            )

            total_found += (
                result.found_count
            )

            total_imported += (
                result.imported_count
            )

            total_skipped += (
                result.skipped_count
            )

            total_failed += (
                result.failed_count
            )

            logger.info(
                "External catalog provider done: "
                "provider=%s query=%r "
                "found=%s imported=%s "
                "skipped=%s failed=%s",
                result.provider,
                cleaned_query,
                result.found_count,
                result.imported_count,
                result.skipped_count,
                result.failed_count,
            )

            if (
                stop_after_success
                and result.imported_count > 0
            ):
                break

        if (
            commit
            and total_imported > 0
        ):
            await session.commit()

        logger.info(
            "External catalog complete: "
            "query=%r providers=%s "
            "found=%s imported=%s "
            "skipped=%s failed=%s",
            cleaned_query,
            providers_attempted,
            total_found,
            total_imported,
            total_skipped,
            total_failed,
        )

        return ExternalCatalogServiceResult(
            query=cleaned_query,
            providers_attempted=tuple(
                providers_attempted
            ),
            total_found=total_found,
            total_imported=total_imported,
            total_skipped=total_skipped,
            total_failed=total_failed,
            provider_results=tuple(
                provider_results
            ),
        )


_default_service: (
    ExternalCatalogService
    | None
) = None


def get_external_catalog_service(
) -> ExternalCatalogService:
    """ Возвращает общий экземпляр сервиса. Это позволяет переиспользовать клиенты провайдеров и их локальные кэши между пользовательскими запросами. """

    global _default_service

    if _default_service is None:
        _default_service = (
            ExternalCatalogService()
        )

    return _default_service


async def enrich_catalog( *, session: AsyncSession, query: str, limit_per_provider: int = 8, stop_after_success: bool = False, commit: bool = True, ) -> ExternalCatalogServiceResult:
    """ Удобная функциональная точка входа. В дальнейшем handlers смогут вызывать: result = await enrich_catalog( session=session, query=query, ) не импортируя конкретные провайдеры. """

    service = (
        get_external_catalog_service()
    )

    return await service.search_and_enrich(
        session=session,
        query=query,
        limit_per_provider=(
            limit_per_provider
        ),
        stop_after_success=(
            stop_after_success
        ),
        commit=commit,
    )
