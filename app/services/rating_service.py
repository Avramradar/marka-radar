from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.rating import Rating


async def get_product_rating(
    session: AsyncSession,
    product_id: int,
) -> tuple[float, int]:
    """
    Возвращает:
    (средняя_оценка, количество_голосов)
    """

    result = await session.execute(
        select(
            func.avg(Rating.score),
            func.count(Rating.id),
        ).where(
            Rating.product_id == product_id
        )
    )

    average, votes = result.one()

    if average is None:
        return 0.0, 0

    return round(float(average), 2), int(votes)
