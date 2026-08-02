from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message


router = Router()


@router.message(CommandStart())
async def start_handler(message: Message) -> None:
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
