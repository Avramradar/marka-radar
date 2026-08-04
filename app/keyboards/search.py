from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


PRODUCTS_PER_PAGE = 10


def get_search_suggestions_keyboard(
    suggestions: list[dict],
):
    """
    Создаёт клавиатуру с конкретными товарами.
    """

    builder = InlineKeyboardBuilder()

    for suggestion in suggestions:
        builder.row(
            InlineKeyboardButton(
                text=(
                    f"{suggestion['brand']} — "
                    f"{suggestion['name']}"
                )[:64],
                callback_data=(
                    f"product:{suggestion['product_id']}"
                ),
            )
        )

    return builder.as_markup()


def get_intent_groups_keyboard(
    groups: list[dict],
):
    """
    Создаёт клавиатуру с уточняющими группами.
    """

    builder = InlineKeyboardBuilder()

    for index, group in enumerate(groups):
        count = int(
            group.get(
                "count",
                0,
            )
        )

        button_text = str(
            group["title"]
        )

        if count > 0:
            button_text = (
                f"{button_text} · {count}"
            )

        builder.row(
            InlineKeyboardButton(
                text=button_text[:64],
                callback_data=(
                    f"intent:{index}"
                ),
            )
        )

    return builder.as_markup()


def get_paginated_products_keyboard(
    *,
    products: list[dict],
    page: int,
):
    """
    Создаёт клавиатуру списка товаров
    с кнопками пагинации.

    Callback:
    product:<id> — открыть карточку
    products_page:<page> — открыть страницу
    products_page_info — неактивная кнопка номера страницы
    """

    builder = InlineKeyboardBuilder()

    total_products = len(products)

    if total_products == 0:
        return builder.as_markup()

    total_pages = (
        total_products
        + PRODUCTS_PER_PAGE
        - 1
    ) // PRODUCTS_PER_PAGE

    safe_page = max(
        0,
        min(
            page,
            total_pages - 1,
        ),
    )

    start_index = (
        safe_page
        * PRODUCTS_PER_PAGE
    )

    end_index = (
        start_index
        + PRODUCTS_PER_PAGE
    )

    page_products = products[
        start_index:end_index
    ]

    for product in page_products:
        builder.row(
            InlineKeyboardButton(
                text=(
                    f"{product['brand']} — "
                    f"{product['name']}"
                )[:64],
                callback_data=(
                    f"product:{product['product_id']}"
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
                    f"products_page:{safe_page - 1}"
                ),
            )
        )

    navigation_buttons.append(
        InlineKeyboardButton(
            text=(
                f"{safe_page + 1} / "
                f"{total_pages}"
            ),
            callback_data="products_page_info",
        )
    )

    if safe_page < total_pages - 1:
        navigation_buttons.append(
            InlineKeyboardButton(
                text="Далее ➡️",
                callback_data=(
                    f"products_page:{safe_page + 1}"
                ),
            )
        )

    builder.row(
        *navigation_buttons
    )

    return builder.as_markup()
