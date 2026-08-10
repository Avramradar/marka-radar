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
from app.services.product_card_enrichment_service import (
    completeness_log_payload,
    evaluate_product_card_state,
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


def _imported_product_ids( result: ProviderBatchImportResult, ) -> tuple[
    int,
    ...
]:
    """ Извлекает уникальные product_id, которые реально прошли Product Merge Engine. Важно: один провайдер может вернуть несколько внешних карточек одного и того же товара. Поэтому дубли product_id удаляются. """

    product_ids: list[int] = []
    seen: set[int] = set()

    for item in result.items:
        merge_result = item.merge_result

        if merge_result is None:
            continue

        product = merge_result.product

        product_id = getattr(
            product,
            "id",
            None,
        )

        if product_id is None:
            continue

        product_id = int(
            product_id
        )

        if product_id in seen:
            continue

        seen.add(
            product_id
        )

        product_ids.append(
            product_id
        )

    return tuple(
        product_ids
    )


async def _observe_product_completeness( *, session: AsyncSession, provider_name: str, result: ProviderBatchImportResult, ) -> None:
    """ Диагностический режим. После каждого провайдера оценивает состояние всех карточек, которых этот провайдер коснулся. НИЧЕГО: - не удаляет; - не меняет; - не останавливает; - не коммитит; - не выбирает вместо Merge Engine. То есть сейчас это только наблюдение за реальным процессом формирования карточек. Внешнее изображение здесь намеренно НЕ проверяется сетью, чтобы диагностический слой не замедлял каждый пользовательский поиск. Отдельный Image Validator уже существует. Его подключим к управляющему процессу позже, когда убедимся, что progression карточки по источникам работает правильно. """

    product_ids = (
        _imported_product_ids(
            result
        )
    )

    if not product_ids:
        return

    for product_id in product_ids:
        try:
            state = (
                await evaluate_product_card_state(
                    session=session,
                    product_id=product_id,
                    validate_image=False,
                )
            )

        except Exception:
            logger.exception(
                "Card completeness observation failed: "
                "provider=%s product_id=%s",
                provider_name,
                product_id,
            )
            continue

        payload = (
            completeness_log_payload(
                state
            )
        )

        logger.info(
            "Card completeness after provider: "
            "provider=%s "
            "product_id=%s "
            "score=%s "
            "identity=%s "
            "presentation=%s "
            "complete=%s "
            "continue=%s "
            "missing=%s "
            "weak=%s "
            "critical=%s "
            "next=%s",
            provider_name,
            payload["product_id"],
            payload["score"],
            payload["identity_score"],
            payload["presentation_score"],
            payload["is_complete"],
            payload["should_continue"],
            payload["missing_fields"],
            payload["weak_fields"],
            payload["critical_missing_fields"],
            payload["next_priority_fields"],
        )


class ExternalCatalogService:
    """ Единая точка работы MarkaRadar со всеми внешними товарными каталогами. Текущий режим: provider ↓ Provider Import Service ↓ Product Merge Engine ↓ наблюдение полноты карточки ↓ следующий provider ВАЖНО: на этом этапе полнота карточки ещё НЕ влияет на остановку провайдеров. Сначала собираем реальные логи и убеждаемся, что score и missing_fields ведут себя правильно. После этого включим stop_after_complete. """

    def __init__( self, *, providers: tuple[ ExternalCatalogProvider, ... ] | None = None, ) -> None:
        self.providers = (
            providers
            if providers is not None
            else build_default_providers()
        )

    async def search_and_enrich( self, *, session: AsyncSession, query: str, limit_per_provider: int = 8, stop_after_success: bool = False, commit: bool = True, ) -> ExternalCatalogServiceResult:
        """ Ищет товары по подключённым источникам и импортирует полезные карточки. На текущем этапе после каждого провайдера дополнительно логируется полнота карточек. Бизнес-поведение старого сервиса сохранено. """

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
                int(
                    limit_per_provider
                ),
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

            #
            # НОВОЕ:
            # только диагностируем текущее
            # качество сформированных карточек.
            #
            # Никакого stop_after_complete
            # здесь пока нет.
            #
            await _observe_product_completeness(
                session=session,
                provider_name=(
                    provider.provider_name
                ),
                result=result,
            )

            #
            # Старое поведение полностью сохранено.
            #
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
    """ Возвращает общий экземпляр сервиса. """

    global _default_service

    if _default_service is None:
        _default_service = (
            ExternalCatalogService()
        )

    return _default_service


async def enrich_catalog( *, session: AsyncSession, query: str, limit_per_provider: int = 8, stop_after_success: bool = False, commit: bool = True, ) -> ExternalCatalogServiceResult:
    """ Удобная функциональная точка входа. """

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
