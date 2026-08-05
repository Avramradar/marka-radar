import logging
from dataclasses import replace
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.types import InlineKeyboardMarkup

from app.database.session import async_session_maker
from app.keyboards.decision_search import (
    get_decision_search_keyboard,
)
from app.keyboards.product_family import (
    get_product_families_keyboard,
)
from app.keyboards.search import (
    get_intent_groups_keyboard,
)
from app.search.decision_search import (
    DecisionProduct,
    DecisionSearchResult,
)
from app.search.intent_state import (
    clear_intent_groups,
    get_intent_group,
    save_intent_groups,
)
from app.search.search_pipeline import (
    SearchPipelineResult,
    SearchPipelineScreen,
    run_search_pipeline,
)


router = Router()
logger = logging.getLogger(__name__)


def format_decision_product(
    item: DecisionProduct,
) -> str:
    """
    Форматирует товар для экрана решения.
    """

    product_name = escape(
        str(item.name)
    )

    brand_name = str(
        item.brand_name or ""
    ).strip()

    if brand_name:
        title = (
            f"{escape(brand_name)} — "
            f"{product_name}"
        )
    else:
        title = product_name

    if item.votes_count > 0:
        rating_text = (
            f"⭐ {item.average_rating:.1f} из 10"
            f" · 👥 {item.votes_count}"
        )
    else:
        rating_text = "⭐ Оценок пока нет"

    trust_title = escape(
        str(
            item.trust_result.trust_title
        )
    )

    return (
        f"<b>{title}</b>\n"
        f"{rating_text}\n"
        f"🛡 {trust_title}"
    )


def prepare_decision_result(
    result: DecisionSearchResult,
) -> DecisionSearchResult:
    """
    Подготавливает компактную выдачу.

    Пока отдельная пагинация Decision Search
    не подключена, остальные товары не передаются
    в клавиатуру. Это исключает появление
    неработающей кнопки «Показать ещё».
    """

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

    return replace(
        result,
        alternatives=alternatives,
        insufficient_data=insufficient_data,
        other_products=[],
    )


