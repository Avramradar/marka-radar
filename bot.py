import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import config
from app.database.init_db import init_database
from app.database.session import close_database
from app.handlers.rating import router as rating_router
from app.handlers.search import router as search_router
from app.handlers.start import router as start_router


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("MarkaRadar")


async def main() -> None:
    logger.info("Запуск MarkaRadar")

    await init_database()

    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
        ),
    )

    dispatcher = Dispatcher()

    dispatcher.include_router(start_router)
    dispatcher.include_router(rating_router)
    dispatcher.include_router(search_router)

    logger.info("Все роутеры подключены")

    try:
        await dispatcher.start_polling(bot)
    finally:
        logger.info("Остановка MarkaRadar...")

        await bot.session.close()
        await close_database()

        logger.info("MarkaRadar остановлен")


if __name__ == "__main__":
    asyncio.run(main())
