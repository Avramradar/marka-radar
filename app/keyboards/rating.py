from aiogram.types import InlineKeyboardButton
from aiogram.types import InlineKeyboardMarkup


def get_rating_keyboard(
    product_id: int,
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="1",
                    callback_data=f"rate:{product_id}:1",
                ),
                InlineKeyboardButton(
                    text="2",
                    callback_data=f"rate:{product_id}:2",
                ),
                InlineKeyboardButton(
                    text="3",
                    callback_data=f"rate:{product_id}:3",
                ),
                InlineKeyboardButton(
                    text="4",
                    callback_data=f"rate:{product_id}:4",
                ),
                InlineKeyboardButton(
                    text="5",
                    callback_data=f"rate:{product_id}:5",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="6",
                    callback_data=f"rate:{product_id}:6",
                ),
                InlineKeyboardButton(
                    text="7",
                    callback_data=f"rate:{product_id}:7",
                ),
                InlineKeyboardButton(
                    text="8",
                    callback_data=f"rate:{product_id}:8",
                ),
                InlineKeyboardButton(
                    text="9",
                    callback_data=f"rate:{product_id}:9",
                ),
                InlineKeyboardButton(
                    text="10",
                    callback_data=f"rate:{product_id}:10",
                ),
            ],
        ]
    )
