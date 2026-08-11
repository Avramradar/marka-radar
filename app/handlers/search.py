import logging
import re
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
from app.services.external_catalog_service import (
    enrich_catalog,
)
from app.services.external_product_enrichment_service import (
    enrich_product_by_barcode,
)
from app.services.product_card_enrichment_service import (
    ensure_product_card_enriched,
    load_product_card,
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
    "сметана",
    "майонез",
    "колбаса",
    "творог",
    "сливки",
    "паштет",
    "козинаки",
    "шоколад",
    "печенье",
    "сахар",
    "мука",
    "рис",
    "гречка",
    "макароны",
    "хлеб",
    "мороженое",
    "пельмени",
    "вареники",
}


GENERIC_QUERY_WORDS = (
    GENERIC_PRODUCT_NAMES
    | {
        "продукт",
        "продукты",
        "товар",
        "товары",
        "гост",
        "бзмж",
    }
)


UNIT_ALIASES = {
    "гр": "г",
    "грамм": "г",
    "грамма": "г",
    "граммов": "г",
    "g": "г",
    "gram": "г",
    "grams": "г",
    "кг": "кг",
    "kg": "кг",
    "мл": "мл",
    "ml": "мл",
    "л": "л",
    "l": "л",
}


def normalize_simple_text( value: Any, ) -> str:
    """Простая нормализация текста для внутренних проверок."""

    return " ".join(
        str(value or "")
        .strip()
        .lower()
        .replace("ё", "е")
        .split()
    )


def normalize_match_token( token: str, ) -> str:
    """Нормализует отдельный токен поискового совпадения."""

    normalized_token = (
        normalize_simple_text(token)
        .strip()
    )

    return UNIT_ALIASES.get(
        normalized_token,
        normalized_token,
    )


def extract_match_tokens( value: Any, ) -> list[str]:
    """ Извлекает слова и числа отдельно. Поэтому: 500гр -> 500 + г 20% -> 20 0.5кг -> 0.5 + кг """

    text = normalize_simple_text(value)

    raw_tokens = re.findall(
        r"\d+(?:[.,]\d+)?|[a-zа-я]+",
        text,
        flags=re.IGNORECASE,
    )

    result: list[str] = []

    for raw_token in raw_tokens:
        token = normalize_match_token(
            raw_token.replace(",", ".")
        )

        if not token:
            continue

        result.append(token)

    return result


def query_required_tokens( query: str, ) -> list[str]:
    """ Возвращает обязательные уточняющие токены. Общий тип продукта не считается достаточным доказательством локального совпадения. Пример: "Майонез Славолия 500гр" -> ["славолия", "500", "г"] """

    tokens = extract_match_tokens(
        query
    )

    required: list[str] = []

    for token in tokens:
        if (
            token.isalpha()
            and token in GENERIC_QUERY_WORDS
        ):
            continue

        if token not in required:
            required.append(
                token
            )

    return required


def token_matches_candidate( required_token: str, candidate_tokens: set[str], ) -> bool:
    """Осторожное совпадение токена с локальной карточкой."""

    if required_token in candidate_tokens:
        return True

    if (
        required_token.replace(".", "", 1).isdigit()
        or len(required_token) < 4
    ):
        return False

    return any(
        (
            required_token in candidate_token
            or candidate_token in required_token
        )
        for candidate_token in candidate_tokens
        if len(candidate_token) >= 4
    )


def format_number( value: Decimal | float | int | None, ) -> str:
    """Убирает лишние нули у чисел."""

    if value is None:
        return ""

    decimal_value = Decimal(
        str(value)
    )

    if decimal_value == decimal_value.to_integral():
        return str(
            int(decimal_value)
        )

    return format(
        decimal_value.normalize(),
        "f",
    )


def format_package( package_value: Decimal | float | int | None, package_unit: str | None, ) -> str:
    """Форматирует вес или объём упаковки."""

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
    """Форматирует подтип товара."""

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
    """Проверяет, указан ли настоящий бренд."""

    normalized_brand = normalize_simple_text(
        brand_name
    )

    return (
        normalized_brand
        not in UNKNOWN_BRAND_NAMES
    )


