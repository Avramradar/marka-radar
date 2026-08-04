from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_search_suggestions_keyboard(
    suggestions: list[dict],
):
    """
    Создает клавиатуру с подсказками поиска.
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
