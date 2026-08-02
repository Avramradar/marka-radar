import asyncio

from aiogram import Bot
from aiogram import Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import config


async def main():
    bot = Bot(
        token=config.bot_token,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML
        ),
    )

    dispatcher = Dispatcher()

    print("===================================")
    print("     MarkaRadar запускается")
    print("===================================")

    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
