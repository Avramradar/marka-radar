import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.price import PriceObservation
from app.database.models.product import Product
from app.database.models.product_alias import ProductAlias
from app.database.models.product_relation import ProductRelation
from app.database.session import async_session_maker
from app.database.session import close_database
from app.utils.text import normalize_text


async def get_or_create_brand(
    name: str,
    aliases: str | None = None,
    country: str | None = None,
) -> Brand:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Brand).where(
                Brand.normalized_name == normalize_text(name)
            )
        )

        brand = result.scalar_one_or_none()

        if brand is not None:
            return brand

        brand = Brand(
            name=name,
            normalized_name=normalize_text(name),
            aliases=aliases,
            country=country,
        )

        session.add(brand)
        await session.commit()
        await session.refresh(brand)

        return brand


async def get_or_create_category(
    name: str,
    parent_id: int | None = None,
) -> Category:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Category).where(
                Category.normalized_name == normalize_text(name),
                Category.parent_id == parent_id,
            )
        )

        category = result.scalar_one_or_none()

        if category is not None:
            return category

        category = Category(
            name=name,
            normalized_name=normalize_text(name),
            parent_id=parent_id,
        )

        session.add(category)
        await session.commit()
        await session.refresh(category)

        return category


async def get_or_create_product(
    *,
    name: str,
    brand_id: int,
    category_id: int,
    barcode: str,
    package_value: Decimal,
    package_unit: str,
    subtype: str,
    keywords: str,
) -> Product:
    async with async_session_maker() as session:
        result = await session.execute(
            select(Product).where(
                Product.barcode == barcode
            )
        )

        product = result.scalar_one_or_none()

        if product is not None:
            return product

        product = Product(
            name=name,
            normalized_name=normalize_text(name),
            brand_id=brand_id,
            category_id=category_id,
            barcode=barcode,
            package_value=package_value,
            package_unit=package_unit,
            subtype=normalize_text(subtype),
            keywords=normalize_text(keywords),
            is_active=True,
        )

        session.add(product)
        await session.commit()
        await session.refresh(product)

        return product


async def add_alias(
    product_id: int,
    alias: str,
) -> None:
    async with async_session_maker() as session:
        normalized_alias = normalize_text(alias)

        result = await session.execute(
            select(ProductAlias).where(
                ProductAlias.product_id == product_id,
                ProductAlias.normalized_alias == normalized_alias,
            )
        )

        existing_alias = result.scalar_one_or_none()

        if existing_alias is not None:
            return

        session.add(
            ProductAlias(
                product_id=product_id,
                alias=alias,
                normalized_alias=normalized_alias,
            )
        )

        await session.commit()


async def add_price(
    *,
    product_id: int,
    retailer_name: str,
    region: str,
    price: Decimal,
) -> None:
    async with async_session_maker() as session:
        result = await session.execute(
            select(PriceObservation).where(
                PriceObservation.product_id == product_id,
                PriceObservation.retailer_name == retailer_name,
                PriceObservation.region == region,
                PriceObservation.price == price,
            )
        )

        existing_price = result.scalar_one_or_none()

        if existing_price is not None:
            return

        session.add(
            PriceObservation(
                product_id=product_id,
                retailer_name=retailer_name,
                region=region,
                price=price,
            )
        )

        await session.commit()


async def add_relation(
    *,
    source_category_id: int,
    target_category_id: int,
    target_subtype: str,
    compatibility_score: Decimal,
    explanation: str,
) -> None:
    async with async_session_maker() as session:
        normalized_subtype = normalize_text(target_subtype)

        result = await session.execute(
            select(ProductRelation).where(
                ProductRelation.source_category_id
                == source_category_id,
                ProductRelation.target_category_id
                == target_category_id,
                ProductRelation.target_subtype
                == normalized_subtype,
            )
        )

        existing_relation = result.scalar_one_or_none()

        if existing_relation is not None:
            return

        session.add(
            ProductRelation(
                source_category_id=source_category_id,
                target_category_id=target_category_id,
                target_subtype=normalized_subtype,
                compatibility_score=compatibility_score,
                explanation=explanation,
            )
        )

        await session.commit()


