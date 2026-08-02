from datetime import datetime

from aiogram import F
from aiogram import Router
from aiogram.types import CallbackQuery
from sqlalchemy import select

from app.database.models.rating import Rating
from app.database.repositories.user_repository import (
    create_or_update_user,
)
from app.database.session import async_session_maker
from app.services.rating_service import get_full_product_rating


router = Router()


@router.callback_query(F.data.startswith("rate:"))
async def rating_handler(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer(
            "Не удалось определить оценку.",
            show_alert=True,
        )
        return

    try:
        _, product_id_raw, score_raw = callback.data.split(":")
        product_id = int(product_id_raw)
        score = int(score_raw)
    except (ValueError, TypeError):
        await callback.answer(
            "Некорректные данные оценки.",
            show_alert=True,
        )
        return

    if score < 1 or score > 10:
        await callback.answer(
            "Оценка должна быть от 1 до 10.",
            show_alert=True,
        )
        return

    async with async_session_maker() as session:
        await create_or_update_user(
            session=session,
            telegram_user=callback.from_user,
        )

        result = await session.execute(
            select(Rating).where(
                Rating.user_id == callback.from_user.id,
                Rating.product_id == product_id,
            )
        )

        rating = result.scalar_one_or_none()
        now = datetime.utcnow()

        if rating is None:
            rating = Rating(
                user_id=callback.from_user.id,
                product_id=product_id,
                score=score,
                created_at=now,
                updated_at=now,
            )
            session.add(rating)
            action_text = "Оценка сохранена"
        else:
            rating.score = score
            rating.updated_at = now
            action_text = "Оценка изменена"

        await session.commit()

        rating_stats = await get_full_product_rating(
            session=session,
            product_id=product_id,
        )

    await callback.answer(action_text)

    if callback.message is not None:
        await callback.message.answer(
            f"✅ Ваша оценка: <b>{score} из 10</b>\n\n"
            "⭐ Новый рейтинг товара: "
            f"<b>{rating_stats['average_rating']:.1f} из 10</b>\n"
            "👥 Количество оценок: "
            f"<b>{rating_stats['votes_count']}</b>"
        )