def is_generic_product_name( product_name: str | None, ) -> bool:
    """Проверяет слишком общее название."""

    return (
        normalize_simple_text(
            product_name
        )
        in GENERIC_PRODUCT_NAMES
    )


def should_enrich_barcode_product( pipeline_result: SearchPipelineResult, ) -> bool:
    """Решает, нужно ли внешнее обогащение найденного по штрихкоду товара."""

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


def product_card_is_complete_enough( *, product, brand, ) -> bool:
    """Проверяет общую полноту локальной карточки."""

    checks = (
        bool(
            getattr(
                product,
                "name",
                None,
            )
        ),
        is_real_brand(
            getattr(
                brand,
                "name",
                None,
            )
        ),
        bool(
            getattr(
                product,
                "image_url",
                None,
            )
        ),
        (
            getattr(
                product,
                "package_value",
                None,
            )
            is not None
            and bool(
                getattr(
                    product,
                    "package_unit",
                    None,
                )
            )
        ),
        bool(
            getattr(
                product,
                "description",
                None,
            )
        ),
    )

    return (
        sum(
            1
            for value in checks
            if value
        )
        >= 4
    )


def decision_items( decision: DecisionSearchResult, ) -> list[DecisionProduct]:
    """Собирает уникальный набор локальных кандидатов для проверки."""

    items: list[DecisionProduct] = []

    if decision.best_choice is not None:
        items.append(
            decision.best_choice
        )

    items.extend(
        decision.alternatives
    )
    items.extend(
        decision.insufficient_data
    )
    items.extend(
        decision.other_products[:8]
    )

    unique: list[DecisionProduct] = []
    seen_ids: set[int] = set()

    for item in items:
        product_id = int(
            item.product_id
        )

        if product_id in seen_ids:
            continue

        seen_ids.add(
            product_id
        )
        unique.append(
            item
        )

    return unique


def decision_has_complete_matching_card( *, query: str, decision: DecisionSearchResult | None, ) -> bool:
    """ Проверяет, есть ли локальная карточка именно запрошенного товара. Ключевое отличие от старой логики: общая похожесть больше не блокирует внешний каталог. Все специфические токены запроса должны присутствовать в ОДНОМ локальном кандидате. Поэтому наличие хорошего "Майонеза МЖК" не блокирует поиск "Майонез Славолия 500гр". """

    if (
        decision is None
        or not decision.has_results
    ):
        return False

    required_tokens = query_required_tokens(
        query
    )

    # Одно широкое слово вроде "кофе" не должно
    # запускать внешний импорт десятков товаров.
    if not required_tokens:
        return True

    for item in decision_items(
        decision
    ):
        product = item.product
        brand = item.brand

        candidate_text = " ".join(
            [
                str(
                    getattr(
                        brand,
                        "name",
                        "",
                    )
                    or ""
                ),
                str(
                    getattr(
                        product,
                        "name",
                        "",
                    )
                    or ""
                ),
                str(
                    getattr(
                        product,
                        "subtype",
                        "",
                    )
                    or ""
                ),
                str(
                    getattr(
                        product,
                        "package_value",
                        "",
                    )
                    or ""
                ),
                str(
                    getattr(
                        product,
                        "package_unit",
                        "",
                    )
                    or ""
                ),
            ]
        )

        candidate_tokens = set(
            extract_match_tokens(
                candidate_text
            )
        )

        missing_tokens = [
            token
            for token in required_tokens
            if not token_matches_candidate(
                token,
                candidate_tokens,
            )
        ]

        logger.info(
            "External gate candidate: "
            "query=%r product_id=%s "
            "required=%s candidate=%r "
            "missing=%s complete=%s",
            query,
            getattr(
                product,
                "id",
                None,
            ),
            required_tokens,
            candidate_text,
            missing_tokens,
            product_card_is_complete_enough(
                product=product,
                brand=brand,
            ),
        )

        if missing_tokens:
            continue

        if not product_card_is_complete_enough(
            product=product,
            brand=brand,
        ):
            continue

        logger.info(
            "External gate: exact local card accepted: "
            "query=%r product_id=%s required=%s",
            query,
            getattr(
                product,
                "id",
                None,
            ),
            required_tokens,
        )

        return True

    logger.info(
        "External gate: no exact complete local card: "
        "query=%r required=%s",
        query,
        required_tokens,
    )

    return False


