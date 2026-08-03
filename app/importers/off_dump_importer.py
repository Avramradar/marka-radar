import gzip
import json

from app.database.session import AsyncSessionLocal
from app.database.repositories.product_repository import create_or_update_product


async def import_dump(path: str):
    total = 0
    imported = 0

    async with AsyncSessionLocal() as session:

        with gzip.open(path, "rt", encoding="utf-8") as f:

            for line in f:

                total += 1

                try:
                    product = json.loads(line)
                except Exception:
                    continue

                countries = (
                    product.get("countries_tags")
                    or []
                )

                if "en:russia" not in countries:
                    continue

                barcode = product.get("code")

                if not barcode:
                    continue

                await create_or_update_product(
                    session=session,
                    raw_product=product,
                )

                imported += 1

                if imported % 100 == 0:
                    await session.commit()
                    print(
                        f"Импортировано {imported}"
                    )

        await session.commit()

    print(
        f"Всего строк: {total}"
    )

    print(
        f"Импортировано: {imported}"
    )
