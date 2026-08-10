import asyncio
import logging

from app.database.session import async_session_maker
from app.services.product_consolidation_service import (
    consolidate_products,
)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)


async def main() -> None:
    canonical_product_id = 31661
    duplicate_product_id = 25659

    print("=" * 70)
    print("MarkaRadar Product Consolidation — DRY RUN")
    print(f"Canonical product: {canonical_product_id}")
    print(f"Duplicate product: {duplicate_product_id}")
    print("Database changes: DISABLED")
    print("=" * 70)

    async with async_session_maker() as session:
        result = await consolidate_products(
            session=session,
            canonical_product_id=canonical_product_id,
            duplicate_product_id=duplicate_product_id,
            dry_run=True,
        )

        print()
        print("=" * 70)
        print("CONSOLIDATION RESULT")
        print("=" * 70)
        print(result)
        print("=" * 70)

        # Дополнительная страховка:
        # даже если внутри сервиса что-то было изменено
        # в текущей транзакции, ничего не сохраняем.
        await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
