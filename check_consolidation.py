import asyncio

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_source import ProductSource
from app.database.session import async_session_maker
from app.services.product_merge_service import (
    normalize_barcode,
    normalize_package_unit,
    normalize_package_value,
)


async def load_product_bundle(
    *,
    product_id: int,
):
    async with async_session_maker() as session:
        product_result = await session.execute(
            select(Product)
            .where(
                Product.id == product_id
            )
            .limit(1)
        )

        product = product_result.scalar_one_or_none()

        if product is None:
            return None

        brand_result = await session.execute(
            select(Brand)
            .where(
                Brand.id == product.brand_id
            )
            .limit(1)
        )

        brand = brand_result.scalar_one_or_none()

        category_result = await session.execute(
            select(Category)
            .where(
                Category.id == product.category_id
            )
            .limit(1)
        )

        category = category_result.scalar_one_or_none()

        sources_result = await session.execute(
            select(ProductSource)
            .where(
                ProductSource.product_id
                == product.id
            )
            .order_by(
                ProductSource.provider.asc(),
                ProductSource.source_id.asc(),
            )
        )

        sources = list(
            sources_result.scalars().all()
        )

        return (
            product,
            brand,
            category,
            sources,
        )


def print_product_bundle(
    *,
    title: str,
    bundle,
) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)

    if bundle is None:
        print("NOT FOUND")
        return

    (
        product,
        brand,
        category,
        sources,
    ) = bundle

    print("id:", product.id)
    print("name:", repr(product.name))
    print(
        "brand:",
        repr(
            brand.name
            if brand is not None
            else None
        ),
    )
    print(
        "category:",
        repr(
            category.name
            if category is not None
            else None
        ),
    )
    print(
        "barcode_raw:",
        repr(product.barcode),
    )
    print(
        "barcode_normalized:",
        repr(
            normalize_barcode(
                product.barcode
            )
        ),
    )
    print(
        "package_value_raw:",
        repr(product.package_value),
    )
    print(
        "package_value_normalized:",
        repr(
            normalize_package_value(
                product.package_value
            )
        ),
    )
    print(
        "package_unit_raw:",
        repr(product.package_unit),
    )
    print(
        "package_unit_normalized:",
        repr(
            normalize_package_unit(
                product.package_unit
            )
        ),
    )
    print(
        "subtype:",
        repr(product.subtype),
    )
    print(
        "image_url:",
        repr(product.image_url),
    )
    print(
        "description:",
        repr(product.description),
    )
    print(
        "is_active:",
        product.is_active,
    )

    print()
    print("SOURCES:")

    if not sources:
        print("  none")
    else:
        for source in sources:
            print(
                " ",
                source.provider,
                "|",
                source.source_id,
                "|",
                source.source_url,
            )


async def main() -> None:
    canonical_id = 25659
    duplicate_id = 31661

    canonical = await load_product_bundle(
        product_id=canonical_id
    )

    duplicate = await load_product_bundle(
        product_id=duplicate_id
    )

    print_product_bundle(
        title="CANONICAL 25659",
        bundle=canonical,
    )

    print_product_bundle(
        title="DUPLICATE 31661",
        bundle=duplicate,
    )

    print()
    print("=" * 70)
    print("DATABASE CHANGES: NONE")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
