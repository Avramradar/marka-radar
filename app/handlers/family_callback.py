import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.product import Product
from app.database.repositories.product_family_search_repository import (
    get_product_family,
)
from app.database.session import async_session_maker
from app.keyboards.search import (
    get_paginated_products_keyboard,
)
from app.search.product_list_state import (
    save_product_list,
)


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(
    F.data.startswith("family:")
)
async def family_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Показывает товары выбранного семейства.

    Например:

    family:15

    может соответствовать семейству:

    Сельдь филе в масле
    """

    if callback.data is None:
        return

    if callback.message is None:
        await callback.answer()
        return

    try:
        family_id = int(
            callback.data.split(
                ":",
                1,
            )[1]
        )

    except (
        IndexError,
        ValueError,
    ):
        await callback.answer(
            "Некорректное семейство",
            show_alert=True,
        )
        return

    await callback.answer(
        "Загружаю товары…"
    )

    try:
        async with async_session_maker() as session:
            family = await get_product_family(
                session=session,
                family_id=family_id,
            )

            if family is None:
                await callback.answer(
                    "Семейство не найдено",
                    show_alert=True,
                )
                return

            statement = (
                select(
                    Product,
                    Brand,
                )
                .join(
                    Brand,
                    Product.brand_id == Brand.id,
                )
                .where(
                    Product.family_id == family_id,
                    Product.is_active.is_(True),
                )
                .order_by(
                    Brand.name.asc(),
                    Product.name.asc(),
                    Product.id.asc(),
                )
                .limit(100)
            )

            result = await session.execute(
                statement
            )

            rows = list(
                result.all()
            )

    except Exception:
        logger.exception(
            "Ошибка загрузки семейства %s",
            family_id,
        )

        await callback.message.answer(
            "⚠️ Не удалось загрузить товары.\n"
            "Попробуйте повторить немного позже."
        )
        return

    if not rows:
        await callback.message.answer(
            "В этом семействе пока нет активных товаров."
        )
        return

    products = [
        {
            "product_id": product.id,
            "name": product.name,
            "brand": brand.name,
        }
        for product, brand in rows
    ]

    save_product_list(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        title=family.name,
        products=products,
    )

    total_products = len(products)

    text = (
        "🧺 <b>Товары выбранного вида</b>\n\n"
        f"Семейство: «{escape(family.name)}»\n"
        f"Найдено товаров: "
        f"<b>{total_products}</b>\n\n"
        "Выберите товар:"
    )

    keyboard = get_paginated_products_keyboard(
        products=products,
        page=0,
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
        )

    except TelegramBadRequest:
        logger.warning(
            "Не удалось заменить список семейств "
            "на список товаров",
            exc_info=True,
        )

        await callback.message.answer(
            text,
            reply_markup=keyboard,
        )