async def seed_database() -> None:
    print("Начинаем добавление тестовых данных MarkaRadar")

    baltika = await get_or_create_brand(
        name="Балтика",
        aliases="Baltika, Балтика",
        country="Россия",
    )

    dobroflot = await get_or_create_brand(
        name="Доброфлот",
        aliases="Dobroflot, Добро флот",
        country="Россия",
    )

    meridian = await get_or_create_brand(
        name="Меридиан",
        aliases="Meridian",
        country="Россия",
    )

    beverages = await get_or_create_category(
        name="Напитки",
    )

    beer = await get_or_create_category(
        name="Пиво",
        parent_id=beverages.id,
    )

    fish = await get_or_create_category(
        name="Рыба",
    )

    canned_food = await get_or_create_category(
        name="Консервы",
    )

    baltika_7 = await get_or_create_product(
        name="Балтика 7 Экспортное",
        brand_id=baltika.id,
        category_id=beer.id,
        barcode="4600682001286",
        package_value=Decimal("0.450"),
        package_unit="л",
        subtype="светлое",
        keywords="пиво светлое лагер алкогольный напиток",
    )

    smoked_mackerel = await get_or_create_product(
        name="Скумбрия холодного копчения",
        brand_id=meridian.id,
        category_id=fish.id,
        barcode="4607010741555",
        package_value=Decimal("300"),
        package_unit="г",
        subtype="копченая",
        keywords="скумбрия рыба холодного копчения закуска",
    )

    canned_mackerel = await get_or_create_product(
        name="Скумбрия натуральная",
        brand_id=dobroflot.id,
        category_id=canned_food.id,
        barcode="4607033151010",
        package_value=Decimal("245"),
        package_unit="г",
        subtype="консервированная",
        keywords="скумбрия натуральная рыбные консервы",
    )

    await add_alias(
        product_id=baltika_7.id,
        alias="Балтика семь",
    )

    await add_alias(
        product_id=smoked_mackerel.id,
        alias="копченая скумбрия",
    )

    await add_alias(
        product_id=canned_mackerel.id,
        alias="скумбрия в банке",
    )

    await add_alias(
        product_id=canned_mackerel.id,
        alias="консервы Доброфлот",
    )

    await add_price(
        product_id=baltika_7.id,
        retailer_name="Магнит",
        region="разные регионы",
        price=Decimal("89"),
    )

    await add_price(
        product_id=baltika_7.id,
        retailer_name="Пятёрочка",
        region="разные регионы",
        price=Decimal("99"),
    )

    await add_price(
        product_id=baltika_7.id,
        retailer_name="Лента",
        region="разные регионы",
        price=Decimal("109"),
    )

    await add_price(
        product_id=smoked_mackerel.id,
        retailer_name="Магнит",
        region="разные регионы",
        price=Decimal("310"),
    )

    await add_price(
        product_id=smoked_mackerel.id,
        retailer_name="Лента",
        region="разные регионы",
        price=Decimal("420"),
    )

    await add_price(
        product_id=smoked_mackerel.id,
        retailer_name="Ozon",
        region="разные регионы",
        price=Decimal("810"),
    )

    await add_price(
        product_id=canned_mackerel.id,
        retailer_name="Магнит",
        region="разные регионы",
        price=Decimal("149"),
    )

    await add_price(
        product_id=canned_mackerel.id,
        retailer_name="Пятёрочка",
        region="разные регионы",
        price=Decimal("169"),
    )

    await add_price(
        product_id=canned_mackerel.id,
        retailer_name="Лента",
        region="разные регионы",
        price=Decimal("205"),
    )

    await add_relation(
        source_category_id=beer.id,
        target_category_id=fish.id,
        target_subtype="копченая",
        compatibility_score=Decimal("0.950"),
        explanation=(
            "Копчёная рыба хорошо сочетается "
            "со светлым пивом."
        ),
    )

    await add_relation(
        source_category_id=beer.id,
        target_category_id=canned_food.id,
        target_subtype="консервированная",
        compatibility_score=Decimal("0.350"),
        explanation=(
            "Рыбные консервы могут сочетаться с пивом, "
            "но слабее копчёной рыбы."
        ),
    )

    print("Тестовые данные MarkaRadar успешно добавлены")


async def main() -> None:
    try:
        await seed_database()
    finally:
        await close_database()


if __name__ == "__main__":
    asyncio.run(main())
