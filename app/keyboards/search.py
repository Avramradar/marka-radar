from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


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

    Например:
    - Сельдь в масле
    - Сельдь слабосолёная
    - Филе сельди
    """

    builder = InlineKeyboardBuilder()

    for index, group in enumerate(groups):
        count = int(group.get("count", 0))

        button_text = group["title"]

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
