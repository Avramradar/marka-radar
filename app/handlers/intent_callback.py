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
    PRODUCTS_PER_PAGE,
    get_paginated_products_keyboard,
)
from app.search.intent_state import (
    clear_intent_groups,
    get_intent_group,
)
from app.search.product_list_state import (
    get_product_list,
    save_product_list,
)


router = Router()
logger = logging.getLogger(__name__)


def build_products_page_text(
    *,
    title: str,
    total_products: int,
    page: int,
) -> str:
    """
    Формирует текст сообщения со списком товаров.
    """

    total_pages = max(
        1,
        (
            total_products
            + PRODUCTS_PER_PAGE
            - 1
        )
        // PRODUCTS_PER_PAGE,
    )

    safe_page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    first_product_number = (
        safe_page
        * PRODUCTS_PER_PAGE
        + 1
    )

    last_product_number = min(
        (
            safe_page + 1
        )
        * PRODUCTS_PER_PAGE,
        total_products,
    )

    return (
        "🔍 <b>Подходящие товары</b>\n\n"
        f"Уточнение: «{escape(title)}»\n"
        f"Найдено вариантов: "
        f"<b>{total_products}</b>\n"
        f"Показаны товары: "
        f"<b>{first_product_number}–"
        f"{last_product_number}</b>\n"
        f"Страница: "
        f"<b>{safe_page + 1} из "
        f"{total_pages}</b>\n\n"
        "Выберите товар:"
    )


async def show_products_page(
    *,
    callback: CallbackQuery,
    page: int,
) -> None:
    """
    Показывает выбранную страницу сохранённого
    списка товаров.
    """

    if callback.message is None:
        await callback.answer()
        return

    state = get_product_list(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
    )

    if state is None:
        await callback.answer(
            (
                "Список товаров устарел. "
                "Повторите поиск."
            ),
            show_alert=True,
        )
        return

    total_products = len(
        state.products
    )

    if total_products == 0:
        await callback.answer(
            "Список товаров пуст",
            show_alert=True,
        )
        return

    total_pages = max(
        1,
        (
            total_products
            + PRODUCTS_PER_PAGE
            - 1
        )
        // PRODUCTS_PER_PAGE,
    )

    safe_page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    text = build_products_page_text(
        title=state.title,
        total_products=total_products,
        page=safe_page,
    )

    keyboard = get_paginated_products_keyboard(
        products=state.products,
        page=safe_page,
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
        )

    except TelegramBadRequest as error:
        error_text = str(error).lower()

        # Telegram возвращает эту ошибку,
        # если пользователь нажал на уже открытую страницу.
        if "message is not modified" not in error_text:
            logger.warning(
                "Не удалось обновить страницу товаров",
                exc_info=True,
            )

    await callback.answer()


@router.callback_query(
    F.data.startswith("intent:")
)
async def intent_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает выбор уточняющей группы.

    Пример:

    intent:0

    может соответствовать запросу:

    сельдь в масле
    """

    if callback.data is None:
        return

    if callback.message is None:
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

    try:
        async with async_session_maker() as session:
            products = await search_products(
                session=session,
                query=query,
                limit=100,
            )

    except Exception:
        logger.exception(
            "Ошибка поиска товаров "
            "для уточнения: %s",
            query,
        )

        await callback.message.answer(
            "⚠️ Не удалось выполнить поиск.\n"
            "Попробуйте ещё раз немного позже."
        )
        return

    if not products:
        await callback.message.answer(
            (
                "🔍 По выбранному уточнению "
                "ничего не найдено.\n\n"
                "Попробуйте другой вариант."
            )
        )
        return

    product_items = [
        {
            "product_id": product.id,
            "name": product.name,
            "brand": brand.name,
        }
        for product, brand, _category
        in products
    ]

    save_product_list(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        title=title,
        products=product_items,
    )

    clear_intent_groups(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
    )

    text = build_products_page_text(
        title=title,
        total_products=len(product_items),
        page=0,
    )

    keyboard = get_paginated_products_keyboard(
        products=product_items,
        page=0,
    )

    try:
        await callback.message.edit_text(
            text,
            reply_markup=keyboard,
        )

    except TelegramBadRequest:
        logger.warning(
            "Не удалось заменить список уточнений "
            "на список товаров",
            exc_info=True,
        )

        await callback.message.answer(
            text,
            reply_markup=keyboard,
        )


@router.callback_query(
    F.data.startswith("products_page:")
)
async def products_page_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Переключает страницы списка товаров.
    """

    if callback.data is None:
        return

    try:
        page = int(
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
            "Некорректная страница",
            show_alert=True,
        )
        return

    await show_products_page(
        callback=callback,
        page=page,
    )


@router.callback_query(
    F.data == "products_page_info"
)
async def products_page_info_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает нажатие на кнопку номера страницы.
    """

    if callback.message is None:
        await callback.answer()
        return

    state = get_product_list(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
    )

    if state is None:
        await callback.answer(
            (
                "Список товаров устарел. "
                "Повторите поиск."
            ),
            show_alert=True,
        )
        return

    total_pages = max(
        1,
        (
            len(state.products)
            + PRODUCTS_PER_PAGE
            - 1
        )
        // PRODUCTS_PER_PAGE,
    )

    await callback.answer(
        f"Всего страниц: {total_pages}"
    )
