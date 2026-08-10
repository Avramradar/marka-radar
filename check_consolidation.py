import asyncio
import logging

from sqlalchemy import select

from app.database.models.product import Product
from app.database.session import async_session_maker
from app.services.product_merge_service import (
    identity_name_similarity,
    identity_name_tokens,
    normalized,
)


logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s | %(levelname)s | "
        "%(name)s | %(message)s"
    ),
)


async def load_product(
    *,
    product_id: int,
):
    async with async_session_maker() as session:
        result = await session.execute(
            select(Product)
            .where(
                Product.id == product_id
            )
            .limit(1)
        )

        return result.scalar_one_or_none()


async def main() -> None:
    canonical_id = 25659
    duplicate_id = 31661

    canonical = await load_product(
        product_id=canonical_id
    )

    duplicate = await load_product(
        product_id=duplicate_id
    )

    print("=" * 70)
    print("MarkaRadar Product Identity Diagnostic")
    print("=" * 70)

    if canonical is None:
        print(
            f"Canonical product {canonical_id} "
            "NOT FOUND"
        )
        return

    if duplicate is None:
        print(
            f"Duplicate product {duplicate_id} "
            "NOT FOUND"
        )
        return

    canonical_tokens = identity_name_tokens(
        canonical.name
    )

    duplicate_tokens = identity_name_tokens(
        duplicate.name
    )

    (
        coverage,
        jaccard,
        common_count,
    ) = identity_name_similarity(
        canonical.name,
        duplicate.name,
    )

    print()
    print("CANONICAL")
    print("-" * 70)
    print("id:", canonical.id)
    print("name:", repr(canonical.name))
    print(
        "normalized_name:",
        repr(canonical.normalized_name),
    )
    print(
        "normalized(name):",
        repr(normalized(canonical.name)),
    )
    print(
        "tokens:",
        sorted(canonical_tokens),
    )

    print()
    print("DUPLICATE")
    print("-" * 70)
    print("id:", duplicate.id)
    print("name:", repr(duplicate.name))
    print(
        "normalized_name:",
        repr(duplicate.normalized_name),
    )
    print(
        "normalized(name):",
        repr(normalized(duplicate.name)),
    )
    print(
        "tokens:",
        sorted(duplicate_tokens),
    )

    print()
    print("SIMILARITY")
    print("-" * 70)
    print(
        "common_tokens:",
        sorted(
            canonical_tokens
            & duplicate_tokens
        ),
    )
    print("common_count:", common_count)
    print("coverage:", coverage)
    print("jaccard:", jaccard)

    print()
    print("=" * 70)
    print(
        "DATABASE CHANGES: NONE"
    )
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
