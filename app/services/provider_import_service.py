from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.product_source import ProductSource
from app.integrations.providers.base import (
    ExternalCatalogProvider,
    ExternalProduct,
    ExternalSearchResult,
)
from app.services.category_mapper import (
    map_external_category,
)
from app.services.product_merge_service import (
    ExternalProductData,
    ProductMergeResult,
    merge_external_product,
)


logger = logging.getLogger(__name__)


@dataclass( slots=True, frozen=True, )
class ProviderImportItemResult:
    """ Результат импорта одной внешней карточки. """

    provider: str
    source_id: str

    imported: bool
    skipped: bool

    merge_result: ProductMergeResult | None
    reason: str | None = None


@dataclass( slots=True, frozen=True, )
class ProviderBatchImportResult:
    """ Результат массового импорта результатов одного провайдера. """

    provider: str
    query: str

    found_count: int
    imported_count: int
    skipped_count: int
    failed_count: int

    items: tuple[
        ProviderImportItemResult,
        ...
    ]


def _clean_text( value: Any, ) -> str:
    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def _normalize_source_key( value: Any, ) -> str:
    """ Нормализация provider/source_id только для служебных проверок. Сам source_id в БД сохраняется в исходном очищенном виде. """

    return (
        _clean_text(
            value
        )
        .lower()
        .replace(
            "ё",
            "е",
        )
    )


def _keywords_to_text( values: tuple[ str, ... ], ) -> str | None:
    """ Преобразует универсальные keywords в формат Product Merge Engine. """

    cleaned = [
        value.strip()
        for value in values
        if str(
            value or ""
        ).strip()
    ]

    if not cleaned:
        return None

    return ", ".join(
        cleaned
    )


def _external_source_url( product: ExternalProduct, ) -> str | None:
    """ Извлекает URL внешней карточки, не привязываясь жёстко к одному названию поля провайдера. Поддерживаем наиболее вероятные поля: source_url product_url url Если провайдер пока URL не передаёт, связь всё равно сохраняется. """

    for attribute_name in (
        "source_url",
        "product_url",
        "url",
    ):
        value = _clean_text(
            getattr(
                product,
                attribute_name,
                None,
            )
        )

        if value:
            return value

    return None


