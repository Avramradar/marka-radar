import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.database.session import async_session_maker
from app.handlers.search import process_pipeline_result
from app.keyboards.search import (
    PRODUCTS_PER_PAGE,
    get_paginated_products_keyboard,
)
from app.search.intent_state import (
    get_intent_group,
)
from app.search.product_list_state import (
    get_product_list,
)
from app.search.search_pipeline import (
    run_search_pipeline,
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
    Формирует текст страницы сохранённого списка.

    Этот механизм временно сохраняется для старых
    списков, которые ещё могут создаваться
    обработчиками семейств товаров.
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
    Показывает страницу ранее сохранённого
    списка товаров.

    Оставлено для совместимости с обработчиком
    семейств товаров и старой пагинацией.
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
        error_text = str(
            error
        ).lower()

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
    Обрабатывает выбор поискового фасета.

    Новый сценарий:

        выбранный фасет
        → полный уточнённый запрос
        → Search Pipeline
        → следующий фасет или Decision Search.

    Обработчик сам больше не получает
    и не сортирует список из 100 товаров.
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
        "Применяю уточнение…"
    )

    try:
        async with async_session_maker() as session:
            pipeline_result = (
                await run_search_pipeline(
                    session=session,
                    query=query,
                    intent_limit=6,
                    family_limit=6,
                    decision_candidates_limit=30,
                    allow_refinements=True,
                )
            )

            logger.info(
                "Recursive facet: title=%r, "
                "query=%r, screen=%s, "
                "intents=%s, families=%s, "
                "candidates=%s",
                title,
                query,
                pipeline_result.screen,
                len(
                    pipeline_result.intent_groups
                ),
                len(
                    pipeline_result.families
                ),
                (
                    pipeline_result
                    .decision
                    .total_candidates
                    if pipeline_result.decision
                    else 0
                ),
            )

            # Убираем старые кнопки перед показом
            # следующего этапа поиска.
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
                        "Не удалось убрать старую "
                        "клавиатуру фасетов",
                        exc_info=True,
                    )

            # Все решения о следующем экране
            # принимает только Search Pipeline.
            await process_pipeline_result(
                message=callback.message,
                session=session,
                pipeline_result=pipeline_result,
            )

    except Exception:
        logger.exception(
            "Ошибка повторного Search Pipeline "
            "после выбора фасета %r: %s",
            title,
            query,
        )

        await callback.message.answer(
            "⚠️ Не удалось применить уточнение.\n"
            "Попробуйте повторить поиск немного позже."
        )


@router.callback_query(
    F.data.startswith("products_page:")
)
async def products_page_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Переключает страницы сохранённого списка.

    Оставлено для совместимости с теми участками,
    которые пока используют старую пагинацию.
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
    Показывает количество страниц
    сохранённого списка товаров.
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
