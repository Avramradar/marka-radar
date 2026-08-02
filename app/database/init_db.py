from app.database.base import Base
from app.database.models import (
    Brand,
    Category,
    PriceObservation,
    Product,
    ProductAlias,
    ProductRelation,
    Rating,
    Review,
    SearchHistory,
    User,
)
from app.database.session import engine


async def init_database() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