async def _resolve_category_id( *, session: AsyncSession, product: ExternalProduct, ) -> int | None:
    """ Определяет category_id MarkaRadar для внешнего товара. Приоритет подсказок: 1. category_name провайдера; 2. дополнительные категории провайдера; 3. название товара; 4. keywords товара. """

    values: list[str] = []

    #
    # 1. Явная категория провайдера.
    #
    if product.category_name:
        values.append(
            product.category_name
        )

    #
    # 2. Дополнительные категории/теги.
    #
    values.extend(
        product.external_category_values
    )

    #
    # 3. Название товара как fallback.
    #
    if product.name:
        values.append(
            product.name
        )

    #
    # 4. Keywords также могут содержать
    # тип продукта.
    #
    values.extend(
        product.keywords
    )

    unique_values: list[str] = []
    seen: set[str] = set()

    for value in values:
        cleaned = _clean_text(
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

        unique_values.append(
            cleaned
        )

    if not unique_values:
        logger.info(
            "Category mapping: "
            "provider=%s source_id=%s "
            "no category hints",
            product.provider,
            product.source_id,
        )

        return None

    mapping = await map_external_category(
        session=session,
        categories=unique_values,
    )

    if mapping.category is None:
        logger.info(
            "Category mapping failed: "
            "provider=%s source_id=%s "
            "name=%r category_name=%r "
            "source_value=%r matched_by=%s "
            "hints=%r",
            product.provider,
            product.source_id,
            product.name,
            product.category_name,
            mapping.source_value,
            mapping.matched_by,
            unique_values,
        )

        return None

    category_id = int(
        mapping.category.id
    )

    logger.info(
        "Category mapping success: "
        "provider=%s source_id=%s "
        "name=%r category_id=%s "
        "category=%r matched_by=%s "
        "confidence=%.0f source_value=%r",
        product.provider,
        product.source_id,
        product.name,
        category_id,
        mapping.category.name,
        mapping.matched_by,
        mapping.confidence,
        mapping.source_value,
    )

    return category_id


async def convert_external_product( *, session: AsyncSession, product: ExternalProduct, ) -> ExternalProductData:
    """ Преобразует универсальный ExternalProduct в формат Product Merge Engine. """

    category_id = (
        await _resolve_category_id(
            session=session,
            product=product,
        )
    )

    return ExternalProductData(
        source=product.provider,
        name=product.name,
        brand_name=product.brand_name,
        barcode=product.barcode,
        category_id=category_id,
        package_value=product.package_value,
        package_unit=product.package_unit,
        subtype=product.subtype,
        description=product.description,
        image_url=product.image_url,
        keywords=_keywords_to_text(
            product.keywords
        ),
    )


async def get_product_source( *, session: AsyncSession, provider: str, source_id: str, ) -> ProductSource | None:
    """ Ищет уже известную постоянную связь: provider + source_id -> product_id """

    cleaned_provider = _clean_text(
        provider
    )

    cleaned_source_id = _clean_text(
        source_id
    )

    if (
        not cleaned_provider
        or not cleaned_source_id
    ):
        return None

    result = await session.execute(
        select(
            ProductSource
        )
        .where(
            ProductSource.provider
            == cleaned_provider,
            ProductSource.source_id
            == cleaned_source_id,
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def save_product_source( *, session: AsyncSession, provider: str, source_id: str, product_id: int, source_url: str | None = None, ) -> ProductSource:
    """ Создаёт или обновляет постоянную связь внешней карточки с каноническим Product. ВАЖНО: уже существующую связь с ДРУГИМ product_id автоматически не перепривязываем. Такое событие означает потенциальный дубль или ошибку сопоставления и должно разбираться отдельным consolidation-процессом. """

    cleaned_provider = _clean_text(
        provider
    )

    cleaned_source_id = _clean_text(
        source_id
    )

    cleaned_source_url = (
        _clean_text(
            source_url
        )
        or None
    )

    if not cleaned_provider:
        raise ValueError(
            "Невозможно сохранить ProductSource "
            "без provider."
        )

    if not cleaned_source_id:
        raise ValueError(
            "Невозможно сохранить ProductSource "
            "без source_id."
        )

    existing = await get_product_source(
        session=session,
        provider=cleaned_provider,
        source_id=cleaned_source_id,
    )

    if existing is not None:
        if (
            int(
                existing.product_id
            )
            != int(
                product_id
            )
        ):
            logger.warning(
                "ProductSource conflict: "
                "provider=%s source_id=%s "
                "existing_product_id=%s "
                "incoming_product_id=%s",
                cleaned_provider,
                cleaned_source_id,
                existing.product_id,
                product_id,
            )

            #
            # Ничего не перепривязываем автоматически.
            # Существующая связь считается более
            # устойчивой, пока отдельный consolidation
            # не докажет обратное.
            #
            return existing

        if (
            cleaned_source_url
            and existing.source_url
            != cleaned_source_url
        ):
            existing.source_url = (
                cleaned_source_url
            )

            await session.flush()

        return existing

    source = ProductSource(
        product_id=int(
            product_id
        ),
        provider=cleaned_provider,
        source_id=cleaned_source_id,
        source_url=cleaned_source_url,
    )

    session.add(
        source
    )

    await session.flush()

    logger.info(
        "ProductSource saved: "
        "provider=%s source_id=%s "
        "product_id=%s source_url=%r",
        cleaned_provider,
        cleaned_source_id,
        product_id,
        cleaned_source_url,
    )

    return source


async def import_external_product( *, session: AsyncSession, product: ExternalProduct, commit: bool = False, ) -> ProviderImportItemResult:
    """ Импортирует одну внешнюю карточку через Product Merge Engine. После успешного merge ОБЯЗАТЕЛЬНО сохраняет provenance: provider + source_id -> product_id Это первый шаг к стабильной multi-source архитектуре MarkaRadar. Важно: существующий товар можно обогащать даже при отсутствии category_id. Новый товар без category_id Merge Engine намеренно не создаёт. """

    provider_name = _clean_text(
        product.provider
    )

    source_id = _clean_text(
        product.source_id
    )

    if not provider_name:
        return ProviderImportItemResult(
            provider="",
            source_id=source_id,
            imported=False,
            skipped=True,
            merge_result=None,
            reason=(
                "Внешний товар не содержит provider."
            ),
        )

    if not source_id:
        logger.info(
            "Provider product skipped: "
            "provider=%s source_id=%r "
            "reason=missing source_id",
            provider_name,
            product.source_id,
        )

        return ProviderImportItemResult(
            provider=provider_name,
            source_id="",
            imported=False,
            skipped=True,
            merge_result=None,
            reason=(
                "Внешний товар не содержит source_id."
            ),
        )

    try:
        incoming = (
            await convert_external_product(
                session=session,
                product=product,
            )
        )

        existing_source = (
            await get_product_source(
                session=session,
                provider=provider_name,
                source_id=source_id,
            )
        )

        if existing_source is not None:
            logger.info(
                "ProductSource known before merge: "
                "provider=%s source_id=%s "
                "product_id=%s",
                provider_name,
                source_id,
                existing_source.product_id,
            )

        async with session.begin_nested():
            merge_result = (
                await merge_external_product(
                    session=session,
                    incoming=incoming,
                    commit=False,
                )
            )

            saved_source = (
                await save_product_source(
                    session=session,
                    provider=provider_name,
                    source_id=source_id,
                    product_id=(
                        merge_result.product.id
                    ),
                    source_url=(
                        _external_source_url(
                            product
                        )
                    ),
                )
            )

            #
            # Если связь уже существовала и Merge
            # выбрал другой Product, мы это не скрываем.
            #
            if (
                int(
                    saved_source.product_id
                )
                != int(
                    merge_result.product.id
                )
            ):
                logger.warning(
                    "ProductSource/merge divergence: "
                    "provider=%s source_id=%s "
                    "linked_product_id=%s "
                    "merge_product_id=%s",
                    provider_name,
                    source_id,
                    saved_source.product_id,
                    merge_result.product.id,
                )

        if commit:
            await session.commit()

        logger.info(
            "Provider import: provider=%s "
            "source_id=%s product_id=%s "
            "created=%s match=%s fields=%s",
            provider_name,
            source_id,
            merge_result.product.id,
            merge_result.created,
            merge_result.match_type,
            merge_result.updated_fields,
        )

        return ProviderImportItemResult(
            provider=provider_name,
            source_id=source_id,
            imported=True,
            skipped=False,
            merge_result=merge_result,
            reason=None,
        )

    except ValueError as error:
        logger.info(
            "Provider product skipped: "
            "provider=%s source_id=%s reason=%s",
            provider_name,
            source_id,
            error,
        )

        return ProviderImportItemResult(
            provider=provider_name,
            source_id=source_id,
            imported=False,
            skipped=True,
            merge_result=None,
            reason=str(
                error
            ),
        )

    except Exception as error:
        logger.exception(
            "Provider product import failed: "
            "provider=%s source_id=%s",
            provider_name,
            source_id,
        )

        return ProviderImportItemResult(
            provider=provider_name,
            source_id=source_id,
            imported=False,
            skipped=False,
            merge_result=None,
            reason=str(
                error
            ),
        )


async def import_search_result( *, session: AsyncSession, result: ExternalSearchResult, commit: bool = False, ) -> ProviderBatchImportResult:
    """ Импортирует весь ExternalSearchResult. Для каждой карточки используется SAVEPOINT, поэтому ошибка одного товара не отменяет импорт остальных. """

    item_results: list[
        ProviderImportItemResult
    ] = []

    imported_count = 0
    skipped_count = 0
    failed_count = 0

    for product in result.products:
        item_result = (
            await import_external_product(
                session=session,
                product=product,
                commit=False,
            )
        )

        item_results.append(
            item_result
        )

        if item_result.imported:
            imported_count += 1

        elif item_result.skipped:
            skipped_count += 1

        else:
            failed_count += 1

    if (
        commit
        and imported_count > 0
    ):
        await session.commit()

    logger.info(
        "Provider batch import: provider=%s "
        "query=%r found=%s imported=%s "
        "skipped=%s failed=%s",
        result.provider,
        result.query,
        result.found_count,
        imported_count,
        skipped_count,
        failed_count,
    )

    return ProviderBatchImportResult(
        provider=result.provider,
        query=result.query,
        found_count=result.found_count,
        imported_count=imported_count,
        skipped_count=skipped_count,
        failed_count=failed_count,
        items=tuple(
            item_results
        ),
    )


async def search_and_import_provider( *, session: AsyncSession, provider: ExternalCatalogProvider, query: str, limit: int = 8, commit: bool = False, ) -> ProviderBatchImportResult:
    """ Полный универсальный сценарий: provider.search(query) ↓ ExternalSearchResult ↓ Category Mapper ↓ Product Merge Engine ↓ ProductSource provenance ↓ MarkaRadar DB Используется для OpenFoodFacts, METRO и будущих внешних провайдеров. """

    result = await provider.search(
        query,
        limit=limit,
    )

    if not result.attempted:
        return ProviderBatchImportResult(
            provider=provider.provider_name,
            query=query,
            found_count=0,
            imported_count=0,
            skipped_count=0,
            failed_count=0,
            items=(),
        )

    if result.unavailable:
        logger.info(
            "Provider unavailable: "
            "provider=%s query=%r error=%r",
            result.provider,
            query,
            result.error,
        )

        return ProviderBatchImportResult(
            provider=result.provider,
            query=query,
            found_count=0,
            imported_count=0,
            skipped_count=0,
            failed_count=0,
            items=(),
        )

    return await import_search_result(
        session=session,
        result=result,
        commit=commit,
    )


async def import_barcode_from_provider( *, session: AsyncSession, provider: ExternalCatalogProvider, barcode: str, commit: bool = False, ) -> ProviderImportItemResult | None:
    """ Универсальный импорт товара по штрихкоду. Провайдеры без поддержки get_by_barcode() возвращают None. Успешный импорт также сохраняет ProductSource. """

    product = await provider.get_by_barcode(
        barcode
    )

    if product is None:
        return None

    return await import_external_product(
        session=session,
        product=product,
        commit=commit,
    )
