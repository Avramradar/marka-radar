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

from app.database.session import async_session_maker
from app.keyboards.decision_search import (
    get_decision_search_keyboard,
)
from app.keyboards.product_family import (
    get_product_families_keyboard,
)
from app.keyboards.rating import get_rating_keyboard
from app.keyboards.search import (
    get_intent_groups_keyboard,
    get_search_suggestions_keyboard,
)
from app.search.decision_search import (
    DecisionProduct,
    DecisionSearchResult,
)
from app.search.intent_state import (
    clear_intent_groups,
    save_intent_groups,
)
from app.search.search_pipeline import (
    SearchPipelineResult,
    SearchPipelineScreen,
    is_possible_barcode,
    run_search_pipeline,
)
from app.services.external_product_enrichment_service import (
    enrich_product_by_barcode,
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


GENERIC_PRODUCT_NAMES = {
    "кофе",
    "молоко",
    "пицца",
    "чай",
    "вода",
    "сыр",
    "масло",
    "йогурт",
    "кефир",
    "сельдь",
    "сок",
}


def normalize_simple_text( value: Any, ) -> str:
    """ Простая нормализация текста для внутренних проверок. """

    return " ".join(
        str(
            value or ""
        )
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def format_number( value: Decimal | float | int | None, ) -> str:
    """ Убирает лишние нули у чисел. Примеры: 245.000 -> 245 0.450 -> 0.45 1.500 -> 1.5 """

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


def format_package( package_value: Decimal | float | int | None, package_unit: str | None, ) -> str:
    """ Форматирует вес или объём упаковки. """

    if (
        package_value is None
        or not package_unit
    ):
        return "не указана"

    return (
        f"{format_number(package_value)} "
        f"{escape(str(package_unit))}"
    )


def format_subtype( subtype: str | None, ) -> str:
    """ Форматирует подтип товара. """

    if not subtype:
        return "не указан"

    cleaned_subtype = subtype.strip()

    if not cleaned_subtype:
        return "не указан"

    formatted_subtype = (
        cleaned_subtype[0].upper()
        + cleaned_subtype[1:]
    )

    return escape(
        formatted_subtype
    )


def is_real_brand( brand_name: str | None, ) -> bool:
    """ Проверяет, указан ли настоящий бренд. """

    normalized_brand = normalize_simple_text(
        brand_name
    )

    return (
        normalized_brand
        not in UNKNOWN_BRAND_NAMES
    )


def is_generic_product_name( product_name: str | None, ) -> bool:
    """ Проверяет слишком общее название. """

    return (
        normalize_simple_text(
            product_name
        )
        in GENERIC_PRODUCT_NAMES
    )


def should_enrich_barcode_product( pipeline_result: SearchPipelineResult, ) -> bool:
    """ Решает, нужно ли внешнее обогащение уже найденного по штрихкоду товара. """

    if (
        pipeline_result.screen
        != SearchPipelineScreen.BARCODE_PRODUCT
    ):
        return False

    item = pipeline_result.barcode_product

    if item is None:
        return False

    product = item.product
    brand = item.brand

    missing_brand = not is_real_brand(
        getattr(
            brand,
            "name",
            None,
        )
    )

    generic_name = is_generic_product_name(
        getattr(
            product,
            "name",
            None,
        )
    )

    missing_image = not bool(
        getattr(
            product,
            "image_url",
            None,
        )
    )

    missing_package = (
        getattr(
            product,
            "package_value",
            None,
        )
        is None
        or not getattr(
            product,
            "package_unit",
            None,
        )
    )

    missing_description = not bool(
        getattr(
            product,
            "description",
            None,
        )
    )

    missing_subtype = not bool(
        getattr(
            product,
            "subtype",
            None,
        )
    )

    return any(
        (
            missing_brand,
            generic_name,
            missing_image,
            missing_package,
            missing_description,
            missing_subtype,
        )
    )


def build_product_title( *, product, brand, ) -> str:
    """ Формирует заголовок карточки. """

    product_name = escape(
        str(product.name)
    )

    if not is_real_brand(
        brand.name
    ):
        return f"<b>{product_name}</b>"

    return (
        f"<b>{escape(str(brand.name))} — "
        f"{product_name}</b>"
    )


def calculate_data_quality_score( *, product, brand, category, price_stats: dict[str, Any] | None, ) -> float:
    """ Оценивает полноту карточки товара. """

    score = 0.0

    product_name = str(
        product.name or ""
    ).strip()

    if product_name:
        score += 20.0

    if len(product_name) >= 4:
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


def format_explanation( trust_result: TrustEngineResult, ) -> str:
    """ Форматирует объяснение Trust Engine. """

    if not trust_result.explanation:
        return (
            "• Пока недостаточно информации "
            "для подробного объяснения."
        )

    return "\n".join(
        f"• {escape(reason)}"
        for reason
        in trust_result.explanation
    )


def format_trust_engine_text( trust_result: TrustEngineResult, ) -> str:
    """ Формирует главный блок решения MarkaRadar. """

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
                f"{trust_result.trust_score:.0f} "
                "из 100"
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


def format_price_text( price_stats: dict[str, Any] | None, ) -> str:
    """ Форматирует статистику цен. """

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


def build_product_card( *, product, brand, category, trust_result: TrustEngineResult, price_stats: dict[str, Any] | None, ) -> str:
    """ Формирует карточку товара. """

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

    category_name = escape(
        str(category.name)
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
            f"{category_name}"
        ),
    ]

    if is_real_brand(
        brand.name
    ):
        product_lines.append(
            "🏷 <b>Бренд:</b> "
            f"{escape(str(brand.name))}"
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
            f"{escape(str(product.barcode))}"
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


async def send_search_loader( message: Message, ) -> Message | None:
    """ Показывает пользователю, что поиск начался. """

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
            "Сравниваю совпадения, оценки "
            "и надёжность результатов."
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
            "Сравниваю совпадения, оценки "
            "и надёжность результатов."
        )


async def remove_search_loader( loading_message: Message | None, ) -> None:
    """ Удаляет сообщение загрузки. """

    if loading_message is None:
        return

    with suppress(Exception):
        await loading_message.delete()


async def send_product_card( *, message: Message, product, brand, category, trust_result: TrustEngineResult, price_stats: dict[str, Any] | None, ) -> None:
    """ Отправляет карточку товара. """

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


async def show_single_product( *, message: Message, session, product, brand, category, ) -> None:
    """ Загружает рейтинг и цены, запускает Trust Engine и показывает карточку. """

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
        data_quality_score=data_quality_score,
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


def format_decision_product( item: DecisionProduct, ) -> str:
    """ Форматирует товар для первого экрана решения. """

    if item.brand_name:
        title = (
            f"{escape(item.brand_name)} — "
            f"{escape(item.name)}"
        )
    else:
        title = escape(
            item.name
        )

    if item.votes_count > 0:
        rating_line = (
            f"⭐ {item.average_rating:.1f} из 10"
            f" · 👥 {item.votes_count}"
        )
    else:
        rating_line = "⭐ Оценок пока нет"

    return (
        f"<b>{title}</b>\n"
        f"{rating_line}\n"
        f"{escape(item.trust_result.trust_title)}"
    )


def build_decision_screen_text( *, result: DecisionSearchResult, query: str, explanation: str | None, ) -> str:
    """ Формирует первый экран помощника выбора. """

    lines = [
        "🎯 <b>Помощник выбора MarkaRadar</b>",
        "",
        f"Запрос: «{escape(query)}»",
        "",
    ]

    if result.best_choice is not None:
        lines.extend(
            [
                "🏆 <b>Лучший подтверждённый выбор</b>",
                "",
                format_decision_product(
                    result.best_choice
                ),
                "",
            ]
        )

        reasons = (
            result.best_choice
            .trust_result
            .explanation
        )

        if reasons:
            lines.extend(
                [
                    (
                        "Почему рекомендуем: "
                        f"{escape(reasons[0])}"
                    ),
                    "",
                ]
            )
    else:
        lines.extend(
            [
                "⚪ <b>Уверенного лидера пока нет</b>",
                "",
                (
                    "Подходящие товары найдены, "
                    "но данных пока недостаточно "
                    "для надёжной рекомендации."
                ),
                "",
            ]
        )

    if result.alternatives:
        lines.extend(
            [
                "👍 <b>Подходящие варианты</b>",
                (
                    "Ниже — товары, которые лучше "
                    "всего соответствуют запросу."
                ),
                "",
            ]
        )

    if result.insufficient_data:
        lines.extend(
            [
                (
                    "⚪ <b>Товары с небольшим "
                    "количеством оценок</b>"
                ),
                (
                    "Они подходят по запросу, "
                    "но их рейтинг пока нельзя "
                    "считать устойчивым."
                ),
                "",
            ]
        )

    if explanation:
        lines.extend(
            [
                f"ℹ️ {escape(explanation)}",
                "",
            ]
        )

    lines.append(
        "Нажмите на товар, чтобы увидеть "
        "подробную оценку и объяснение."
    )

    return "\n".join(
        lines
    )


def build_decision_keyboard_result( result: DecisionSearchResult, ) -> DecisionSearchResult:
    """ Подготавливает Decision Search для клавиатуры. """

    alternatives = list(
        result.alternatives
    )

    insufficient_data = list(
        result.insufficient_data
    )

    if (
        result.best_choice is None
        and not alternatives
        and result.other_products
    ):
        alternatives = list(
            result.other_products[:3]
        )

    return DecisionSearchResult(
        query=result.query,
        total_candidates=result.total_candidates,
        best_choice=result.best_choice,
        alternatives=alternatives,
        insufficient_data=insufficient_data,
        other_products=[],
    )


async def show_decision_screen( *, message: Message, pipeline_result: SearchPipelineResult, ) -> None:
    """ Показывает экран решения MarkaRadar. """

    decision = pipeline_result.decision

    if (
        decision is None
        or not decision.has_results
    ):
        await show_not_found_screen(
            message=message,
            query=pipeline_result.normalized_query,
        )
        return

    keyboard_result = (
        build_decision_keyboard_result(
            decision
        )
    )

    has_buttons = bool(
        keyboard_result.best_choice
        or keyboard_result.alternatives
        or keyboard_result.insufficient_data
    )

    if has_buttons:
        keyboard = (
            get_decision_search_keyboard(
                keyboard_result
            )
        )
    else:
        fallback_products = [
            {
                "product_id": item.product_id,
                "name": item.name,
                "brand": item.brand_name,
                "score": item.recommendation_score,
            }
            for item
            in decision.other_products[:8]
        ]

        keyboard = (
            get_search_suggestions_keyboard(
                fallback_products
            )
        )

    text = build_decision_screen_text(
        result=keyboard_result,
        query=pipeline_result.normalized_query,
        explanation=pipeline_result.explanation,
    )

    await message.answer(
        text,
        reply_markup=keyboard,
    )


async def show_intents_screen( *, message: Message, pipeline_result: SearchPipelineResult, ) -> None:
    """ Показывает уточнения. """

    user = message.from_user
    groups = pipeline_result.intent_groups

    if not groups:
        await show_decision_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

    if user is None:
        await show_decision_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

    save_intent_groups(
        chat_id=message.chat.id,
        user_id=user.id,
        groups=groups,
    )

    await message.answer(
        "🧭 <b>Что именно вы ищете?</b>\n\n"
        f"Запрос: «"
        f"{escape(pipeline_result.normalized_query)}"
        f"»\n\n"
        "Выберите подходящий вариант. "
        "Дальше MarkaRadar сравнит товары "
        "по оценкам и уровню доверия:",
        reply_markup=(
            get_intent_groups_keyboard(
                groups
            )
        ),
    )


async def show_families_screen( *, message: Message, pipeline_result: SearchPipelineResult, ) -> None:
    """ Показывает виды продукта. """

    families = pipeline_result.families

    if not families:
        await show_decision_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

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
        "🧭 <b>Уточните вид продукта</b>\n\n"
        f"Запрос: «"
        f"{escape(pipeline_result.normalized_query)}"
        f"»\n"
        f"Направлений: "
        f"<b>{len(families)}</b>\n"
        f"Товаров внутри: "
        f"<b>{total_products}</b>\n\n"
        "После выбора MarkaRadar сравнит "
        "подходящие товары по оценкам:",
        reply_markup=(
            get_product_families_keyboard(
                families
            )
        ),
    )


