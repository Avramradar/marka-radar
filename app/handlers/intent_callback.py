import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from app.database.session import async_session_maker
from app.handlers.search import (
    process_pipeline_result,
)
from app.keyboards.search import (
    PRODUCTS_PER_PAGE,
    get_intent_groups_keyboard,
    get_paginated_products_keyboard,
)
from app.search.intent_state import (
    clear_intent_groups,
    get_intent_group,
    save_intent_groups,
)
from app.search.product_list_state import (
    get_product_list,
)
from app.search.search_pipeline import (
    SearchPipelineScreen,
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

    Старая пагинация временно остаётся
    для других обработчиков, которые ещё
    используют product_list_state.
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

    Функция оставлена для совместимости
    со старыми обработчиками пагинации.
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
                "Не удалось обновить "
                "страницу товаров",
                exc_info=True,
            )

    await callback.answer()


async def remove_old_keyboard(
    callback: CallbackQuery,
) -> None:
    """
    Убирает кнопки предыдущего уровня поиска.

    Это предотвращает повторные нажатия
    по уже устаревшему набору фасетов.
    """

    if callback.message is None:
        return

    try:
        await callback.message.edit_reply_markup(
            reply_markup=None
        )

    except TelegramBadRequest as error:
        error_text = str(
            error
        ).lower()

        if "message is not modified" not in error_text:
            logger.debug(
                "Не удалось убрать старую "
                "клавиатуру уточнений",
                exc_info=True,
            )


async def show_next_intents(
    *,
    callback: CallbackQuery,
    normalized_query: str,
    groups: list[dict],
) -> None:
    """
    Показывает следующий уровень фасетов.

    Важный момент: состояние сохраняется
    по callback.from_user.id — это настоящий
    пользователь, нажавший кнопку.

    Нельзя использовать message.from_user.id,
    потому что автором сообщения является бот.
    """

    if callback.message is None:
        return

    save_intent_groups(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        groups=groups,
    )

    await remove_old_keyboard(
        callback
    )

    await callback.message.answer(
        "🧭 <b>Что именно вы ищете?</b>\n\n"
        f"Запрос: «{escape(normalized_query)}»\n\n"
        "Выберите следующий параметр. "
        "После уточнения MarkaRadar сравнит "
        "товары по оценкам и уровню доверия:",
        reply_markup=(
            get_intent_groups_keyboard(
                groups
            )
        ),
    )


@router.callback_query(
    F.data.startswith("intent:")
)
async def intent_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает выбор фасета.

    Правильная рекурсивная цепочка:

        молоко
        → ультрапастеризованное
        → 3,2%
        → Search Pipeline
        → следующий фасет или Decision Search.

    Состояние каждого нового уровня сохраняется
    под ID реального пользователя.
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
                "Recursive facet: "
                "user_id=%s, title=%r, query=%r, "
                "normalized_query=%r, screen=%s, "
                "intents=%s, families=%s, "
                "candidates=%s",
                callback.from_user.id,
                title,
                query,
                pipeline_result.normalized_query,
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

            # Новый уровень фасетов обрабатываем
            # непосредственно здесь.
            #
            # process_pipeline_result() нельзя
            # использовать для INTENTS после
            # callback, потому что внутри него
            # message.from_user — это бот.
            if (
                pipeline_result.screen
                == SearchPipelineScreen.INTENTS
            ):
                groups = (
                    pipeline_result.intent_groups
                )

                if not groups:
                    logger.warning(
                        "Pipeline вернул INTENTS "
                        "без групп: query=%r",
                        query,
                    )

                    clear_intent_groups(
                        chat_id=(
                            callback.message.chat.id
                        ),
                        user_id=callback.from_user.id,
                    )

                    await remove_old_keyboard(
                        callback
                    )

                    await callback.message.answer(
                        "⚠️ Не удалось построить "
                        "следующее уточнение.\n"
                        "Попробуйте выполнить "
                        "новый поиск."
                    )
                    return

                await show_next_intents(
                    callback=callback,
                    normalized_query=(
                        pipeline_result
                        .normalized_query
                    ),
                    groups=groups,
                )
                return

            # Если фасеты закончились,
            # старое состояние больше не нужно.
            clear_intent_groups(
                chat_id=callback.message.chat.id,
                user_id=callback.from_user.id,
            )

            await remove_old_keyboard(
                callback
            )

            # DECISION, FAMILIES, NOT_FOUND
            # и BARCODE отображает единый
            # обработчик Search Pipeline.
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
            "Попробуйте повторить поиск "
            "немного позже."
        )


@router.callback_query(
    F.data.startswith("products_page:")
)
async def products_page_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Переключает страницы сохранённого
    списка товаров.

    Оставлено для совместимости
    со старыми экранами.
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
    старого сохранённого списка.
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
