import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.session import async_session_maker
from app.handlers.search import show_single_product


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(
    F.data.startswith("product:")
)
async def product_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Открывает полную карточку выбранного товара.

    Цепочка:

    product:<id>
    → загрузка товара из базы
    → рейтинг и цены
    → Trust Engine
    → карточка товара
    → возможность поставить оценку
    """

    if callback.data is None:
        return

    if callback.message is None:
        await callback.answer()
        return

    try:
        product_id = int(
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
            "Некорректный товар",
            show_alert=True,
        )
        return

    # Сразу закрываем индикатор ожидания Telegram.
    await callback.answer(
        "Открываю карточку товара…"
    )

    try:
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
                    Product.category_id
                    == Category.id,
                )
                .where(
                    Product.id == product_id,
                    Product.is_active.is_(True),
                )
            )

            result = await session.execute(
                statement
            )

            row = result.first()

            if row is None:
                await callback.message.answer(
                    "🔍 Товар не найден или "
                    "больше недоступен."
                )
                return

            product, brand, category = row

            # Убираем старую клавиатуру результатов,
            # чтобы пользователь не нажимал повторно
            # на уже выбранные варианты.
            try:
                await callback.message.edit_reply_markup(
                    reply_markup=None
                )

            except TelegramBadRequest as error:
                error_text = str(
                    error
                ).lower()

                if (
                    "message is not modified"
                    not in error_text
                ):
                    logger.debug(
                        "Не удалось убрать клавиатуру "
                        "результатов",
                        exc_info=True,
                    )

            # Используем единую функцию карточки.
            #
            # Она сама:
            # - загружает рейтинг;
            # - загружает цены;
            # - рассчитывает полноту данных;
            # - запускает Trust Engine;
            # - отправляет карточку;
            # - добавляет клавиатуру оценки.
            await show_single_product(
                message=callback.message,
                session=session,
                product=product,
                brand=brand,
                category=category,
            )

    except Exception:
        logger.exception(
            "Ошибка открытия карточки товара %s",
            product_id,
        )

        await callback.message.answer(
            "⚠️ Не удалось открыть карточку товара.\n"
            "Попробуйте ещё раз немного позже."
        )