async def show_not_found_screen( *, message: Message, query: str, ) -> None:
    """ Показывает экран отсутствия результатов. """

    await message.answer(
        "🔍 <b>Подходящих товаров не найдено</b>\n\n"
        f"Запрос: «{escape(query)}»\n\n"
        "Попробуйте:\n"
        "• написать название короче;\n"
        "• убрать лишние характеристики;\n"
        "• указать бренд;\n"
        "• проверить написание;\n"
        "• отправить штрихкод."
    )


async def run_pipeline_with_external_enrichment( *, session, query: str, ) -> SearchPipelineResult:
    """ Выполняет Search Pipeline и при необходимости подключает единый сервис внешнего обогащения. Здесь намеренно нет прямого вызова OpenFoodFacts. Конкретными внешними источниками управляет external_product_enrichment_service. """

    pipeline_result = await run_search_pipeline(
        session=session,
        query=query,
        intent_limit=6,
        family_limit=6,
        decision_candidates_limit=20,
    )

    cleaned_query = " ".join(
        query.strip().split()
    )

    if not is_possible_barcode(
        cleaned_query
    ):
        return pipeline_result

    should_try_external = False

    if (
        pipeline_result.screen
        == SearchPipelineScreen.NOT_FOUND
    ):
        should_try_external = True
    elif should_enrich_barcode_product(
        pipeline_result
    ):
        should_try_external = True

    if not should_try_external:
        return pipeline_result

    logger.info(
        "Пробуем внешнее обогащение "
        "для штрихкода %s",
        cleaned_query,
    )

    barcode_item = (
        pipeline_result.barcode_product
    )

    enrichment_result = (
        await enrich_product_by_barcode(
            session=session,
            barcode=cleaned_query,
            product=(
                barcode_item.product
                if barcode_item is not None
                else None
            ),
            brand=(
                barcode_item.brand
                if barcode_item is not None
                else None
            ),
            category=(
                barcode_item.category
                if barcode_item is not None
                else None
            ),
        )
    )

    if not enrichment_result.enriched:
        logger.info(
            "Внешние источники не дали "
            "полезного обогащения для %s",
            cleaned_query,
        )

        # Провайдер мог выполнить flush/изменения,
        # которые сервис не признал полезными.
        # Не оставляем незавершённую транзакцию.
        await session.rollback()

        return pipeline_result

    await session.commit()

    logger.info(
        "Внешнее обогащение выполнено: "
        "provider=%s",
        enrichment_result.provider,
    )

    # Новый запуск Pipeline нужен, чтобы получить
    # свежие Product/Brand/Category после commit.
    return await run_search_pipeline(
        session=session,
        query=cleaned_query,
        intent_limit=6,
        family_limit=6,
        decision_candidates_limit=20,
    )


