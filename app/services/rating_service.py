from collections.abc import Iterable

from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.rating import Rating


MINIMUM_VOTES = 20
DEFAULT_GLOBAL_AVERAGE = 7.0


RatingData = dict[str, float | int]
RatingsByProductId = dict[int, RatingData]


def normalize_product_ids(
    product_ids: Iterable[int],
) -> list[int]:
    """
    Очищает список идентификаторов товаров.

    Удаляет:
    - повторяющиеся значения;
    - неположительные идентификаторы.

    Сохраняет исходный порядок.
    """

    normalized_ids: list[int] = []
    seen_ids: set[int] = set()

    for raw_product_id in product_ids:
        try:
            product_id = int(
                raw_product_id
            )
        except (
            TypeError,
            ValueError,
        ):
            continue

        if product_id <= 0:
            continue

        if product_id in seen_ids:
            continue

        seen_ids.add(
            product_id
        )

        normalized_ids.append(
            product_id
        )

    return normalized_ids


async def get_product_rating(
    session: AsyncSession,
    product_id: int,
) -> tuple[float, int]:
    """
    Возвращает обычную среднюю оценку товара
    и количество пользовательских голосов.

    Функция сохранена для совместимости
    с существующими обработчиками карточек.
    """

    result = await session.execute(
        select(
            func.avg(
                Rating.score
            ),
            func.count(
                Rating.id
            ),
        ).where(
            Rating.product_id
            == product_id
        )
    )

    average, votes = result.one()

    if average is None:
        return 0.0, 0

    return (
        round(
            float(average),
            2,
        ),
        int(votes),
    )


async def get_global_average_rating(
    session: AsyncSession,
) -> float:
    """
    Возвращает среднюю оценку всех товаров
    MarkaRadar.

    Значение используется как базовый уровень
    при расчёте взвешенного рейтинга.
    """

    result = await session.execute(
        select(
            func.avg(
                Rating.score
            )
        )
    )

    global_average = (
        result.scalar_one_or_none()
    )

    if global_average is None:
        return DEFAULT_GLOBAL_AVERAGE

    return round(
        float(global_average),
        2,
    )


def calculate_weighted_rating(
    average_rating: float,
    votes_count: int,
    global_average: float,
) -> float:
    """
    Рассчитывает достоверный рейтинг товара.

    Товар с одной оценкой 10 не должен быть выше
    товара с рейтингом 9.2 и большим количеством
    пользовательских голосов.
    """

    if votes_count <= 0:
        return 0.0

    weighted_rating = (
        votes_count
        / (
            votes_count
            + MINIMUM_VOTES
        )
        * average_rating
        +
        MINIMUM_VOTES
        / (
            votes_count
            + MINIMUM_VOTES
        )
        * global_average
    )

    return round(
        weighted_rating,
        2,
    )


def build_rating_data(
    *,
    average_rating: float,
    votes_count: int,
    global_average: float,
) -> RatingData:
    """
    Формирует единый словарь рейтинга товара.
    """

    weighted_rating = (
        calculate_weighted_rating(
            average_rating=average_rating,
            votes_count=votes_count,
            global_average=global_average,
        )
    )

    return {
        "average_rating": round(
            float(average_rating),
            2,
        ),
        "votes_count": int(
            votes_count
        ),
        "weighted_rating": weighted_rating,
    }


def build_empty_rating_data() -> RatingData:
    """
    Возвращает пустой рейтинг товара.
    """

    return {
        "average_rating": 0.0,
        "votes_count": 0,
        "weighted_rating": 0.0,
    }


async def get_products_ratings(
    session: AsyncSession,
    product_ids: Iterable[int],
) -> RatingsByProductId:
    """
    Загружает рейтинги нескольких товаров
    одним агрегирующим SQL-запросом.

    Вместо:

        SELECT рейтинг товара 1
        SELECT рейтинг товара 2
        SELECT рейтинг товара 3

    выполняется:

        SELECT product_id, AVG(score), COUNT(id)
        FROM ratings
        WHERE product_id IN (...)
        GROUP BY product_id

    Для товаров без оценок возвращаются
    нулевые показатели.
    """

    normalized_ids = normalize_product_ids(
        product_ids
    )

    if not normalized_ids:
        return {}

    # Глобальная средняя загружается один раз
    # для всей группы товаров.
    global_average = (
        await get_global_average_rating(
            session=session
        )
    )

    result = await session.execute(
        select(
            Rating.product_id,
            func.avg(
                Rating.score
            ).label(
                "average_rating"
            ),
            func.count(
                Rating.id
            ).label(
                "votes_count"
            ),
        )
        .where(
            Rating.product_id.in_(
                normalized_ids
            )
        )
        .group_by(
            Rating.product_id
        )
    )

    ratings_by_product_id: (
        RatingsByProductId
    ) = {
        product_id: (
            build_empty_rating_data()
        )
        for product_id
        in normalized_ids
    }

    for row in result.all():
        product_id = int(
            row.product_id
        )

        average_rating = float(
            row.average_rating
            or 0.0
        )

        votes_count = int(
            row.votes_count
            or 0
        )

        ratings_by_product_id[
            product_id
        ] = build_rating_data(
            average_rating=average_rating,
            votes_count=votes_count,
            global_average=global_average,
        )

    return ratings_by_product_id


async def get_full_product_rating(
    session: AsyncSession,
    product_id: int,
) -> RatingData:
    """
    Возвращает все показатели одного товара.

    Внутри использует тот же формат данных,
    что и пакетная функция.
    """

    ratings = await get_products_ratings(
        session=session,
        product_ids=[
            product_id,
        ],
    )

    return ratings.get(
        int(product_id),
        build_empty_rating_data(),
    )
