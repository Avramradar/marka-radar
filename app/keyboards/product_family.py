from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def get_product_families_keyboard(
    families: list[dict],
):
    """
    Создаёт клавиатуру со списком семейств товаров.

    Пример:

    Сельдь филе в масле · 18
    Сельдь слабосолёная · 11
    Сельдь по-царски · 6
    """

    builder = InlineKeyboardBuilder()

    for family in families:
        family_id = int(
            family["family_id"]
        )

        family_name = str(
            family["name"]
        ).strip()

        products_count = int(
            family.get(
                "products_count",
                0,
            )
        )

        button_text = family_name

        if products_count > 0:
            button_text = (
                f"{family_name} · "
                f"{products_count}"
            )

        builder.row(
            InlineKeyboardButton(
                text=button_text[:64],
                callback_data=(
                    f"family:{family_id}"
                ),
            )
        )

    return builder.as_markup()
