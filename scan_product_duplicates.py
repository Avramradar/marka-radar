import asyncio
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.session import async_session_maker
from app.services.product_merge_service import (
    identity_name_similarity,
    is_generic_category,
    is_unknown_brand,
    normalize_barcode,
    package_values_compatible,
)


@dataclass(
    slots=True,
    frozen=True,
)
class DuplicateCandidate:
    left_id: int
    right_id: int

    brand_name: str
    category_name: str

    left_name: str
    right_name: str

    left_barcode: str | None
    right_barcode: str | None

    left_package: str
    right_package: str

    coverage: float
    jaccard: float
    common_count: int

    barcode_equal: bool
    package_compatible: bool | None

    score: float


def package_text(
    product: Product,
) -> str:
    value = product.package_value
    unit = product.package_unit or ""

    if value is None:
        return ""

    return f"{value}{unit}"


def candidate_score(
    *,
    barcode_equal: bool,
    package_compatible: bool | None,
    same_brand: bool,
    same_category: bool,
    coverage: float,
    jaccard: float,
    common_count: int,
) -> float:
    score = 0.0

    if barcode_equal:
        score += 100.0

    if same_brand:
        score += 20.0

    if same_category:
        score += 15.0

    if package_compatible is True:
        score += 20.0

    score += coverage * 25.0
    score += jaccard * 15.0

    if common_count >= 3:
        score += 10.0
    elif common_count >= 2:
        score += 5.0

    return round(
        score,
        1,
    )