async def process_pipeline_result( *, message: Message, session, pipeline_result: SearchPipelineResult, ) -> None:
    """ Показывает экран, выбранный Search Pipeline. """

    user = message.from_user

    if (
        pipeline_result.screen
        != SearchPipelineScreen.INTENTS
        and user is not None
    ):
        clear_intent_groups(
            chat_id=message.chat.id,
            user_id=user.id,
        )

    if (
        pipeline_result.screen
        == SearchPipelineScreen.BARCODE_PRODUCT
    ):
        barcode_product = (
            pipeline_result.barcode_product
        )

        if barcode_product is None:
            await show_not_found_screen(
                message=message,
                query=(
                    pipeline_result.normalized_query
                ),
            )
            return

        await show_single_product(
            message=message,
            session=session,
            product=barcode_product.product,
            brand=barcode_product.brand,
            category=barcode_product.category,
        )
        return

    if (
        pipeline_result.screen
        == SearchPipelineScreen.INTENTS
    ):
        await show_intents_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

    if (
        pipeline_result.screen
        == SearchPipelineScreen.FAMILIES
    ):
        await show_families_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

    if (
        pipeline_result.screen
        == SearchPipelineScreen.DECISION
    ):
        await show_decision_screen(
            message=message,
            pipeline_result=pipeline_result,
        )
        return

    await show_not_found_screen(
        message=message,
        query=pipeline_result.normalized_query,
    )


@router.message(F.text)
async def search_handler( message: Message, ) -> None:
    """ Главный обработчик поиска MarkaRadar. """

    if message.text is None:
        return

    query = message.text.strip()

    if not query:
        await message.answer(
            "Введите название продукта, "
            "бренда или штрихкод."
        )
        return

    if query.startswith("/"):
        return

    loading_message = (
        await send_search_loader(
            message
        )
    )

    try:
        async with async_session_maker() as session:
            pipeline_result = (
                await run_pipeline_with_external_enrichment(
                    session=session,
                    query=query,
                )
            )

            logger.info(
                "Search Pipeline: "
                "query=%r, screen=%s, "
                "intents=%s, families=%s, "
                "candidates=%s",
                query,
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

            await process_pipeline_result(
                message=message,
                session=session,
                pipeline_result=pipeline_result,
            )

    except Exception:
        logger.exception(
            "Ошибка Search Pipeline "
            "по запросу: %s",
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
