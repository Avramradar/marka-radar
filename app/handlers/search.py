from html import escape

from aiogram import F
from aiogram import Router
from aiogram.types import Message

from app.database.repositories.product_repository import search_products
from app.database.session import async_session_maker
from app.keyboards.rating import get_rating_keyboard
from app.services.price_service import get_price_statistics
from app.services.rating_service import get_full_product_rating


router = Router()


def format_price_text(price_stats: dict | None) -> str:
    if price_stats is None:
        return (
            "💰 Цена пока не собрана\n"
            "Данные появятся после подключения источников цен."
        )

    price_text = (
        "💰 Средняя цена по рынку: "
        f"<b>около {price_stats['median']:.0f} ₽</b>\n"
        "📊 Встречается от "
        f"<b>{price_stats['minimum']:.0f} ₽</b> "
        "до "
        f"<b>{price_stats['maximum']:.0f} ₽</b>\n"
        "🏪 Найдено цен: "
        f"<b>{price_stats['prices_count']}</b>"
    )

    if price_stats["spread"] >= 500:
        price_text += (
            "\n⚠️ Разница между ценами: "
            f"<b>{price_stats['spread']:.0f} ₽</b>\n"
            "Перед покупкой лучше сравнить стоимость."
        )
    elif price_stats["spread_percent"] >= 25:
        price_text += (
            "\n⚠️ Большой разброс цен: "
            f"<b>{price_stats['spread_percent']:.0f}%</b>"
        )
    elif price_stats["spread"] > 0:
        price_text += (
            "\n↕️ Разброс цен: "
            f"<b>{price_stats['spread']:.0f} ₽</b>"
        )

    return price_text


@router.message(F.text)
async def search_handler(message: Message) -> None:
    if message.text is None:
        return

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

            price_stats = await get_price_statistics(
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

            price_text = format_price_text(price_stats)

            await message.answer(
                f"<b>{escape(brand.name)} — "
                f"{escape(product.name)}</b>\n\n"
                f"📂 Категория: {escape(category.name)}"
                f"{subtype_text}"
                f"{package_text}"
                f"{barcode_text}\n\n"
                f"{rating_text}\n\n"
                f"{price_text}\n\n"
                "Поставьте свою оценку:",
                reply_markup=get_rating_keyboard(product.id),
            )
