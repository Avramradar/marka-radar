import asyncio

from aiogram import Bot
from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import config
from app.database.init_db import init_database
from app.handlers.start import router as start_router
from app.handlers.search import router as search_router

async def main() -> None:
    await init_database()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dispatcher = Dispatcher()

    dispatcher.include_router(start_router)
    dispatcher.include_router(search_router)

    print("===================================")
    print("     MarkaRadar запускается")
    print("     База данных подключена")
    print("     Обработчик /start подключён")
    print("===================================")

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
