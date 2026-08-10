import asyncio

from app.database.session import async_session_maker
from app.services.product_consolidation_service import (
    consolidate_products,
)


async def main() -> None:
    canonical_product_id = 25659
    duplicate_product_id = 31661

    print("=" * 70)
    print("MarkaRadar Product Consolidation — CONFIRMED DRY RUN")
    print("=" * 70)
    print(
        "canonical_product_id:",
        canonical_product_id,
    )
    print(
        "duplicate_product_id:",
        duplicate_product_id,
    )
    print(
        "confirmed_identity:",
        True,
    )
    print(
        "dry_run:",
        True,
    )
    print(
        "DATABASE CHANGES: DISABLED",
    )
    print("=" * 70)

    async with async_session_maker() as session:
        result = await consolidate_products(
            session=session,
            canonical_product_id=(
                canonical_product_id
            ),
            duplicate_product_id=(
                duplicate_product_id
            ),
            dry_run=True,
            commit=False,
            confirmed_identity=True,
        )

        print()
        print("=" * 70)
        print("CONSOLIDATION RESULT")
        print("=" * 70)

        print(
            "canonical_product_id:",
            result.canonical_product_id,
        )
        print(
            "duplicate_product_id:",
            result.duplicate_product_id,
        )
        print(
            "applied:",
            result.applied,
        )
        print(
            "dry_run:",
            result.dry_run,
        )
        print(
            "updated_fields:",
            result.updated_fields,
        )
        print(
            "moved_sources:",
            result.moved_sources,
        )
        print(
            "moved_prices:",
            result.moved_prices,
        )
        print(
            "moved_ratings:",
            result.moved_ratings,
        )
        print(
            "moved_reviews:",
            result.moved_reviews,
        )
        print(
            "moved_search_history:",
            result.moved_search_history,
        )
        print(
            "moved_aliases:",
            result.moved_aliases,
        )
        print(
            "removed_rating_conflicts:",
            result.removed_rating_conflicts,
        )
        print(
            "removed_review_conflicts:",
            result.removed_review_conflicts,
        )
        print(
            "aliases_added:",
            result.aliases_added,
        )
        print(
            "conflicts:",
            result.conflicts,
        )
        print(
            "blocked_reasons:",
            result.blocked_reasons,
        )

        print("=" * 70)

        await session.rollback()


if __name__ == "__main__":
    asyncio.run(main())
