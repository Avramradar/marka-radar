import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openfoodfacts_adapter import (
    build_description,
    build_keywords,
    build_subtype,
    choose_brand,
    choose_name,
    parse_structured_quantity,
    unique_values,
)
from app.integrations.openfoodfacts_search_client import (
    OpenFoodFactsSearchClient,
)
from app.services.category_mapper import (
    map_external_category,
)
from app.services.product_merge_service import (
    ExternalProductData,
    merge_external_product,
)


logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class OpenFoodFactsSearchImportResult:
    found: int
    imported: int
    with_images: int


async def import_openfoodfacts_search( *, session: AsyncSession, query: str, limit: int = 8, commit: bool = False, ) -> OpenFoodFactsSearchImportResult:
    """ Ищет товары в Open Food Facts по названию и безопасно объединяет их с базой MarkaRadar. Приоритет этой интеграции — конкретные карточки с фотографиями. Слабые совпадения уже отсекаются OpenFoodFactsSearchClient. """

    client = OpenFoodFactsSearchClient()

    search_results = await client.search(
        query,
        limit=limit,
        require_image=True,
    )

    if not search_results:
        return OpenFoodFactsSearchImportResult(
            found=0,
            imported=0,
            with_images=0,
        )

    imported = 0
    with_images = 0

    for search_item in search_results:
        product = search_item.product

        name = choose_name(
            product
        )

        if not name:
            continue

        image_url = (
            product.image_front_url
            or product.image_url
            or product.image_front_small_url
        )

        if not image_url:
            continue

        category_values = unique_values(
            (
                *product.categories,
                *product.categories_tags,
                *product.categories_tags_ru,
                *product.categories_tags_en,
            )
        )

        category_mapping = await map_external_category(
            session=session,
            categories=category_values,
        )

        category_id: int | None = None

        if category_mapping.category is not None:
            category_id = int(
                category_mapping.category.id
            )

        package_value, package_unit = (
            parse_structured_quantity(
                product
            )
        )

        incoming = ExternalProductData(
            source="openfoodfacts_search",
            name=name,
            brand_name=choose_brand(
                product
            ),
            barcode=product.barcode,
            category_id=category_id,
            package_value=package_value,
            package_unit=package_unit,
            subtype=build_subtype(
                product
            ),
            description=build_description(
                product
            ),
            image_url=image_url,
            keywords=build_keywords(
                product
            ),
        )

        try:
            # SAVEPOINT: один плохой внешний товар
            # не должен отменить остальные результаты.
            async with session.begin_nested():
                merge_result = await merge_external_product(
                    session=session,
                    incoming=incoming,
                    commit=False,
                )

            imported += 1

            if merge_result.product.image_url:
                with_images += 1

            logger.info(
                "OFF search merge: query=%r "
                "product_id=%s match=%s fields=%s "
                "image=%r",
                query,
                merge_result.product.id,
                merge_result.match_type,
                merge_result.updated_fields,
                merge_result.product.image_url,
            )

        except ValueError as error:
            # Новый товар без сопоставленной category_id
            # не создаём. Существующие товары при этом
            # продолжают обогащаться.
            logger.info(
                "OFF search product skipped: "
                "query=%r barcode=%s reason=%s",
                query,
                product.barcode,
                error,
            )

            continue

        except Exception:
            logger.exception(
                "OFF search merge failed: "
                "query=%r barcode=%s",
                query,
                product.barcode,
            )

            continue

    if imported > 0 and commit:
        await session.commit()

    return OpenFoodFactsSearchImportResult(
        found=len(search_results),
        imported=imported,
        with_images=with_images,
    )
