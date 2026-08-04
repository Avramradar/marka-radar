import logging
from contextlib import suppress
from decimal import Decimal
from html import escape
from pathlib import Path
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

from app.database.repositories.product_repository import (
    search_products,
)
from app.database.session import async_session_maker
from app.keyboards.rating import get_rating_keyboard
from app.keyboards.search import (
    get_search_suggestions_keyboard,
)
from app.search.suggestions import get_search_suggestions
from app.services.price_service import get_price_statistics
from app.services.rating_service import (
    get_full_product_rating,
)


router = Router()
logger = logging.getLogger(__name__)


SEARCH_LOADER_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "markaradar_runner_shops.gif"
)


def format_number(
    value: Decimal | float | int | None,
) -> str:
    """
    Убирает лишние нули у веса и объёма.

    Примеры:
    245.000 -> 245
    0.450 -> 0.45
    1.500 -> 1.5
    """

    if value is None:
        return ""

    decimal_value = Decimal(str(value))

    if decimal_value == decimal_value.to_integral():
        return str(int(decimal_value))

    return format(
        decimal_value.normalize(),
        "f",
    )


def format_package(
    package_value: Decimal | float | int | None,
    package_unit: str | None,
) -> str:
    if package_value is None or not package_unit:
        return "не указана"

    return (
        f"{format_number(package_value)} "
        f"{escape(package_unit)}"
    )


def format_subtype(
    subtype: str | None,
) -> str:
    if not subtype:
        return "не указан"

    cleaned_subtype = subtype.strip()

    if not cleaned_subtype:
        return "не указан"

    return escape(
        cleaned_subtype[0].upper()
        + cleaned_subtype[1:]
    )


def format_rating_text(
    rating: dict[str, float | int],
) -> str:
    votes_count = int(
        rating["votes_count"]
    )

    average_rating = float(
        rating["average_rating"]
    )

    if votes_count == 0:
        return (
            "⭐ <b>Оценок пока нет</b>\n"
            "Будьте первым, кто оценит этот товар."
        )

    if votes_count < 5:
        confidence = "пока недостаточно подтверждён"
    elif votes_count < 20:
        confidence = "средняя"
    else:
        confidence = "высокая"

    return (
        "⭐ <b>Рейтинг пользователей:</b> "
        f"{average_rating:.1f} из 10\n"
        "👥 <b>Количество оценок:</b> "
        f"{votes_count}\n"
        "🛡 <b>Достоверность:</b> "
        f"{confidence}"
    )


def format_price_text(
    price_stats: dict[str, Any] | None,
) -> str:
    if price_stats is None:
        return (
            "💰 <b>Цена пока не собрана</b>\n"
            "Данные появятся после подключения "
            "источников цен."
        )

    median_price = float(
        price_stats["median"]
    )

    minimum = float(
        price_stats["minimum"]
    )

    maximum = float(
        price_stats["maximum"]
    )

    spread = float(
        price_stats["spread"]
    )

    spread_percent = float(
        price_stats["spread_percent"]
    )

    prices_count = int(
        price_stats["prices_count"]
    )

    lines = [
        (
            "💰 <b>Средняя цена по рынку:</b> "
            f"около {median_price:.0f} ₽"
        ),
        (
            "📊 <b>Диапазон цен:</b> "
            f"{minimum:.0f}–{maximum:.0f} ₽"
        ),
        (
            "🏪 <b>Найдено цен:</b> "
            f"{prices_count}"
        ),
    ]

    if spread > 0:
        lines.append(
            "↕️ <b>Разброс:</b> "
            f"{spread_percent:.0f}% "
            f"({spread:.0f} ₽)"
        )

    if spread >= 500:
        lines.append(
            "⚠️ <b>Очень большая разница в цене.</b>\n"
            "Перед покупкой обязательно "
            "сравните магазины."
        )

    elif spread_percent >= 40:
        lines.append(
            "⚠️ <b>Заметный разброс цен.</b>\n"
            "Стоимость лучше проверить "
            "перед покупкой."
        )

    return "\n".join(lines)


def build_product_card(
    *,
    product,
    brand,
    category,
    rating: dict[str, float | int],
    price_stats: dict[str, Any] | None,
) -> str:
    title = (
        f"<b>{escape(brand.name)} — "
        f"{escape(product.name)}</b>"
    )

    package_text = format_package(
        product.package_value,
        product.package_unit,
    )

    subtype_text = format_subtype(
        product.subtype
    )

    product_lines = [
        title,
        "",
        (
            "📂 <b>Категория:</b> "
            f"{escape(category.name)}"
        ),
        (
            "🏷 <b>Бренд:</b> "
            f"{escape(brand.name)}"
        ),
        (
            "🥫 <b>Тип:</b> "
            f"{subtype_text}"
        ),
        (
            "📦 <b>Упаковка:</b> "
            f"{package_text}"
        ),
    ]

    if product.barcode:
        product_lines.append(
            "🔢 <b>Штрихкод:</b> "
            f"<code>{escape(product.barcode)}</code>"
        )

    product_lines.extend(
        [
            "",
            format_price_text(price_stats),
            "",
            format_rating_text(rating),
            "",
            "Поставьте свою оценку 👇",
        ]
    )

    return "\n".join(product_lines)


