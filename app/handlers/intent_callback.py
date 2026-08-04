import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.database.repositories.product_repository import (
    search_products,
)
from app.database.session import async_session_maker
from app.keyboards.search import (
    get_search_suggestions_keyboard,
)
from app.search.intent_state import (
    clear_intent_groups,
    get_intent_group,
)


router = Router()
logger = logging.getLogger(__name__)


@router.callback_query(
    F.data.startswith("intent:")
)
async def intent_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает выбор уточняющей группы.

    Например:

    intent:0

    может соответствовать запросу:

    сельдь в масле
    """

    if callback.data is None:
        return

    if callback.message is None:
        await callback.answer()
        return

    if callback.from_user is None:
        await callback.answer()
        return

    try:
        group_index = int(
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
            "Некорректный вариант",
            show_alert=True,
        )
        return

    group = get_intent_group(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        index=group_index,
    )

    if group is None:
        await callback.answer(
            (
                "Список уточнений устарел. "
                "Повторите поиск."
            ),
            show_alert=True,
        )
        return

    query = str(
        group.get(
            "query",
            "",
        )
    ).strip()

    title = str(
        group.get(
            "title",
            query,
        )
    ).strip()

    if not query:
        await callback.answer(
            "Пустой поисковый запрос",
            show_alert=True,
        )
        return

    await callback.answer(
        "Ищу подходящие товары…"
    )

    async with async_session_maker() as session:
        products = await search_products(
            session=session,
            query=query,
            limit=30,
        )

    if not products:
        await callback.message.answer(
            (
                "🔍 По выбранному уточнению "
                "ничего не найдено.\n\n"
                "Попробуйте другой вариант."
            )
        )
        return

    suggestions = [
        {
            "product_id": product.id,
            "name": product.name,
            "brand": brand.name,
            "score": 0.0,
        }
        for product, brand, _category
        in products[:10]
    ]

    try:
        await callback.message.edit_text(
            (
                "🔍 <b>Подходящие товары</b>\n\n"
                f"Уточнение: «{escape(title)}»\n"
                f"Найдено вариантов: "
                f"<b>{len(products)}</b>\n\n"
                "Выберите товар:"
            ),
            reply_markup=(
                get_search_suggestions_keyboard(
                    suggestions
                )
            ),
        )

    except TelegramBadRequest:
        logger.warning(
            "Не удалось изменить сообщение уточнений",
            exc_info=True,
        )

        await callback.message.answer(
            (
                "🔍 <b>Подходящие товары</b>\n\n"
                f"Уточнение: «{escape(title)}»\n"
                f"Найдено вариантов: "
                f"<b>{len(products)}</b>\n\n"
                "Выберите товар:"
            ),
            reply_markup=(
                get_search_suggestions_keyboard(
                    suggestions
                )
            ),
        )

    clear_intent_groups(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
    )
