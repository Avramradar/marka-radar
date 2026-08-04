import asyncio

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.session import async_session_maker
from app.search.index_builder import build_search_index


BATCH_SIZE = 500


async def rebuild() -> None:
    updated = 0

    async with async_session_maker() as session:
        offset = 0

        while True:
            result = await session.execute(
                select(
                    Product,
                    Brand,
                    Category,
                )
                .join(
                    Brand,
                    Product.brand_id == Brand.id,
                )
                .join(
                    Category,
                    Product.category_id == Category.id,
                )
                .offset(offset)
                .limit(BATCH_SIZE)
            )

            rows = result.all()

            if not rows:
                break

            for product, brand, category in rows:
                product.search_text = build_search_index(
                    name=product.name,
                    brand=brand.name,
                    category=category.name,
                    keywords=product.keywords,
                )

                updated += 1

            await session.commit()

            print(f"Обновлено товаров: {updated}")

            offset += BATCH_SIZE

    print(f"Готово. Всего обновлено: {updated}")


if __name__ == "__main__":
    asyncio.run(rebuild())
