from datetime import datetime

from aiogram.types import User as TelegramUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.user import User


async def create_or_update_user(
    session: AsyncSession,
    telegram_user: TelegramUser,
) -> User:
    result = await session.execute(
        select(User).where(
            User.telegram_id == telegram_user.id
        )
    )

    user = result.scalar_one_or_none()

    if user is None:
        user = User(
            telegram_id=telegram_user.id,
            username=telegram_user.username,
            first_name=telegram_user.first_name,
            last_name=telegram_user.last_name,
            language_code=telegram_user.language_code,
            created_at=datetime.utcnow(),
            last_activity=datetime.utcnow(),
        )

        session.add(user)

    else:
        user.username = telegram_user.username
        user.first_name = telegram_user.first_name
        user.last_name = telegram_user.last_name
        user.language_code = telegram_user.language_code
        user.last_activity = datetime.utcnow()

    await session.commit()
    await session.refresh(user)

    return user
