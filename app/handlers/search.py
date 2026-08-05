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
from app.keyboards.product_family import (
    get_product_families_keyboard,
)
from app.keyboards.rating import get_rating_keyboard
from app.keyboards.search import (
    get_intent_groups_keyboard,
    get_search_suggestions_keyboard,
)
from app.search.engine import (
    SearchMode,
    run_search_engine,
)
from app.search.family_search import (
    find_product_families,
)
from app.search.intent_state import (
    clear_intent_groups,
    save_intent_groups,
)
from app.services.price_service import (
    get_price_statistics,
)
from app.services.rating_service import (
    get_full_product_rating,
)
from app.services.trust_engine import (
    TrustEngineResult,
    evaluate_product,
)


router = Router()
logger = logging.getLogger(__name__)


SEARCH_LOADER_PATH = (
    Path(__file__).resolve().parent.parent
    / "assets"
    / "markaradar_dino_shop_runner.gif"
)


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


def format_number(
    value: Decimal | float | int | None,
) -> str:
    """
    Убирает лишние нули у чисел.

    Примеры:
    245.000 -> 245
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
    package_value: Decimal | float | int | None,
    package_unit: str | None,
) -> str:
    """
    Форматирует вес или объём упаковки.
    """

    if (
        package_value is None
        or not package_unit
    ):
        return "не указана"

    return (
        f"{format_number(package_value)} "
        f"{escape(package_unit)}"
    )


def format_subtype(
    subtype: str | None,
) -> str:
    """
    Форматирует подтип товара.
    """

    if not subtype:
        return "не указан"

    cleaned_subtype = subtype.strip()

    if not cleaned_subtype:
        return "не указан"

    return escape(
        cleaned_subtype[0].upper()
        + cleaned_subtype[1:]
    )


def is_real_brand(
    brand_name: str | None,
) -> bool:
    """
    Проверяет, указан ли настоящий бренд.
    """

    normalized_brand = str(
        brand_name or ""
    ).strip().lower()

    return (
        normalized_brand
        not in UNKNOWN_BRAND_NAMES
    )


def build_product_title(
    *,
    product,
    brand,
) -> str:
    """
    Формирует заголовок карточки.

    Служебное значение «Бренд не указан»
    пользователю не показывается.
    """

    product_name = escape(
        product.name
    )

    if not is_real_brand(
        brand.name
    ):
        return f"<b>{product_name}</b>"

    return (
        f"<b>{escape(brand.name)} — "
        f"{product_name}</b>"
    )


def calculate_data_quality_score(
    *,
    product,
    brand,
    category,
    price_stats: dict[str, Any] | None,
) -> float:
    """
    Оценивает полноту карточки товара.

    Этот показатель не оценивает качество самого
    продукта. Он показывает, насколько достаточно
    информации для принятия решения.
    """

    score = 0.0

    product_name = str(
        product.name or ""
    ).strip()

    if product_name:
        score += 20.0

    if (
        product_name
        and len(product_name) >= 4
    ):
        score += 5.0

    if is_real_brand(
        brand.name
    ):
        score += 15.0

    category_name = str(
        category.name or ""
    ).strip()

    if category_name:
        score += 10.0

    if product.image_url:
        score += 15.0

    if product.barcode:
        score += 15.0

    if (
        product.package_value is not None
        and product.package_unit
    ):
        score += 10.0

    if product.description:
        score += 5.0

    if product.subtype:
        score += 3.0

    if product.keywords:
        score += 2.0

    if price_stats is not None:
        score += 5.0

    return min(
        score,
        100.0,
    )


def format_explanation(
    trust_result: TrustEngineResult,
) -> str:
    """
    Форматирует объяснение решения Trust Engine.
    """

    if not trust_result.explanation:
        return (
            "• Пока недостаточно информации "
            "для подробного объяснения."
        )

    return "\n".join(
        f"• {escape(reason)}"
        for reason in trust_result.explanation
    )


def format_trust_engine_text(
    trust_result: TrustEngineResult,
) -> str:
    """
    Формирует главный блок решения MarkaRadar.

    Этот блок всегда показывается раньше
    технических характеристик товара.
    """

    lines = [
        (
            f"<b>"
            f"{escape(trust_result.recommendation_title)}"
            f"</b>"
        ),
        "",
    ]

    if trust_result.votes_count == 0:
        lines.extend(
            [
                "⭐ <b>Рейтинг:</b> пока нет оценок",
                "👥 <b>Оценок:</b> 0",
            ]
        )
    else:
        lines.extend(
            [
                (
                    "⭐ <b>Рейтинг пользователей:</b> "
                    f"{trust_result.average_rating:.1f} "
                    "из 10"
                ),
                (
                    "👥 <b>Количество оценок:</b> "
                    f"{trust_result.votes_count}"
                ),
            ]
        )

    lines.extend(
        [
            (
                "🛡 <b>Уровень доверия:</b> "
                f"{escape(trust_result.trust_title)}"
            ),
            (
                "📊 <b>Доверие к данным:</b> "
                f"{trust_result.trust_score:.0f} из 100"
            ),
            "",
            "<b>Почему такой вывод:</b>",
            format_explanation(
                trust_result
            ),
        ]
    )

    return "\n".join(
        lines
    )


def format_price_text(
    price_stats: dict[str, Any] | None,
) -> str:
    """
    Форматирует статистику цен.
    """

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
            "💰 <b>Ориентир по цене:</b> "
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
            "⚠️ <b>Очень большая разница "
            "в цене.</b>\n"
            "Перед покупкой обязательно "
            "сравните магазины."
        )

    elif spread_percent >= 40:
        lines.append(
            "⚠️ <b>Заметный разброс цен.</b>\n"
            "Стоимость лучше проверить "
            "перед покупкой."
        )

    return "\n".join(
        lines
    )


def build_product_card(
    *,
    product,
    brand,
    category,
    trust_result: TrustEngineResult,
    price_stats: dict[str, Any] | None,
) -> str:
    """
    Формирует карточку товара.

    Порядок принципиально важен:

    1. решение MarkaRadar;
    2. объяснение и доверие;
    3. название товара;
    4. цена;
    5. технические характеристики;
    6. возможность поставить оценку.
    """

    title = build_product_title(
        product=product,
        brand=brand,
    )

    package_text = format_package(
        product.package_value,
        product.package_unit,
    )

    subtype_text = format_subtype(
        product.subtype
    )

    product_lines = [
        format_trust_engine_text(
            trust_result
        ),
        "",
        "━━━━━━━━━━━━━━━━━━",
        "",
        title,
        "",
        format_price_text(
            price_stats
        ),
        "",
        "<b>Информация о товаре:</b>",
        (
            "📂 <b>Категория:</b> "
            f"{escape(category.name)}"
        ),
    ]

    if is_real_brand(
        brand.name
    ):
        product_lines.append(
            "🏷 <b>Бренд:</b> "
            f"{escape(brand.name)}"
        )
    else:
        product_lines.append(
            "🏷 <b>Бренд:</b> "
            "информация отсутствует"
        )

    product_lines.extend(
        [
            (
                "🥫 <b>Тип:</b> "
                f"{subtype_text}"
            ),
            (
                "📦 <b>Упаковка:</b> "
                f"{package_text}"
            ),
        ]
    )

    if product.barcode:
        product_lines.append(
            "🔢 <b>Штрихкод:</b> "
            f"<code>"
            f"{escape(product.barcode)}"
            f"</code>"
        )

    product_lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━",
            "",
            (
                "Ваша оценка поможет другим "
                "покупателям сделать выбор 👇"
            ),
        ]
    )

    return "\n".join(
        product_lines
    )


async def send_search_loader(
    message: Message,
) -> Message | None:
    """
    Сразу показывает пользователю,
    что поиск начался.

    Если GIF отсутствует, отправляет
    обычное текстовое сообщение.
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
            "🔎 <b>Ищу подходящие товары…</b>\n"
            "Проверяю оценки, доверие "
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
                "Сравниваю товары, оценки "
                "и надёжность результатов."
            ),
        )

    except Exception:
        logger.exception(
            "Не удалось отправить GIF поиска"
        )

        return await message.answer(
            "🔎 <b>Ищу подходящие товары…</b>\n"
            "Проверяю оценки, доверие "
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
    trust_result: TrustEngineResult,
    price_stats: dict[str, Any] | None,
) -> None:
    """
    Отправляет карточку товара.
    """

    card_text = build_product_card(
        product=product,
        brand=brand,
        category=category,
        trust_result=trust_result,
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
    Загружает рейтинг и цены, запускает
    Trust Engine и отправляет карточку.
    """

    rating = await get_full_product_rating(
        session=session,
        product_id=product.id,
    )

    price_stats = await get_price_statistics(
        session=session,
        product_id=product.id,
    )

    average_rating = float(
        rating.get(
            "average_rating",
            0.0,
        )
    )

    votes_count = int(
        rating.get(
            "votes_count",
            0,
        )
    )

    data_quality_score = (
        calculate_data_quality_score(
            product=product,
            brand=brand,
            category=category,
            price_stats=price_stats,
        )
    )

    trust_result = evaluate_product(
        average_rating=average_rating,
        votes_count=votes_count,
        data_quality_score=(
            data_quality_score
        ),
        popularity_score=0.0,
        relevance_score=100.0,
    )

    await send_product_card(
        message=message,
        product=product,
        brand=brand,
        category=category,
        trust_result=trust_result,
        price_stats=price_stats,
    )


async def show_product_families(
    *,
    message: Message,
    families: list[dict],
    query: str,
) -> None:
    """
    Показывает найденные виды товаров.
    """

    total_products = sum(
        int(
            family.get(
                "products_count",
                0,
            )
        )
        for family in families
    )

    await message.answer(
        "🧭 <b>Уточните, что именно нужно</b>\n\n"
        f"Запрос: «{escape(query)}»\n"
        f"Подходящих направлений: "
        f"<b>{len(families)}</b>\n"
        f"Товаров внутри: "
        f"<b>{total_products}</b>\n\n"
        "После выбора MarkaRadar покажет "
        "товары с учётом оценок и доверия:",
        reply_markup=(
            get_product_families_keyboard(
                families
            )
        ),
    )


async def show_fallback_products(
    *,
    message: Message,
    session,
    query: str,
) -> None:
    """
    Выполняет резервный расширенный поиск.
    """

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

    if len(products) == 1:
        product, brand, category = (
            products[0]
        )

        await show_single_product(
            message=message,
            session=session,
            product=product,
            brand=brand,
            category=category,
        )
        return

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
        "🔍 <b>Подходящие варианты</b>\n\n"
        f"Запрос: «{escape(query)}»\n"
        "Выберите товар — MarkaRadar покажет "
        "его оценку и надёжность:",
        reply_markup=(
            get_search_suggestions_keyboard(
                fallback_suggestions
            )
        ),
    )


@router.message(F.text)
async def search_handler(
    message: Message,
) -> None:
    """
    Главный обработчик поиска MarkaRadar.

    Порядок:

    1. Поиск по штрихкоду.
    2. Уточняющие группы.
    3. Семейства товаров.
    4. Конкретные товары.
    5. Резервный расширенный поиск.
    """

    if message.text is None:
        return

    query = message.text.strip()

    if not query:
        await message.answer(
            "Введите название продукта "
            "или бренда."
        )
        return

    if query.startswith("/"):
        return

    loading_message = await send_search_loader(
        message
    )

    try:
        async with async_session_maker() as session:
            user = message.from_user

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

            engine_result = await run_search_engine(
                session=session,
                query=query,
                intent_limit=8,
                suggestion_limit=8,
            )

            if (
                engine_result.mode
                == SearchMode.INTENTS
            ):
                if user is None:
                    await show_fallback_products(
                        message=message,
                        session=session,
                        query=query,
                    )
                    return

                groups_as_dicts = [
                    {
                        "title": group["title"],
                        "query": group["query"],
                        "count": group["count"],
                    }
                    for group
                    in engine_result.intent_groups
                ]

                save_intent_groups(
                    chat_id=message.chat.id,
                    user_id=user.id,
                    groups=groups_as_dicts,
                )

                await message.answer(
                    "🧭 <b>Что именно "
                    "вы ищете?</b>\n\n"
                    f"Запрос: «{escape(query)}»\n"
                    "Выберите подходящий вариант:",
                    reply_markup=(
                        get_intent_groups_keyboard(
                            groups_as_dicts
                        )
                    ),
                )
                return

            families = await find_product_families(
                session=session,
                query=query,
                limit=10,
            )

            if families:
                if user is not None:
                    clear_intent_groups(
                        chat_id=message.chat.id,
                        user_id=user.id,
                    )

                await show_product_families(
                    message=message,
                    families=families,
                    query=query,
                )
                return

            if (
                engine_result.mode
                == SearchMode.PRODUCTS
            ):
                if user is not None:
                    clear_intent_groups(
                        chat_id=message.chat.id,
                        user_id=user.id,
                    )

                await message.answer(
                    "🔍 <b>Лучшие совпадения</b>\n\n"
                    f"Запрос: «{escape(query)}»\n"
                    "Выберите товар — MarkaRadar "
                    "покажет оценку и уровень доверия:",
                    reply_markup=(
                        get_search_suggestions_keyboard(
                            engine_result
                            .product_suggestions
                        )
                    ),
                )
                return

            if user is not None:
                clear_intent_groups(
                    chat_id=message.chat.id,
                    user_id=user.id,
                )

            await show_fallback_products(
                message=message,
                session=session,
                query=query,
            )

    except Exception:
        logger.exception(
            "Ошибка поиска по запросу: %s",
            query,
        )

        await message.answer(
            "⚠️ Во время поиска произошла ошибка.\n"
            "Попробуйте повторить запрос "
            "немного позже."
        )

    finally:
        await remove_search_loader(
            loading_message
    )
