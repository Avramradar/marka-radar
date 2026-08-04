import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.session import async_session_maker
from app.handlers.search import send_product_card
from app.services.price_service import get_price_statistics
from app.services.rating_service import get_full_product_rating
from sqlalchemy import select


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(F.data.startswith("product:"))
async def product_callback_handler(
    callback: CallbackQuery,
) -> None:
    if callback.data is None:
        return

    try:
        product_id = int(
            callback.data.split(":", 1)[1]
        )
    except (IndexError, ValueError):
        await callback.answer(
            "Некорректный товар",
            show_alert=True,
        )
        return

    if callback.message is None:
        await callback.answer()
        return

    async with async_session_maker() as session:
        statement = (
            select(
                Product,
                Brand,
                Category,
            )
            .join(
                Brand,
                Product.brand_id == Brand.id,
            )
            .join(
                Category,
                Product.category_id == Category.id,
            )
            .where(
                Product.id == product_id,
                Product.is_active.is_(True),
            )
        )

        result = await session.execute(statement)
        row = result.first()

        if row is None:
            await callback.answer(
                "Товар не найден",
                show_alert=True,
            )
            return

        product, brand, category = row

        rating = await get_full_product_rating(
            session=session,
            product_id=product.id,
        )

        price_stats = await get_price_statistics(
            session=session,
            product_id=product.id,
        )

        try:
            await callback.message.edit_reply_markup(
                reply_markup=None
            )
        except TelegramBadRequest:
            logger.debug(
                "Не удалось убрать клавиатуру подсказок",
                exc_info=True,
            )

        await send_product_card(
            message=callback.message,
            product=product,
            brand=brand,
            category=category,
            rating=rating,
            price_stats=price_stats,
        )

    await callback.answer()
