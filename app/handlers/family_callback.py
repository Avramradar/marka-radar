import logging
from decimal import Decimal
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.database.models.brand import Brand
from app.database.models.product import Product
from app.database.repositories.product_family_search_repository import (
    get_product_family,
)
from app.database.session import async_session_maker
from app.keyboards.search import (
    get_paginated_products_keyboard,
)
from app.search.product_list_state import (
    save_product_list,
)


router = Router()
logger = logging.getLogger(__name__)


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


GENERIC_PRODUCT_NAMES = {
    "кофе",
    "молоко",
    "сельдь",
    "пицца",
    "чай",
    "вода",
    "сыр",
    "масло",
    "йогурт",
}


def normalize_value(
    value: Any,
) -> str:
    """
    Нормализует текст для сравнения.
    """

    return " ".join(
        str(
            value or ""
        )
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def is_real_brand(
    brand_name: str | None,
) -> bool:
    """
    Проверяет, указан ли настоящий бренд.
    """

    return (
        normalize_value(
            brand_name
        )
        not in UNKNOWN_BRAND_NAMES
    )


def format_number(
    value: Decimal | float | int | None,
) -> str:
    """
    Убирает лишние нули у веса и объёма.

    Примеры:

    1000.000 -> 1000
    0.450 -> 0.45
    1.500 -> 1.5
    """

    if value is None:
        return ""

    decimal_value = Decimal(
        str(value)
    )

    if (
        decimal_value
        == decimal_value.to_integral()
    ):
        return str(
            int(decimal_value)
        )

    return format(
        decimal_value.normalize(),
        "f",
    )


def format_package(
    product: Product,
) -> str:
    """
    Формирует короткое описание упаковки.
    """

    package_value = getattr(
        product,
        "package_value",
        None,
    )

    package_unit = str(
        getattr(
            product,
            "package_unit",
            "",
        )
        or ""
    ).strip()

    if (
        package_value is None
        or not package_unit
    ):
        return ""

    return (
        f"{format_number(package_value)} "
        f"{package_unit}"
    )


def clean_subtype(
    product: Product,
) -> str:
    """
    Возвращает очищенный подтип товара.
    """

    return " ".join(
        str(
            getattr(
                product,
                "subtype",
                "",
            )
            or ""
        )
        .strip()
        .split()
    )


def is_generic_product_name(
    *,
    product_name: str,
    family_name: str,
) -> bool:
    """
    Проверяет, является ли название слишком общим.

    Например:

    Кофе
    Молоко
    Пицца
    """

    normalized_product_name = normalize_value(
        product_name
    )

    normalized_family_name = normalize_value(
        family_name
    )

    if (
        normalized_product_name
        == normalized_family_name
    ):
        return True

    return (
        normalized_product_name
        in GENERIC_PRODUCT_NAMES
    )


def build_product_display_name(
    *,
    product: Product,
    family_name: str,
) -> str:
    """
    Формирует информативное название кнопки.

    Примеры:

    Кофе · растворимый · 100 г
    Кофе · в зёрнах · 1 кг
    Молоко · ультрапастеризованное · 3,2% · 1 л

    Если обычное название уже подробное,
    оно сохраняется без лишнего дублирования.
    """

    product_name = " ".join(
        str(
            product.name or ""
        )
        .strip()
        .split()
    )

    if not product_name:
        product_name = family_name

    subtype = clean_subtype(
        product
    )

    package = format_package(
        product
    )

    parts = [
        product_name,
    ]

    normalized_product_name = normalize_value(
        product_name
    )

    normalized_subtype = normalize_value(
        subtype
    )

    generic_name = is_generic_product_name(
        product_name=product_name,
        family_name=family_name,
    )

    # Подтип особенно важен для карточек
    # с общим названием вроде «Кофе».
    if (
        subtype
        and normalized_subtype
        not in normalized_product_name
    ):
        parts.append(
            subtype
        )

    # Для общего названия добавляем упаковку,
    # чтобы отличать похожие позиции.
    if package:
        normalized_package = normalize_value(
            package
        )

        full_text = normalize_value(
            " ".join(parts)
        )

        if normalized_package not in full_text:
            parts.append(
                package
            )

    display_name = " · ".join(
        part
        for part in parts
        if part
    )

    # Если данных всё равно нет,
    # сохраняем хотя бы обычное имя.
    if not display_name:
        display_name = product_name

    # Для подробного исходного имени не нужно
    # искусственно добавлять технические данные.
    if (
        not generic_name
        and len(product_name) >= 12
        and not subtype
        and not package
    ):
        return product_name

    return display_name


def product_sort_key(
    item: dict[str, Any],
) -> tuple[
    int,
    int,
    int,
    str,
    int,
]:
    """
    Сортирует товары перед показом.

    Сначала:

    1. товары с настоящим брендом;
    2. информативные названия;
    3. более длинные названия;
    4. алфавит;
    5. ID товара.
    """

    brand_name = str(
        item.get(
            "brand",
            "",
        )
    )

    display_name = str(
        item.get(
            "name",
            "",
        )
    )

    family_name = str(
        item.get(
            "family_name",
            "",
        )
    )

    has_real_brand_order = (
        0
        if is_real_brand(
            brand_name
        )
        else 1
    )

    generic_name_order = (
        1
        if is_generic_product_name(
            product_name=display_name,
            family_name=family_name,
        )
        else 0
    )

    return (
        has_real_brand_order,
        generic_name_order,
        -len(display_name),
        normalize_value(display_name),
        int(
            item.get(
                "product_id",
                0,
            )
        ),
    )


def make_product_titles_unique(
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Делает одинаковые кнопки различимыми.

    Сначала пытается добавить упаковку и подтип.
    Если названия всё равно совпадают, добавляет
    короткий номер товара.

    Товары при этом не удаляются.
    """

    title_counts: dict[
        tuple[str, str],
        int,
    ] = {}

    for item in products:
        key = (
            normalize_value(
                item.get(
                    "brand",
                    "",
                )
            ),
            normalize_value(
                item.get(
                    "name",
                    "",
                )
            ),
        )

        title_counts[key] = (
            title_counts.get(
                key,
                0,
            )
            + 1
        )

    used_titles: set[
        tuple[str, str]
    ] = set()

    prepared_products: list[
        dict[str, Any]
    ] = []

    for item in products:
        prepared_item = dict(
            item
        )

        brand = str(
            prepared_item.get(
                "brand",
                "",
            )
        )

        name = str(
            prepared_item.get(
                "name",
                "",
            )
        )

        normalized_key = (
            normalize_value(
                brand
            ),
            normalize_value(
                name
            ),
        )

        final_name = name

        if (
            title_counts.get(
                normalized_key,
                0,
            )
            > 1
        ):
            barcode = str(
                prepared_item.get(
                    "barcode",
                    "",
                )
                or ""
            ).strip()

            if barcode:
                final_name = (
                    f"{name} · "
                    f"{barcode[-4:]}"
                )
            else:
                product_id = int(
                    prepared_item[
                        "product_id"
                    ]
                )

                final_name = (
                    f"{name} · №{product_id}"
                )

        final_key = (
            normalize_value(
                brand
            ),
            normalize_value(
                final_name
            ),
        )

        # Дополнительная страховка:
        # одинаковых callback-кнопок по тексту
        # больше не останется.
        if final_key in used_titles:
            product_id = int(
                prepared_item[
                    "product_id"
                ]
            )

            final_name = (
                f"{final_name} · "
                f"ID {product_id}"
            )

            final_key = (
                normalize_value(
                    brand
                ),
                normalize_value(
                    final_name
                ),
            )

        used_titles.add(
            final_key
        )

        prepared_item[
            "name"
        ] = final_name

        prepared_products.append(
            prepared_item
        )

    return prepared_products


@router.callback_query(
    F.data.startswith("family:")
)
async def family_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Показывает товары выбранного семейства.

    Пример:

    family:15

    может соответствовать семейству:

    Сельдь филе в масле
    """

    if callback.data is None:
        return

    if callback.message is None:
        await callback.answer()
        return

    try:
        family_id = int(
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
            "Некорректное семейство",
            show_alert=True,
        )
        return

    await callback.answer(
        "Загружаю товары…"
    )

    try:
        async with async_session_maker() as session:
            family = await get_product_family(
                session=session,
                family_id=family_id,
            )

            if family is None:
                await callback.message.answer(
                    "Семейство не найдено."
                )
                return

            statement = (
                select(
                    Product,
                    Brand,
                )
                .join(
                    Brand,
                    Product.brand_id
                    == Brand.id,
                )
                .where(
                    Product.family_id
                    == family_id,
                    Product.is_active.is_(
                        True
                    ),
                )
                .limit(
                    100
                )
            )

            result = await session.execute(
                statement
            )

            rows = list(
                result.all()
            )

    except Exception:
        logger.exception(
            "Ошибка загрузки семейства %s",
            family_id,
        )

        await callback.message.answer(
            "⚠️ Не удалось загрузить товары.\n"
            "Попробуйте повторить немного позже."
        )
        return

    if not rows:
        await callback.message.answer(
            "В этом семействе пока нет "
            "активных товаров."
        )
        return

    products = [
        {
            "product_id": int(
                product.id
            ),
            "name": build_product_display_name(
                product=product,
                family_name=str(
                    family.name
                ),
            ),
            "brand": (
                str(
                    brand.name or ""
                ).strip()
                if is_real_brand(
                    brand.name
                )
                else ""
            ),
            "barcode": str(
                product.barcode or ""
            ),
            "family_name": str(
                family.name
            ),
        }
        for product, brand
        in rows
    ]

    products.sort(
        key=product_sort_key
    )

    products = make_product_titles_unique(
        products
    )

    # В состояние сохраняем только те поля,
    # которые используются клавиатурой.
    stored_products = [
        {
            "product_id": item[
                "product_id"
            ],
            "name": item[
                "name"
            ],
            "brand": item[
                "brand"
            ],
        }
        for item in products
    ]

    save_product_list(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        title=str(
            family.name
        ),
        products=stored_products,
    )

    total_products = len(
        stored_products
    )

    text = (
        "🧺 <b>Товары выбранного вида</b>\n\n"
        f"Семейство: «"
        f"{escape(str(family.name))}»\n"
        f"Найдено товаров: "
        f"<b>{total_products}</b>\n\n"
        "Выберите товар:"
    )

    keyboard = (
        get_paginated_products_keyboard(
            products=stored_products,
            page=0,
        )
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

        if (
            "message is not modified"
            not in error_text
        ):
            logger.warning(
                "Не удалось заменить список "
                "семейств на список товаров",
                exc_info=True,
            )

            await callback.message.answer(
                text,
                reply_markup=keyboard,
            )
