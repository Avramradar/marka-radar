import asyncio
import logging
from dataclasses import dataclass

from app.database.session import close_database
from app.importers.open_food_facts_importer import (
    ImportStatistics,
    import_open_food_facts_products,
)


logger = logging.getLogger(__name__)


@dataclass
class BulkImportStatistics:
    start_page: int
    requested_pages: int
    completed_pages: int = 0
    failed_pages: int = 0
    received: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: int = 0
    brands_created: int = 0
    categories_created: int = 0
    last_completed_page: int | None = None


def add_page_statistics(
    total: BulkImportStatistics,
    page_statistics: ImportStatistics,
) -> None:
    total.received += page_statistics.received
    total.created += page_statistics.created
    total.updated += page_statistics.updated
    total.skipped += page_statistics.skipped
    total.errors += page_statistics.errors
    total.brands_created += page_statistics.brands_created
    total.categories_created += (
        page_statistics.categories_created
    )


async def import_open_food_facts_pages(
    *,
    start_page: int = 1,
    pages_count: int = 10,
    page_size: int = 100,
    country: str = "russia",
    delay_seconds: int = 8,
    stop_on_page_error: bool = True,
) -> BulkImportStatistics:
    """
    Последовательно импортирует несколько страниц Open Food Facts.

    При page_size=100:
    - 10 страниц = до 1 000 товаров;
    - 100 страниц = до 10 000 товаров.

    start_page позволяет продолжить импорт после прерывания.
    """

    if start_page < 1:
        raise ValueError(
            "Начальная страница должна быть больше нуля"
        )

    if pages_count < 1 or pages_count > 100:
        raise ValueError(
            "Количество страниц должно быть от 1 до 100"
        )

    if page_size < 1 or page_size > 100:
        raise ValueError(
            "Количество товаров на странице "
            "должно быть от 1 до 100"
        )

    if delay_seconds < 6:
        raise ValueError(
            "Пауза должна быть не меньше 6 секунд, "
            "чтобы не превышать лимит Open Food Facts"
        )

    total = BulkImportStatistics(
        start_page=start_page,
        requested_pages=pages_count,
    )

    end_page = start_page + pages_count - 1

    logger.info(
        "Начинаем пакетный импорт Open Food Facts. "
        "Страницы: %s–%s; товаров на странице: %s.",
        start_page,
        end_page,
        page_size,
    )

    try:
        for page in range(start_page, end_page + 1):
            logger.info(
                "Импорт страницы %s из диапазона %s–%s",
                page,
                start_page,
                end_page,
            )

            try:
                page_statistics = (
                    await import_open_food_facts_products(
                        page=page,
                        page_size=page_size,
                        country=country,
                    )
                )
            except Exception:
                total.failed_pages += 1

                logger.exception(
                    "Ошибка импорта страницы %s. "
                    "Последняя успешно завершённая страница: %s",
                    page,
                    total.last_completed_page,
                )

                if stop_on_page_error:
                    raise

                if page < end_page:
                    await asyncio.sleep(delay_seconds)

                continue

            add_page_statistics(
                total=total,
                page_statistics=page_statistics,
            )

            total.completed_pages += 1
            total.last_completed_page = page

            logger.info(
                "Страница %s завершена. "
                "Всего создано: %s; обновлено: %s; "
                "пропущено: %s; ошибок товаров: %s.",
                page,
                total.created,
                total.updated,
                total.skipped,
                total.errors,
            )

            if page < end_page:
                logger.info(
                    "Пауза %s секунд перед следующей страницей",
                    delay_seconds,
                )
                await asyncio.sleep(delay_seconds)

    finally:
        await close_database()

    logger.info(
        "Пакетный импорт завершён. "
        "Страниц успешно: %s; страниц с ошибкой: %s; "
        "получено товаров: %s; создано: %s; "
        "обновлено: %s; пропущено: %s; "
        "ошибок товаров: %s; новых брендов: %s; "
        "новых категорий: %s; "
        "последняя страница: %s.",
        total.completed_pages,
        total.failed_pages,
        total.received,
        total.created,
        total.updated,
        total.skipped,
        total.errors,
        total.brands_created,
        total.categories_created,
        total.last_completed_page,
    )

    return total
