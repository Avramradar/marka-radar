from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.product_family_search_repository import (
    search_product_families,
)


async def find_product_families(
    *,
    session: AsyncSession,
    query: str,
    limit: int = 10,
) -> list[dict]:
    """
    Выполняет поиск семейств товаров.

    Это отдельный слой между обработчиком
    Telegram и репозиторием БД.

    В дальнейшем здесь можно будет:

    - учитывать историю поиска;
    - популярность товаров;
    - рейтинг;
    - персональные рекомендации;
    - исправление опечаток;
    - машинное ранжирование.

    Пока сервис лишь делегирует поиск
    репозиторию.
    """

    families = await search_product_families(
        session=session,
        query=query,
        limit=limit,
    )

    return families
