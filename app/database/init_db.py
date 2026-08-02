import logging

from sqlalchemy import text

from app.database.session import engine


logger = logging.getLogger(__name__)


async def check_database_connection() -> None:
    """
    Проверяет доступность базы данных перед запуском бота.

    Структура таблиц создаётся и обновляется только
    через Alembic-миграции.
    """

    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))

    logger.info("Подключение к базе данных успешно проверено")