async def main() -> None:
    print("=" * 80)
    print("MarkaRadar Duplicate Scanner")
    print("DATABASE CHANGES: NONE")
    print("=" * 80)

    async with async_session_maker() as session:
        result = await session.execute(
            select(
                Product,
                Brand,
                Category,
            )
            .join(
                Brand,
                Brand.id == Product.brand_id,
            )
            .join(
                Category,
                Category.id == Product.category_id,
            )
            .where(
                Product.is_active.is_(True)
            )
            .order_by(
                Product.id.asc()
            )
        )

        rows = list(
            result.all()
        )

    print(
        "Active products:",
        len(rows),
    )

    by_barcode: dict[
        str,
        list[
            tuple[
                Product,
                Brand,
                Category,
            ]
        ],
    ] = defaultdict(list)

    by_brand_category: dict[
        tuple[
            int,
            int,
        ],
        list[
            tuple[
                Product,
                Brand,
                Category,
            ]
        ],
    ] = defaultdict(list)

    for (
        product,
        brand,
        category,
    ) in rows:
        barcode = normalize_barcode(
            product.barcode
        )

        if barcode:
            by_barcode[
                barcode
            ].append(
                (
                    product,
                    brand,
                    category,
                )
            )

        if (
            not is_unknown_brand(
                brand.name
            )
            and not is_generic_category(
                category.name
            )
        ):
            by_brand_category[
                (
                    brand.id,
                    category.id,
                )
            ].append(
                (
                    product,
                    brand,
                    category,
                )
            )

    candidates: dict[
        tuple[
            int,
            int,
        ],
        DuplicateCandidate,
    ] = {}

    #
    # 1. Самое сильное доказательство:
    # одинаковый barcode.
    #
    for (
        barcode,
        group,
    ) in by_barcode.items():
        if len(group) < 2:
            continue

        for left_index in range(
            len(group)
        ):
            for right_index in range(
                left_index + 1,
                len(group),
            ):
                (
                    left,
                    left_brand,
                    left_category,
                ) = group[
                    left_index
                ]

                (
                    right,
                    right_brand,
                    right_category,
                ) = group[
                    right_index
                ]

                (
                    coverage,
                    jaccard,
                    common_count,
                ) = identity_name_similarity(
                    left.name,
                    right.name,
                )

                compatibility = (
                    package_values_compatible(
                        current_value=(
                            left.package_value
                        ),
                        current_unit=(
                            left.package_unit
                        ),
                        incoming_value=(
                            right.package_value
                        ),
                        incoming_unit=(
                            right.package_unit
                        ),
                    )
                )

                key = (
                    min(
                        left.id,
                        right.id,
                    ),
                    max(
                        left.id,
                        right.id,
                    ),
                )

                candidates[
                    key
                ] = DuplicateCandidate(
                    left_id=left.id,
                    right_id=right.id,
                    brand_name=(
                        left_brand.name
                    ),
                    category_name=(
                        left_category.name
                    ),
                    left_name=left.name,
                    right_name=right.name,
                    left_barcode=barcode,
                    right_barcode=barcode,
                    left_package=(
                        package_text(
                            left
                        )
                    ),
                    right_package=(
                        package_text(
                            right
                        )
                    ),
                    coverage=coverage,
                    jaccard=jaccard,
                    common_count=(
                        common_count
                    ),
                    barcode_equal=True,
                    package_compatible=(
                        compatibility
                    ),
                    score=candidate_score(
                        barcode_equal=True,
                        package_compatible=(
                            compatibility
                        ),
                        same_brand=(
                            left.brand_id
                            == right.brand_id
                        ),
                        same_category=(
                            left.category_id
                            == right.category_id
                        ),
                        coverage=coverage,
                        jaccard=jaccard,
                        common_count=(
                            common_count
                        ),
                    ),
                )

    #
    # 2. Без одинакового barcode:
    # только внутри одного реального бренда
    # и одной конкретной категории.
    #
    for (
        _group_key,
        group,
    ) in by_brand_category.items():
        if len(group) < 2:
            continue

        for left_index in range(
            len(group)
        ):
            for right_index in range(
                left_index + 1,
                len(group),
            ):
                (
                    left,
                    brand,
                    category,
                ) = group[
                    left_index
                ]

                (
                    right,
                    _right_brand,
                    _right_category,
                ) = group[
                    right_index
                ]

                left_barcode = (
                    normalize_barcode(
                        left.barcode
                    )
                )

                right_barcode = (
                    normalize_barcode(
                        right.barcode
                    )
                )

                #
                # Разные известные barcode:
                # это НЕ дубль.
                #
                if (
                    left_barcode
                    and right_barcode
                    and left_barcode
                    != right_barcode
                ):
                    continue

                compatibility = (
                    package_values_compatible(
                        current_value=(
                            left.package_value
                        ),
                        current_unit=(
                            left.package_unit
                        ),
                        incoming_value=(
                            right.package_value
                        ),
                        incoming_unit=(
                            right.package_unit
                        ),
                    )
                )

                if compatibility is False:
                    continue

                (
                    coverage,
                    jaccard,
                    common_count,
                ) = identity_name_similarity(
                    left.name,
                    right.name,
                )

                #
                # Для автоматического кандидата
                # нужны хотя бы два сильных общих
                # токена.
                #
                if common_count < 2:
                    continue

                if coverage < 0.80:
                    continue

                if jaccard < 0.55:
                    continue

                key = (
                    min(
                        left.id,
                        right.id,
                    ),
                    max(
                        left.id,
                        right.id,
                    ),
                )

                score = candidate_score(
                    barcode_equal=(
                        bool(
                            left_barcode
                            and right_barcode
                            and left_barcode
                            == right_barcode
                        )
                    ),
                    package_compatible=(
                        compatibility
                    ),
                    same_brand=True,
                    same_category=True,
                    coverage=coverage,
                    jaccard=jaccard,
                    common_count=(
                        common_count
                    ),
                )

                existing = candidates.get(
                    key
                )

                if (
                    existing is not None
                    and existing.score
                    >= score
                ):
                    continue

                candidates[
                    key
                ] = DuplicateCandidate(
                    left_id=left.id,
                    right_id=right.id,
                    brand_name=brand.name,
                    category_name=(
                        category.name
                    ),
                    left_name=left.name,
                    right_name=right.name,
                    left_barcode=(
                        left_barcode
                    ),
                    right_barcode=(
                        right_barcode
                    ),
                    left_package=(
                        package_text(
                            left
                        )
                    ),
                    right_package=(
                        package_text(
                            right
                        )
                    ),
                    coverage=coverage,
                    jaccard=jaccard,
                    common_count=(
                        common_count
                    ),
                    barcode_equal=(
                        bool(
                            left_barcode
                            and right_barcode
                            and left_barcode
                            == right_barcode
                        )
                    ),
                    package_compatible=(
                        compatibility
                    ),
                    score=score,
                )

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item.score,
            item.barcode_equal,
            item.common_count,
        ),
        reverse=True,
    )

    print()
    print(
        "Duplicate candidates:",
        len(ordered),
    )
    print()

    #
    # Чтобы GitHub Actions не превратился
    # в огромную простыню, пока показываем
    # первые 100 наиболее сильных кандидатов.
    #
    for index, item in enumerate(
        ordered[:100],
        start=1,
    ):
        print("-" * 80)
        print(
            f"#{index} score={item.score}"
        )
        print(
            "ids:",
            item.left_id,
            "<->",
            item.right_id,
        )
        print(
            "brand:",
            item.brand_name,
        )
        print(
            "category:",
            item.category_name,
        )
        print(
            "left:",
            repr(item.left_name),
        )
        print(
            "right:",
            repr(item.right_name),
        )
        print(
            "barcodes:",
            item.left_barcode,
            "|",
            item.right_barcode,
        )
        print(
            "packages:",
            item.left_package,
            "|",
            item.right_package,
        )
        print(
            "barcode_equal:",
            item.barcode_equal,
        )
        print(
            "package_compatible:",
            item.package_compatible,
        )
        print(
            "common_count:",
            item.common_count,
        )
        print(
            "coverage:",
            round(
                item.coverage,
                3,
            ),
        )
        print(
            "jaccard:",
            round(
                item.jaccard,
                3,
            ),
        )

    print()
    print("=" * 80)
    print("SCAN COMPLETE")
    print("DATABASE CHANGES: NONE")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
