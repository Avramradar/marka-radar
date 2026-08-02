from aiogram import Bot
from aiogram import Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
import asyncio
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")


async def main():
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )

    dp = Dispatcher()

    print("MarkaRadar запущен")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
