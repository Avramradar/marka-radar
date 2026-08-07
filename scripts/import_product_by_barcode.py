import asyncio
import sys

from app.database.session import (
    async_session_maker,
    close_database,
)
from app.integrations.openfoodfacts_adapter import (
    import_openfoodfacts_product,
)


async def main(
    barcode: str,
) -> None:
    async with async_session_maker() as session:
        try:
            result = (
                await import_openfoodfacts_product(
                    session=session,
                    barcode=barcode,
                    commit=True,
                )
            )

            if result is None:
                print(
                    "Товар не найден "
                    "в Open Food Facts."
                )
                return

            print(
                "Готово."
            )

            print(
                "Product ID:",
                result.product.id,
            )

            print(
                "Название:",
                result.product.name,
            )

            print(
                "Бренд:",
                result.brand.name,
            )

            print(
                "Создан:",
                result.created,
            )

            print(
                "Совпадение:",
                result.match_type,
            )

            print(
                "Обновлены поля:",
                ", ".join(
                    result.updated_fields
                ),
            )

        except Exception:
            await session.rollback()
            raise


if __name__ == "__main__":
    if len(
        sys.argv
    ) != 2:
        raise SystemExit(
            "Использование:\n"
            "python -m "
            "scripts.import_product_by_barcode "
            "<barcode>"
        )

    try:
        asyncio.run(
            main(
                sys.argv[1]
            )
        )

    finally:
        asyncio.run(
            close_database()
        )