def should_try_external_text_catalog( *, query: str, pipeline_result: SearchPipelineResult, ) -> bool:
    """ Решает, нужен ли внешний каталог для текстового запроса. Правила: - штрихкоды идут отдельной цепочкой; - одно широкое слово не запускает импорт; - конкретный запрос 2+ слов запускает внешний каталог, если нет именно такого полного товара. """

    cleaned = " ".join(
        str(query or "")
        .strip()
        .split()
    )

    if not cleaned:
        return False

    if is_possible_barcode(
        cleaned
    ):
        return False

    if len(
        cleaned.split()
    ) < 2:
        return False

    has_matching_card = (
        decision_has_complete_matching_card(
            query=cleaned,
            decision=pipeline_result.decision,
        )
    )

    should_try = not has_matching_card

    logger.info(
        "External text gate: "
        "query=%r should_try=%s screen=%s",
        cleaned,
        should_try,
        pipeline_result.screen,
    )

    return should_try


def build_product_title( *, product, brand, ) -> str:
    """Формирует заголовок карточки."""

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
    """Оценивает полноту карточки товара."""

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
    """Форматирует объяснение Trust Engine."""

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
    """Формирует главный блок решения MarkaRadar."""

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
    """Форматирует статистику цен."""

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
    """Формирует карточку товара."""

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
    """Показывает пользователю, что поиск начался."""

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
    """Удаляет сообщение загрузки."""

    if loading_message is None:
        return

    with suppress(Exception):
        await loading_message.delete()


async def send_product_card( *, message: Message, product, brand, category, trust_result: TrustEngineResult, price_stats: dict[str, Any] | None, ) -> None:
    """ Отправляет карточку товара. Сломанная внешняя картинка не должна ломать карточку целиком. """

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

    raw_image = getattr(
        product,
        "image_url",
        None,
    )

    image_value = (
        str(raw_image).strip()
        if raw_image
        else ""
    )

    if image_value:
        try:
            await message.answer_photo(
                photo=image_value,
                caption=card_text,
                reply_markup=keyboard,
            )

            logger.info(
                "Product card sent with image: "
                "product_id=%s image=%r",
                product.id,
                image_value[:200],
            )

            return

        except TelegramBadRequest as error:
            logger.warning(
                "Product image rejected by Telegram: "
                "product_id=%s image=%r error=%s",
                product.id,
                image_value[:300],
                error,
            )

        except Exception as error:
            logger.warning(
                "Product image send failed: "
                "product_id=%s image=%r "
                "error_type=%s error=%s",
                product.id,
                image_value[:300],
                type(error).__name__,
                error,
            )

    await message.answer(
        card_text,
        reply_markup=keyboard,
    )

    logger.info(
        "Product card sent without image: "
        "product_id=%s had_image=%s",
        product.id,
        bool(image_value),
    )


