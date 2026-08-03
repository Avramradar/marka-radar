import gzip
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.database.session import (
    async_session_maker,
    close_database,
)
from app.importers.open_food_facts_importer import (
    ImportStatistics,
    prepare_product,
    save_product,
)


logger = logging.getLogger(__name__)


@dataclass
class DumpImportStatistics:
    scanned_lines: int = 0
    russian_products: int = 0
    invalid_json: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0
    errors: int = 0
    brands_created: int = 0
    categories_created: int = 0


def is_russian_product(
    raw_product: dict[str, Any],
) -> bool:
    countries_tags = (
        raw_product.get("countries_tags")
        or []
    )

    if not isinstance(countries_tags, list):
        return False

    normalized_tags = {
        str(tag).strip().lower()
        for tag in countries_tags
    }

    return bool(
        {
            "en:russia",
            "ru:россия",
            "en:russian-federation",
        }
        & normalized_tags
    )


def add_import_statistics(
    total: DumpImportStatistics,
    current: ImportStatistics,
) -> None:
    total.created += current.created
    total.updated += current.updated
    total.errors += current.errors
    total.brands_created += current.brands_created
    total.categories_created += (
        current.categories_created
    )


async def import_dump(
    path: str,
    *,
    max_products: int | None = None,
    commit_every: int = 100,
) -> DumpImportStatistics:
    """
    Потоково читает архив Open Food Facts.

    В память загружается только одна строка JSON.
    Импортируются только товары, связанные с Россией.

    max_products:
        Ограничивает число обработанных российских товаров.
        Для первого теста используем 500.

    commit_every:
        Через сколько сохранённых товаров выполнять commit.
    """

    dump_path = Path(path)

    if not dump_path.is_file():
        raise FileNotFoundError(
            f"Файл дампа не найден: {dump_path}"
        )

    if commit_every < 1:
        raise ValueError(
            "commit_every должен быть больше нуля"
        )

    if max_products is not None and max_products < 1:
        raise ValueError(
            "max_products должен быть больше нуля"
        )

    statistics = DumpImportStatistics()
    pending_since_commit = 0

    try:
        async with async_session_maker() as session:
            with gzip.open(
                dump_path,
                mode="rt",
                encoding="utf-8",
                errors="replace",
            ) as dump_file:
                for line in dump_file:
                    statistics.scanned_lines += 1

                    try:
                        raw_product = json.loads(line)
                    except json.JSONDecodeError:
                        statistics.invalid_json += 1
                        continue

                    if not isinstance(raw_product, dict):
                        statistics.skipped += 1
                        continue

                    if not is_russian_product(raw_product):
                        continue

                    statistics.russian_products += 1

                    prepared = prepare_product(raw_product)

                    if prepared is None:
                        statistics.skipped += 1
                        continue

                    product_statistics = ImportStatistics()

                    try:
                        async with session.begin_nested():
                            await save_product(
                                session=session,
                                prepared=prepared,
                                statistics=product_statistics,
                            )
                    except Exception:
                        statistics.errors += 1

                        logger.exception(
                            "Ошибка импорта товара %s",
                            prepared.barcode,
                        )
                        continue

                    add_import_statistics(
                        total=statistics,
                        current=product_statistics,
                    )

                    pending_since_commit += 1

                    if pending_since_commit >= commit_every:
                        await session.commit()
                        pending_since_commit = 0

                        logger.info(
                            "Дамп: строк %s; российских %s; "
                            "создано %s; обновлено %s; "
                            "ошибок %s",
                            statistics.scanned_lines,
                            statistics.russian_products,
                            statistics.created,
                            statistics.updated,
                            statistics.errors,
                        )

                    processed_products = (
                        statistics.created
                        + statistics.updated
                    )

                    if (
                        max_products is not None
                        and processed_products
                        >= max_products
                    ):
                        logger.info(
                            "Достигнут лимит max_products=%s",
                            max_products,
                        )
                        break

            await session.commit()

    finally:
        await close_database()

    logger.info(
        "Импорт дампа завершён. "
        "Просмотрено строк: %s; "
        "российских товаров: %s; "
        "создано: %s; обновлено: %s; "
        "пропущено: %s; ошибки JSON: %s; "
        "ошибки импорта: %s; брендов: %s; "
        "категорий: %s.",
        statistics.scanned_lines,
        statistics.russian_products,
        statistics.created,
        statistics.updated,
        statistics.skipped,
        statistics.invalid_json,
        statistics.errors,
        statistics.brands_created,
        statistics.categories_created,
    )

    return statistics
