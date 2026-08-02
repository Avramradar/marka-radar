from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.price import PriceObservation


async def get_price_statistics(
    session: AsyncSession,
    product_id: int,
) -> dict | None:
    """
    Возвращает статистику цен товара.

    Используется медианная цена, а не обычное среднее,
    чтобы исключить влияние ошибочных или сильно
    завышенных предложений.
    """

    result = await session.execute(
        select(PriceObservation).where(
            PriceObservation.product_id == product_id
        )
    )

    prices = result.scalars().all()

    if not prices:
        return None

    values = [float(price.price) for price in prices]

    minimum = min(values)
    maximum = max(values)
    median_price = median(values)

    spread = maximum - minimum

    if median_price > 0:
        spread_percent = round(
            spread / median_price * 100,
            1,
        )
    else:
        spread_percent = 0

    return {
        "median": round(median_price, 2),
        "minimum": round(minimum, 2),
        "maximum": round(maximum, 2),
        "spread": round(spread, 2),
        "spread_percent": spread_percent,
        "warning": spread >= 500,
        "prices_count": len(values),
    }
