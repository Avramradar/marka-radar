from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.search.decision_search import (
    DecisionProduct,
    DecisionSearchResult,
)


def build_product_button_text(
    item: DecisionProduct,
    *,
    prefix: str | None = None,
) -> str:
    """
    Формирует короткий и понятный текст кнопки.

    Сначала показывается смысловой статус,
    затем бренд и название товара.
    """

    title_parts: list[str] = []

    if prefix:
        title_parts.append(prefix)

    if item.brand_name:
        title_parts.append(
            f"{item.brand_name} — {item.name}"
        )
    else:
        title_parts.append(
            item.name
        )

    title_parts.append(
        (
            f"⭐ {item.average_rating:.1f}"
            if item.votes_count > 0
            else "без оценок"
        )
    )

    return " · ".join(
        title_parts
    )[:64]


def get_decision_search_keyboard(
    result: DecisionSearchResult,
):
    """
    Создаёт клавиатуру первого экрана
    помощника выбора MarkaRadar.

    Структура:

    🏆 лучший подтверждённый выбор
    👍 хорошие альтернативы
    ⚪ товары с недостатком данных
    📋 остальные результаты
    """

    builder = InlineKeyboardBuilder()

    if result.best_choice is not None:
        builder.row(
            InlineKeyboardButton(
                text=build_product_button_text(
                    result.best_choice,
                    prefix="🏆",
                ),
                callback_data=(
                    f"product:"
                    f"{result.best_choice.product_id}"
                ),
            )
        )

    for item in result.alternatives:
        builder.row(
            InlineKeyboardButton(
                text=build_product_button_text(
                    item,
                    prefix="👍",
                ),
                callback_data=(
                    f"product:{item.product_id}"
                ),
            )
        )

    for item in result.insufficient_data:
        builder.row(
            InlineKeyboardButton(
                text=build_product_button_text(
                    item,
                    prefix="⚪",
                ),
                callback_data=(
                    f"product:{item.product_id}"
                ),
            )
        )

    if result.other_products:
        builder.row(
            InlineKeyboardButton(
                text=(
                    "📋 Показать ещё варианты"
                ),
                callback_data=(
                    "decision_more:0"
                ),
            )
        )

    return builder.as_markup()


def get_decision_more_keyboard(
    *,
    products: list[DecisionProduct],
    page: int,
    products_per_page: int = 8,
):
    """
    Создаёт клавиатуру остальных результатов
    с простой пагинацией.

    Callback:

    product:<id>
    decision_more:<page>
    decision_more_info
    """

    builder = InlineKeyboardBuilder()

    if not products:
        return builder.as_markup()

    safe_products_per_page = max(
        1,
        min(
            products_per_page,
            10,
        ),
    )

    total_products = len(
        products
    )

    total_pages = (
        total_products
        + safe_products_per_page
        - 1
    ) // safe_products_per_page

    safe_page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    start_index = (
        safe_page
        * safe_products_per_page
    )

    end_index = (
        start_index
        + safe_products_per_page
    )

    page_products = products[
        start_index:end_index
    ]

    for item in page_products:
        builder.row(
            InlineKeyboardButton(
                text=build_product_button_text(
                    item
                ),
                callback_data=(
                    f"product:{item.product_id}"
                ),
            )
        )

    navigation_buttons: list[
        InlineKeyboardButton
    ] = []

    if safe_page > 0:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=(
                    f"decision_more:"
                    f"{safe_page - 1}"
                ),
            )
        )

    navigation_buttons.append(
        InlineKeyboardButton(
            text=(
                f"{safe_page + 1} / "
                f"{total_pages}"
            ),
            callback_data=(
                "decision_more_info"
            ),
        )
    )

    if safe_page < total_pages - 1:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="Далее ➡️",
                callback_data=(
                    f"decision_more:"
                    f"{safe_page + 1}"
                ),
            )
        )

    builder.row(
        *navigation_buttons
    )

    builder.row(
        InlineKeyboardButton(
            text="↩️ К лучшим вариантам",
            callback_data=(
                "decision_back"
            ),
        )
    )

    return builder.as_markup()
