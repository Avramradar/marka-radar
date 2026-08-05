import logging
from dataclasses import replace
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

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
    Форматирует товар для экрана выбора.
    """

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
        rating_text = (
            f"⭐ {item.average_rating:.1f} из 10"
            f" · 👥 {item.votes_count}"
        )
    else:
        rating_text = (
            "⭐ Оценок пока нет"
        )

    return (
        f"<b>{title}</b>\n"
        f"{rating_text}\n"
        f"🛡 {escape(item.trust_result.trust_title)}"
    )


def prepare_decision_result(
    result: DecisionSearchResult,
) -> DecisionSearchResult:
    """
    Подготавливает компактную выдачу.

    Пока callback-пагинация Decision Search
    не подключена, остальные товары не передаются
    в клавиатуру, чтобы не создавать неработающую
    кнопку «Показать ещё».
    """

    alternatives = list(
        result.alternatives
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
                    "мало оценок для устойчивого вывода."
                ),
                "",
            ]
        )

    if explanation:
        lines.append(
            f"ℹ️ {escape(explanation)}"
        )

    return "\n".join(
        lines
    )


async def edit_or_send(
    *,
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
) -> None:
    """
    Пытается заменить текущее сообщение.

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
        await edit_or_send(
            callback=callback,
            text=(
                "🔍 <b>Подходящих товаров "
                "не найдено</b>\n\n"
                f"Запрос: «"
                f"{escape(pipeline_result.normalized_query)}"
                f"»\n\n"
                "Попробуйте выбрать другое уточнение."
            ),
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

    keyboard = (
        get_decision_search_keyboard(
            prepared_result
        )
        if has_buttons
        else None
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

    Это возможно, если после первого уточнения
    запрос всё ещё остаётся широким.
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
            "Можно выбрать ещё одну характеристику. "
            "После этого MarkaRadar сравнит товары "
            "по оценкам и уровню доверия:"
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
    Отображает экран, выбранный Search Pipeline.
    """

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

    await edit_or_send(
        callback=callback,
        text=(
            "🔍 <b>Подходящих товаров "
            "не найдено</b>\n\n"
            f"Запрос: «"
            f"{escape(pipeline_result.normalized_query)}"
            f"»\n\n"
            "Попробуйте выбрать другое уточнение "
            "или выполнить новый поиск."
        ),
    )


@router.callback_query(
    F.data.startswith("intent:")
)
async def intent_callback_handler(
    callback: CallbackQuery,
) -> None:
    """
    Обрабатывает выбор фасета.

    Главное отличие от старой версии:

    Раньше:
        фасет → search_products(limit=100)
        → каталог с пагинацией.

    Теперь:
        фасет → Search Pipeline
        → Decision Search
        → лучший выбор и альтернативы.
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
        "Facet callback: query=%r, screen=%s, "
        "intents=%s, families=%s, candidates=%s",
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

    await process_callback_pipeline_result(
        callback=callback,
        pipeline_result=pipeline_result,
    )
