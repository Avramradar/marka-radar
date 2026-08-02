from html import escape

from aiogram import F
from aiogram import Router
from aiogram.types import Message

from app.database.repositories.product_repository import search_products
from app.database.session import async_session_maker
from app.keyboards.rating import get_rating_keyboard
from app.services.rating_service import get_full_product_rating


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
            rating = await get_full_product_rating(
                session=session,
                product_id=product.id,
            )

            package_text = ""

            if (
                product.package_value is not None
                and product.package_unit
            ):
                package_text = (
                    "\n📦 Упаковка: "
                    f"{product.package_value} "
                    f"{escape(product.package_unit)}"
                )

            subtype_text = ""

            if product.subtype:
                subtype_text = (
                    "\n🏷 Вариант: "
                    f"{escape(product.subtype)}"
                )

            barcode_text = ""

            if product.barcode:
                barcode_text = (
                    "\n🔢 Штрихкод: "
                    f"<code>{escape(product.barcode)}</code>"
                )

            if rating["votes_count"] > 0:
                rating_text = (
                    "⭐ Рейтинг пользователей: "
                    f"<b>{rating['average_rating']:.1f} из 10</b>\n"
                    "👥 Количество оценок: "
                    f"<b>{rating['votes_count']}</b>"
                )

                if rating["votes_count"] < 5:
                    rating_text += (
                        "\n⚠️ Рейтинг пока недостаточно подтверждён"
                    )
                elif rating["votes_count"] < 20:
                    rating_text += (
                        "\n🛡 Достоверность рейтинга: средняя"
                    )
                else:
                    rating_text += (
                        "\n🛡 Достоверность рейтинга: высокая"
                    )
            else:
                rating_text = (
                    "⭐ Оценок пока нет\n"
                    "Будьте первым, кто оценит этот товар."
                )

            await message.answer(
                f"<b>{escape(brand.name)} — "
                f"{escape(product.name)}</b>\n\n"
                f"📂 Категория: {escape(category.name)}"
                f"{subtype_text}"
                f"{package_text}"
                f"{barcode_text}\n\n"
                f"{rating_text}\n\n"
                "Поставьте свою оценку:",
                reply_markup=get_rating_keyboard(product.id),
            )