def build_decision_text(
    *,
    query: str,
    result: DecisionSearchResult,
    explanation: str | None,
) -> str:
    """
    Формирует экран решения после выбора фасета.
    """

    lines = [
        "🎯 <b>Результат MarkaRadar</b>",
        "",
        f"Уточнение: «{escape(query)}»",
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
                        f"{escape(str(reasons[0]))}"
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
                    "но оценок пока недостаточно, "
                    "чтобы уверенно назвать лучший."
                ),
                "",
            ]
        )

    if result.alternatives:
        lines.extend(
            [
                "👍 <b>Подходящие варианты</b>",
                (
                    "Выберите товар, чтобы увидеть "
                    "его рейтинг и уровень доверия."
                ),
                "",
            ]
        )

    if result.insufficient_data:
        lines.extend(
            [
                "⚪ <b>Мало данных</b>",
                (
                    "У этих товаров пока слишком "
                    "мало оценок для устойчивого "
                    "вывода."
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
        "Нажмите на товар, чтобы открыть "
        "подробную карточку и поставить оценку."
    )

    return "\n".join(
        lines
    )


async def edit_or_send(
    *,
    callback: CallbackQuery,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Пытается заменить исходное сообщение.

    Если Telegram не разрешает редактирование,
    отправляет новое сообщение.
    """

    if callback.message is None:
        return

    try:
        await callback.message.edit_text(
            text,
            reply_markup=reply_markup,
        )

    except TelegramBadRequest as error:
        error_text = str(
            error
        ).lower()

        if "message is not modified" in error_text:
            return

        logger.warning(
            "Не удалось изменить сообщение "
            "после выбора уточнения",
            exc_info=True,
        )

        await callback.message.answer(
            text,
            reply_markup=reply_markup,
        )


async def show_not_found(
    *,
    callback: CallbackQuery,
    query: str,
) -> None:
    """
    Показывает отсутствие результатов
    после выбора уточнения.
    """

    await edit_or_send(
        callback=callback,
        text=(
            "🔍 <b>Подходящих товаров "
            "не найдено</b>\n\n"
            f"Запрос: «{escape(query)}»\n\n"
            "Попробуйте выбрать другое уточнение "
            "или выполнить новый поиск."
        ),
    )


async def show_pipeline_decision(
    *,
    callback: CallbackQuery,
    pipeline_result: SearchPipelineResult,
) -> None:
    """
    Показывает результат Decision Search.
    """

    decision = pipeline_result.decision

    if (
        decision is None
        or not decision.has_results
    ):
        await show_not_found(
            callback=callback,
            query=pipeline_result.normalized_query,
        )
        return

    prepared_result = prepare_decision_result(
        decision
    )

    has_buttons = bool(
        prepared_result.best_choice
        or prepared_result.alternatives
        or prepared_result.insufficient_data
    )

    keyboard = None

    if has_buttons:
        keyboard = get_decision_search_keyboard(
            prepared_result
        )

    text = build_decision_text(
        query=pipeline_result.normalized_query,
        result=prepared_result,
        explanation=pipeline_result.explanation,
    )

    await edit_or_send(
        callback=callback,
        text=text,
        reply_markup=keyboard,
    )


async def show_pipeline_intents(
    *,
    callback: CallbackQuery,
    pipeline_result: SearchPipelineResult,
) -> None:
    """
    Показывает следующий уровень фасетов.

    При allow_refinements=False этот экран
    обычно не должен возвращаться. Функция
    оставлена как безопасная обработка результата.
    """

    if callback.message is None:
        return

    groups = pipeline_result.intent_groups

    if not groups:
        await show_pipeline_decision(
            callback=callback,
            pipeline_result=pipeline_result,
        )
        return

    save_intent_groups(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        groups=groups,
    )

    await edit_or_send(
        callback=callback,
        text=(
            "🧭 <b>Уточните выбор</b>\n\n"
            f"Запрос: «"
            f"{escape(pipeline_result.normalized_query)}"
            f"»\n\n"
            "Выберите подходящую характеристику. "
            "После этого MarkaRadar сравнит "
            "товары по оценкам и доверию:"
        ),
        reply_markup=(
            get_intent_groups_keyboard(
                groups
            )
        ),
    )


async def show_pipeline_families(
    *,
    callback: CallbackQuery,
    pipeline_result: SearchPipelineResult,
) -> None:
    """
    Показывает резервные семейства товаров.

    При allow_refinements=False этот экран
    также не должен возвращаться.
    """

    families = pipeline_result.families

    if not families:
        await show_pipeline_decision(
            callback=callback,
            pipeline_result=pipeline_result,
        )
        return

    await edit_or_send(
        callback=callback,
        text=(
            "🧭 <b>Уточните вид продукта</b>\n\n"
            f"Запрос: «"
            f"{escape(pipeline_result.normalized_query)}"
            f"»\n\n"
            "Выберите подходящий вариант:"
        ),
        reply_markup=(
            get_product_families_keyboard(
                families
            )
        ),
    )


async def process_callback_pipeline_result(
    *,
    callback: CallbackQuery,
    pipeline_result: SearchPipelineResult,
) -> None:
    """
    Показывает экран, выбранный Search Pipeline.
    """

    if callback.message is None:
        return

    if (
        pipeline_result.screen
        == SearchPipelineScreen.INTENTS
    ):
        await show_pipeline_intents(
            callback=callback,
            pipeline_result=pipeline_result,
        )
        return

    clear_intent_groups(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
    )

    if (
        pipeline_result.screen
        == SearchPipelineScreen.FAMILIES
    ):
        await show_pipeline_families(
            callback=callback,
            pipeline_result=pipeline_result,
        )
        return

    if (
        pipeline_result.screen
        == SearchPipelineScreen.DECISION
    ):
        await show_pipeline_decision(
            callback=callback,
            pipeline_result=pipeline_result,
        )
        return

    await show_not_found(
        callback=callback,
        query=pipeline_result.normalized_query,
    )


@router.callback_query(
    F.data.startswith("intent:")
)
async def intent_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает выбор фасета.

    Старый сценарий:

        фасет
        → search_products(limit=100)
        → каталог с пагинацией.

    Новый сценарий:

        фасет
        → Search Pipeline
        → Decision Search
        → лучший выбор и альтернативы.

    После выбора фасета повторные уточнения
    отключаются через allow_refinements=False.
    """

    if callback.data is None:
        return

    if callback.message is None:
        await callback.answer()
        return

    try:
        group_index = int(
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
            "Некорректный вариант",
            show_alert=True,
        )
        return

    group = get_intent_group(
        chat_id=callback.message.chat.id,
        user_id=callback.from_user.id,
        index=group_index,
    )

    if group is None:
        await callback.answer(
            (
                "Список уточнений устарел. "
                "Повторите поиск."
            ),
            show_alert=True,
        )
        return

    query = str(
        group.get(
            "query",
            "",
        )
    ).strip()

    title = str(
        group.get(
            "title",
            query,
        )
    ).strip()

    if not query:
        await callback.answer(
            "Пустой поисковый запрос",
            show_alert=True,
        )
        return

    await callback.answer(
        "Сравниваю товары и оценки…"
    )

    try:
        async with async_session_maker() as session:
            pipeline_result = (
                await run_search_pipeline(
                    session=session,
                    query=query,
                    intent_limit=6,
                    family_limit=6,
                    decision_candidates_limit=30,
                    allow_refinements=False,
                )
            )

    except Exception:
        logger.exception(
            "Ошибка Search Pipeline "
            "после выбора фасета: %s",
            query,
        )

        await callback.message.answer(
            "⚠️ Не удалось выполнить поиск.\n"
            "Попробуйте ещё раз немного позже."
        )
        return

    logger.info(
        "Facet callback: title=%r, query=%r, "
        "screen=%s, candidates=%s",
        title,
        query,
        pipeline_result.screen,
        (
            pipeline_result
            .decision
            .total_candidates
            if pipeline_result.decision
            else 0
        ),
    )

    await process_callback_pipeline_result(
        callback=callback,
        pipeline_result=pipeline_result,
    )
