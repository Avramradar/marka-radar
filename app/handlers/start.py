from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.database.repositories.user_repository import (
    create_or_update_user,
)
from app.database.session import async_session_maker


router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    async with async_session_maker() as session:
        await create_or_update_user(
            session=session,
            telegram_user=message.from_user,
        )

    await message.answer(
        "<b>MarkaRadar</b>\n\n"
        "Я помогу выбрать продукты по оценкам пользователей, "
        "личным предпочтениям и ориентировочным ценам.\n\n"
        "Просто напишите название продукта или бренда.\n\n"
        "Например:\n"
        "• скумбрия\n"
        "• Доброфлот\n"
        "• кофе\n"
        "• Балтика 7"
    )