async def send_search_loader(
    message: Message,
) -> Message | None:
    """
    Сразу показывает пользователю, что поиск начался.

    Если GIF отсутствует, отправляет обычное
    текстовое сообщение.
    """

    with suppress(Exception):
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
        )

    if not SEARCH_LOADER_PATH.is_file():
        logger.warning(
            "GIF загрузки не найден: %s",
            SEARCH_LOADER_PATH,
        )

        return await message.answer(
            "🔎 <b>Ищу товары…</b>\n"
            "Проверяю названия, бренды "
            "и похожие варианты."
        )

    try:
        animation = FSInputFile(
            SEARCH_LOADER_PATH
        )

        return await message.answer_animation(
            animation=animation,
            caption=(
                "🏃 <b>Бегу вдоль витрин…</b>\n"
                "Ищу подходящие товары и бренды."
            ),
        )

    except Exception:
        logger.exception(
            "Не удалось отправить GIF поиска"
        )

        return await message.answer(
            "🔎 <b>Ищу товары…</b>\n"
            "Проверяю названия, бренды "
            "и похожие варианты."
        )


async def remove_search_loader(
    loading_message: Message | None,
) -> None:
    """
    Удаляет GIF или текст загрузки,
    когда поиск завершён.
    """

    if loading_message is None:
        return

    with suppress(Exception):
        await loading_message.delete()


async def send_product_card(
    *,
    message: Message,
    product,
    brand,
    category,
    rating: dict[str, float | int],
    price_stats: dict[str, Any] | None,
) -> None:
    card_text = build_product_card(
        product=product,
        brand=brand,
        category=category,
        rating=rating,
        price_stats=price_stats,
    )

    keyboard = get_rating_keyboard(
        product.id
    )

    if product.image_url:
        try:
            await message.answer_photo(
                photo=product.image_url,
                caption=card_text,
                reply_markup=keyboard,
            )
            return

        except TelegramBadRequest:
            logger.warning(
                "Не удалось отправить "
                "изображение товара %s",
                product.id,
                exc_info=True,
            )

        except Exception:
            logger.exception(
                "Ошибка отправки "
                "изображения товара %s",
                product.id,
            )

    await message.answer(
        card_text,
        reply_markup=keyboard,
    )


async def show_single_product(
    *,
    message: Message,
    session,
    product,
    brand,
    category,
) -> None:
    """
    Загружает рейтинг и цены,
    затем отправляет карточку товара.
    """

    rating = await get_full_product_rating(
        session=session,
        product_id=product.id,
    )

    price_stats = await get_price_statistics(
        session=session,
        product_id=product.id,
    )

    await send_product_card(
        message=message,
        product=product,
        brand=brand,
        category=category,
        rating=rating,
        price_stats=price_stats,
    )


@router.message(F.text)
async def search_handler(
    message: Message,
) -> None:
    if message.text is None:
        return

    query = message.text.strip()

    if not query:
        await message.answer(
            "Введите название продукта или бренда."
        )
        return

    # Команды обрабатываются другими роутерами.
    if query.startswith("/"):
        return

    loading_message = await send_search_loader(
        message
    )

    try:
        async with async_session_maker() as session:
            # Для штрихкода сразу используем основной поиск.
            if query.isdigit():
                barcode_products = await search_products(
                    session=session,
                    query=query,
                    limit=1,
                )

                if barcode_products:
                    product, brand, category = (
                        barcode_products[0]
                    )

                    await show_single_product(
                        message=message,
                        session=session,
                        product=product,
                        brand=brand,
                        category=category,
                    )
                    return

            # Сначала получаем компактные подсказки.
            suggestions = await get_search_suggestions(
                session=session,
                query=query,
                limit=8,
            )

            if suggestions:
                await message.answer(
                    "🔍 <b>Лучшие совпадения</b>\n\n"
                    f"Запрос: «{escape(query)}»\n"
                    "Выберите подходящий товар:",
                    reply_markup=(
                        get_search_suggestions_keyboard(
                            suggestions
                        )
                    ),
                )
                return

            # Если индекс не дал результата,
            # используем расширенный поиск.
            products = await search_products(
                session=session,
                query=query,
                limit=20,
            )

            if not products:
                await message.answer(
                    "🔍 По вашему запросу "
                    "ничего не найдено.\n\n"
                    "Попробуйте:\n"
                    "• написать название короче;\n"
                    "• указать бренд;\n"
                    "• проверить написание;\n"
                    "• отправить штрихкод."
                )
                return

            # Единственный результат открываем сразу.
            if len(products) == 1:
                product, brand, category = products[0]

                await show_single_product(
                    message=message,
                    session=session,
                    product=product,
                    brand=brand,
                    category=category,
                )
                return

            # Создаём кнопки из результатов
            # резервного поиска.
            fallback_suggestions = [
                {
                    "product_id": product.id,
                    "name": product.name,
                    "brand": brand.name,
                    "score": 0.0,
                }
                for product, brand, _category
                in products[:8]
            ]

            await message.answer(
                "🔍 <b>Найдено несколько вариантов</b>\n\n"
                f"Запрос: «{escape(query)}»\n"
                "Нажмите на товар, "
                "чтобы открыть карточку:",
                reply_markup=(
                    get_search_suggestions_keyboard(
                        fallback_suggestions
                    )
                ),
            )

    except Exception:
        logger.exception(
            "Ошибка поиска по запросу: %s",
            query,
        )

        await message.answer(
            "⚠️ Во время поиска произошла ошибка.\n"
            "Попробуйте повторить запрос немного позже."
        )

    finally:
        await remove_search_loader(
            loading_message
        )
