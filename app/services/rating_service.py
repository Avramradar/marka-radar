from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.rating import Rating


MINIMUM_VOTES = 20
DEFAULT_GLOBAL_AVERAGE = 7.0


async def get_product_rating(
    session: AsyncSession,
    product_id: int,
) -> tuple[float, int]:
    """
    Возвращает обычную среднюю оценку товара
    и количество пользовательских голосов.
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


async def get_global_average_rating(
    session: AsyncSession,
) -> float:
    """
    Возвращает среднюю оценку всех товаров MarkaRadar.
    """

    result = await session.execute(
        select(
            func.avg(Rating.score)
        )
    )

    global_average = result.scalar_one_or_none()

    if global_average is None:
        return DEFAULT_GLOBAL_AVERAGE

    return round(float(global_average), 2)


def calculate_weighted_rating(
    average_rating: float,
    votes_count: int,
    global_average: float,
) -> float:
    """
    Рассчитывает достоверный рейтинг товара.

    Товар с одной оценкой 10 не должен быть выше товара
    с рейтингом 9.2 и большим количеством голосов.
    """

    if votes_count <= 0:
        return 0.0

    weighted_rating = (
        votes_count
        / (votes_count + MINIMUM_VOTES)
        * average_rating
        +
        MINIMUM_VOTES
        / (votes_count + MINIMUM_VOTES)
        * global_average
    )

    return round(weighted_rating, 2)


async def get_full_product_rating(
    session: AsyncSession,
    product_id: int,
) -> dict[str, float | int]:
    """
    Возвращает все показатели рейтинга товара.
    """

    average_rating, votes_count = await get_product_rating(
        session=session,
        product_id=product_id,
    )

    global_average = await get_global_average_rating(
        session=session,
    )

    weighted_rating = calculate_weighted_rating(
        average_rating=average_rating,
        votes_count=votes_count,
        global_average=global_average,
    )

    return {
        "average_rating": average_rating,
        "votes_count": votes_count,
        "weighted_rating": weighted_rating,
    }
