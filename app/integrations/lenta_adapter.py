import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.lenta_client import (
    LentaCatalogProduct,
    LentaClient,
)
from app.services.category_mapper import map_external_category
from app.services.product_merge_service import (
    ExternalProductData,
    ProductMergeResult,
    merge_external_product,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class LentaImportResult:
    found: int
    imported: int
    results: list[ProductMergeResult]


def build_category_candidates( product: LentaCatalogProduct, query: str, ) -> list[str]:
    values: list[str] = []
    for value in (product.category, query, product.name):
        cleaned = " ".join(str(value or "").strip().split())
        if cleaned:
            values.append(cleaned)
    return values


async def import_lenta_search( *, session: AsyncSession, query: str, limit: int = 8, commit: bool = False, ) -> LentaImportResult:
    """ Ищет товары в публичном каталоге Ленты и безопасно объединяет их с канонической базой MarkaRadar через Product Merge Engine. """

    client = LentaClient()
    products = await client.search(query, limit=limit)

    if not products:
        return LentaImportResult(found=0, imported=0, results=[])

    merged_results: list[ProductMergeResult] = []

    for item in products:
        # Не импортируем совсем слабую карточку: название обязательно,
        # а кроме него нужен хотя бы бренд или фотография.
        if not item.name.strip():
            continue
        if not item.brand and not item.image_url:
            continue

        category_mapping = await map_external_category(
            session=session,
            categories=build_category_candidates(item, query),
        )

        if category_mapping.category is None:
            logger.info(
                "Lenta: category not mapped for %r (%s)",
                item.name,
                item.category,
            )
            continue

        incoming = ExternalProductData(
            source="lenta",
            name=item.name,
            brand_name=item.brand,
            category_id=int(category_mapping.category.id),
            package_value=item.package_value,
            package_unit=item.package_unit,
            subtype=item.subtype,
            description=item.description,
            image_url=item.image_url,
            keywords=", ".join(
                value
                for value in (
                    item.category,
                    item.brand,
                    "Лента",
                )
                if value
            ) or None,
        )

        try:
            merge_result = await merge_external_product(
                session=session,
                incoming=incoming,
                commit=False,
            )
        except ValueError as error:
            logger.warning("Lenta merge skipped: %s", error)
            continue

        merged_results.append(merge_result)

    await session.flush()

    if commit and merged_results:
        await session.commit()

    return LentaImportResult(
        found=len(products),
        imported=len(merged_results),
        results=merged_results,
    )