async def show_single_product( *, message: Message, session, product, brand, category, ) -> None:
    """ Перед показом доводит конкретную карточку через доступные внешние источники до максимально полного состояния, затем загружает рейтинг/цены и показывает её. """

    product_id = int(
        product.id
    )

    with suppress(Exception):
        await message.bot.send_chat_action(
            chat_id=message.chat.id,
            action=ChatAction.TYPING,
        )

    try:
        card_state = (
            await ensure_product_card_enriched(
                session=session,
                product_id=product_id,
                limit_per_provider=8,
            )
        )

        product = card_state.product
        brand = card_state.brand
        category = card_state.category

        logger.info(
            "Product card ready for display: "
            "product_id=%s score=%.1f complete=%s "
            "missing=%s critical=%s",
            product_id,
            card_state.completeness.score,
            card_state.completeness.is_complete,
            card_state.completeness.missing_fields,
            card_state.completeness.critical_missing_fields,
        )

    except Exception:
        # Внешний источник не должен ломать открытие товара.
        # Если все попытки обогащения дали ошибку, пользователь
        # всё равно получает последнюю доступную локальную карточку.
        logger.exception(
            "Product card enrichment failed before display: "
            "product_id=%s",
            product_id,
        )

        with suppress(Exception):
            await session.rollback()

        # После rollback ORM-объекты могли быть expired.
        # Загружаем последнюю сохранённую карточку заново,
        # чтобы fallback-показ тоже был безопасным.
        try:
            (
                product,
                brand,
                category,
            ) = await load_product_card(
                session=session,
                product_id=product_id,
            )
        except Exception:
            logger.exception(
                "Failed to reload product card after "
                "enrichment error: product_id=%s",
                product_id,
            )

    rating = await get_full_product_rating(
        session=session,
        product_id=product_id,
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
    """Форматирует товар для первого экрана решения."""

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
    """Формирует первый экран помощника выбора."""

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
    """Подготавливает Decision Search для клавиатуры."""

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
    """Показывает экран решения MarkaRadar."""

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
    """Показывает уточнения."""

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
    """Показывает виды продукта."""

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
    """Показывает экран отсутствия результатов."""

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
    """ Главная поисковая цепочка MarkaRadar. Текст: Local Search Pipeline -> ExternalCatalogService при необходимости -> Product Merge Engine -> повторный Search Pipeline. Штрихкод: существующий barcode enrichment flow. """

    cleaned_query = " ".join(
        str(query or "")
        .strip()
        .split()
    )

    pipeline_result = await run_search_pipeline(
        session=session,
        query=cleaned_query,
        intent_limit=6,
        family_limit=6,
        decision_candidates_limit=20,
    )

    # Текстовый поиск.
    if not is_possible_barcode(
        cleaned_query
    ):
        should_try_external = (
            should_try_external_text_catalog(
                query=cleaned_query,
                pipeline_result=pipeline_result,
            )
        )

        logger.info(
            "External text gate: "
            "query=%r screen=%s should_try=%s",
            cleaned_query,
            pipeline_result.screen,
            should_try_external,
        )

        if not should_try_external:
            return pipeline_result

        logger.info(
            "External Catalog Service: query=%r",
            cleaned_query,
        )

        try:
            catalog_result = await enrich_catalog(
                session=session,
                query=cleaned_query,
                limit_per_provider=8,
                stop_after_success=False,
                commit=True,
            )
        except Exception:
            logger.exception(
                "External Catalog Service failed: query=%r",
                cleaned_query,
            )
            with suppress(Exception):
                await session.rollback()
            return pipeline_result

        logger.info(
            "External Catalog Service result: "
            "query=%r providers=%s found=%s "
            "imported=%s skipped=%s failed=%s",
            cleaned_query,
            catalog_result.providers_attempted,
            catalog_result.total_found,
            catalog_result.total_imported,
            catalog_result.total_skipped,
            catalog_result.total_failed,
        )

        if not catalog_result.enriched:
            return pipeline_result

        return await run_search_pipeline(
            session=session,
            query=cleaned_query,
            intent_limit=6,
            family_limit=6,
            decision_candidates_limit=20,
        )

    # Поиск по штрихкоду.
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

    barcode_item = pipeline_result.barcode_product

    enrichment_result = await enrich_product_by_barcode(
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

    if not enrichment_result.enriched:
        logger.info(
            "Внешние источники не дали "
            "полезного обогащения для %s",
            cleaned_query,
        )

        await session.rollback()

        return await run_search_pipeline(
            session=session,
            query=cleaned_query,
            intent_limit=6,
            family_limit=6,
            decision_candidates_limit=20,
        )

    await session.commit()

    logger.info(
        "Внешнее обогащение выполнено: "
        "provider=%s",
        enrichment_result.provider,
    )

    return await run_search_pipeline(
        session=session,
        query=cleaned_query,
        intent_limit=6,
        family_limit=6,
        decision_candidates_limit=20,
    )


async def process_pipeline_result( *, message: Message, session, pipeline_result: SearchPipelineResult, ) -> None:
    """Показывает экран, выбранный Search Pipeline."""

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
        barcode_product = pipeline_result.barcode_product

        if barcode_product is None:
            await show_not_found_screen(
                message=message,
                query=pipeline_result.normalized_query,
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
    """Главный обработчик поиска MarkaRadar."""

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

    loading_message = await send_search_loader(
        message
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
