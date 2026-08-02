from html import escape

from aiogram import F
from aiogram import Router
from aiogram.types import Message

from app.database.repositories.product_repository import search_products
from app.database.session import async_session_maker


router = Router()


@router.message(F.text)
async def search_handler(message: Message) -> None:
    query = message.text.strip()

    if not query:
        await message.answer(
            "Введите название продукта или бренда."
        )
        return

    if query.startswith("/"):
        return

    async with async_session_maker() as session:
        products = await search_products(
            session=session,
            query=query,
            limit=20,
        )

    if not products:
        await message.answer(
            "🔍 По вашему запросу ничего не найдено.\n\n"
            "Попробуйте:\n"
            "• написать название короче;\n"
            "• указать бренд;\n"
            "• проверить написание;\n"
            "• отправить штрихкод."
        )
        return

    await message.answer(
        f"🔍 По запросу <b>{escape(query)}</b> "
        f"найдено вариантов: <b>{len(products)}</b>"
    )

    for product, brand, category in products[:10]:
        package = ""

        if product.package_value and product.package_unit:
            package = (
                f"\n📦 Упаковка: "
                f"{product.package_value} {escape(product.package_unit)}"
            )

        subtype = ""

        if product.subtype:
            subtype = (
                f"\n🏷 Вариант: {escape(product.subtype)}"
            )

        barcode = ""

        if product.barcode:
            barcode = (
                f"\n🔢 Штрихкод: "
                f"<code>{escape(product.barcode)}</code>"
            )

        await message.answer(
            f"<b>{escape(brand.name)} — "
            f"{escape(product.name)}</b>\n\n"
            f"📂 Категория: {escape(category.name)}"
            f"{subtype}"
            f"{package}"
            f"{barcode}\n\n"
            "⭐ Пользовательский рейтинг пока рассчитывается."
        )
