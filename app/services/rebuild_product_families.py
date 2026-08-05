import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.repositories.product_family_repository import (
    assign_product_family,
)
from app.database.session import (
    async_session_maker,
    close_database,
)


logger = logging.getLogger(__name__)


DEFAULT_BATCH_SIZE = 500


@dataclass
class RebuildProductFamiliesStatistics:
    scanned: int = 0
    assigned: int = 0
    unchanged: int = 0
    skipped: int = 0
    errors: int = 0
    last_product_id: int = 0


async def rebuild_product_families(
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    start_product_id: int = 0,
    max_products: int | None = None,
) -> RebuildProductFamiliesStatistics:
    """
    Массово определяет семейства для существующих товаров.

    Обработка выполняется пакетами, чтобы не загружать
    всю таблицу products в память.

    batch_size:
        Количество товаров в одном пакете.

    start_product_id:
        Продолжить обработку после указанного ID товара.

    max_products:
        Ограничение количества просмотренных товаров.
        Если None — обработать всю оставшуюся базу.
    """

    if batch_size < 1:
        raise ValueError(
            "batch_size должен быть больше нуля"
        )

    if start_product_id < 0:
        raise ValueError(
            "start_product_id не может быть отрицательным"
        )

    if max_products is not None and max_products < 1:
        raise ValueError(
            "max_products должен быть больше нуля"
        )

    statistics = RebuildProductFamiliesStatistics(
        last_product_id=start_product_id
    )

    try:
        async with async_session_maker() as session:
            while True:
                remaining_limit: int | None = None

                if max_products is not None:
                    remaining_limit = (
                        max_products
                        - statistics.scanned
                    )

                    if remaining_limit <= 0:
                        break

                current_batch_size = batch_size

                if remaining_limit is not None:
                    current_batch_size = min(
                        batch_size,
                        remaining_limit,
                    )

                statement = (
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
                    .where(
                        Product.id
                        > statistics.last_product_id
                    )
                    .order_by(
                        Product.id.asc()
                    )
                    .limit(
                        current_batch_size
                    )
                )

                result = await session.execute(
                    statement
                )

                rows = list(
                    result.all()
                )

                if not rows:
                    break

                for product, brand, category in rows:
                    statistics.scanned += 1
                    statistics.last_product_id = product.id

                    previous_family_id = product.family_id

                    try:
                        async with session.begin_nested():
                            family = await assign_product_family(
                                session=session,
                                product=product,
                                brand_name=brand.name,
                                category=category,
                            )

                        if family is None:
                            statistics.skipped += 1
                            continue

                        if previous_family_id == family.id:
                            statistics.unchanged += 1
                        else:
                            statistics.assigned += 1

                    except Exception:
                        statistics.errors += 1

                        logger.exception(
                            "Не удалось определить семейство "
                            "для товара id=%s, name=%r",
                            product.id,
                            product.name,
                        )

                await session.commit()

                logger.info(
                    "Семейства: просмотрено=%s; "
                    "назначено=%s; без изменений=%s; "
                    "пропущено=%s; ошибок=%s; "
                    "последний product_id=%s",
                    statistics.scanned,
                    statistics.assigned,
                    statistics.unchanged,
                    statistics.skipped,
                    statistics.errors,
                    statistics.last_product_id,
                )

    finally:
        await close_database()

    logger.info(
        "Перестроение семейств завершено. "
        "Просмотрено: %s; назначено: %s; "
        "без изменений: %s; пропущено: %s; "
        "ошибок: %s; последний product_id: %s.",
        statistics.scanned,
        statistics.assigned,
        statistics.unchanged,
        statistics.skipped,
        statistics.errors,
        statistics.last_product_id,
    )

    return statistics


async def main() -> None:
    statistics = await rebuild_product_families()

    print()
    print("=" * 60)
    print("РАСПРЕДЕЛЕНИЕ ТОВАРОВ ПО СЕМЕЙСТВАМ ЗАВЕРШЕНО")
    print("=" * 60)

    print(
        f"Просмотрено товаров: "
        f"{statistics.scanned}"
    )

    print(
        f"Назначено новых семейств: "
        f"{statistics.assigned}"
    )

    print(
        f"Уже были распределены: "
        f"{statistics.unchanged}"
    )

    print(
        f"Пропущено товаров: "
        f"{statistics.skipped}"
    )

    print(
        f"Ошибок: "
        f"{statistics.errors}"
    )

    print(
        f"Последний product_id: "
        f"{statistics.last_product_id}"
    )

    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
