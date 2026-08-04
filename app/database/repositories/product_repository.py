from sqlalchemy import Float
from sqlalchemy import and_
from sqlalchemy import case
from sqlalchemy import cast
from sqlalchemy import exists
from sqlalchemy import func
from sqlalchemy import literal
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.models.product_alias import ProductAlias
from app.utils.text import (
    build_search_variants,
    normalize_text,
)


SearchResult = tuple[Product, Brand, Category]


def build_alias_ilike_condition(pattern: str):
    """
    Проверяет наличие синонима, содержащего искомый текст.

    Используется EXISTS, поэтому товары не дублируются
    из-за нескольких подходящих синонимов.
    """

    return exists(
        select(ProductAlias.id).where(
            ProductAlias.product_id == Product.id,
            ProductAlias.normalized_alias.ilike(pattern),
        )
    )


def build_alias_exact_condition(
    normalized_query: str,
):
    """Проверяет точное совпадение с синонимом товара."""

    return exists(
        select(ProductAlias.id).where(
            ProductAlias.product_id == Product.id,
            ProductAlias.normalized_alias
            == normalized_query,
        )
    )


def build_alias_trigram_condition(
    normalized_query: str,
):
    """
    Проверяет похожий синоним через pg_trgm.

    Оператор % использует similarity_threshold PostgreSQL.
    """

    return exists(
        select(ProductAlias.id).where(
            ProductAlias.product_id == Product.id,
            ProductAlias.normalized_alias.op("%")(
                normalized_query
            ),
        )
    )


def build_alias_word_similarity_condition(
    normalized_query: str,
):
    """
    Проверяет похожесть запроса на отдельное слово
    или часть фразы в синониме.
    """

    return exists(
        select(ProductAlias.id).where(
            ProductAlias.product_id == Product.id,
            func.word_similarity(
                normalized_query,
                ProductAlias.normalized_alias,
            )
            >= 0.40,
        )
    )


def build_alias_similarity_score(
    normalized_query: str,
):
    """
    Возвращает максимальную похожесть среди синонимов.

    Учитывает как similarity всей строки, так и
    word_similarity для неполных слов.
    """

    return (
        select(
            func.coalesce(
                func.max(
                    func.greatest(
                        func.similarity(
                            ProductAlias.normalized_alias,
                            normalized_query,
                        ),
                        func.word_similarity(
                            normalized_query,
                            ProductAlias.normalized_alias,
                        ),
                    )
                ),
                0.0,
            )
        )
        .where(
            ProductAlias.product_id == Product.id,
        )
        .correlate(Product)
        .scalar_subquery()
    )


def build_token_condition(token: str):
    """
    Строит строгое условие поиска для одного слова.

    Слово может встречаться:
    - в названии товара;
    - в названии бренда;
    - в альтернативных названиях бренда;
    - в категории;
    - в ключевых словах;
    - в подтипе;
    - в синонимах товара.
    """

    pattern = f"%{token}%"

    return or_(
        Product.normalized_name.ilike(pattern),
        Brand.normalized_name.ilike(pattern),
        Brand.aliases.ilike(pattern),
        Category.normalized_name.ilike(pattern),
        Product.keywords.ilike(pattern),
        Product.subtype.ilike(pattern),
        build_alias_ilike_condition(pattern),
    )


def deduplicate_results(
    rows: list[SearchResult],
    *,
    limit: int,
) -> list[SearchResult]:
    """
    Удаляет повторяющиеся товары,
    сохраняя порядок результатов.
    """

    unique_rows: list[SearchResult] = []
    seen_product_ids: set[int] = set()

    for row in rows:
        product = row[0]

        if product.id in seen_product_ids:
            continue

        seen_product_ids.add(product.id)
        unique_rows.append(row)

        if len(unique_rows) >= limit:
            break

    return unique_rows


async def search_by_barcode(
    session: AsyncSession,
    barcode: str,
    *,
    limit: int,
) -> list[SearchResult]:
    """Выполняет точный поиск по штрихкоду."""

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id == Brand.id,
        )
        .join(
            Category,
            Product.category_id == Category.id,
        )
        .where(
            Product.is_active.is_(True),
            Product.barcode == barcode,
        )
        .limit(limit)
    )

    result = await session.execute(statement)

    return list(result.all())


async def search_strict_variant(
    session: AsyncSession,
    normalized_query: str,
    *,
    limit: int,
    excluded_product_ids: set[int] | None = None,
) -> list[SearchResult]:
    """
    Выполняет строгий поиск одного варианта запроса.

    Все слова варианта должны встретиться хотя бы
    в одном из доступных поисковых полей.
    """

    tokens = [
        token
        for token in normalized_query.split()
        if token
    ]

    if not tokens:
        return []

    excluded_product_ids = (
        excluded_product_ids
        if excluded_product_ids is not None
        else set()
    )

    token_conditions = [
        build_token_condition(token)
        for token in tokens
    ]

    full_pattern = f"%{normalized_query}%"

    exact_alias_match = build_alias_exact_condition(
        normalized_query
    )

    full_alias_match = build_alias_ilike_condition(
        full_pattern
    )

    relevance_order = case(
        (
            Product.normalized_name
            == normalized_query,
            0,
        ),
        (
            Brand.normalized_name
            == normalized_query,
            1,
        ),
        (
            exact_alias_match,
            2,
        ),
        (
            Product.normalized_name.startswith(
                normalized_query
            ),
            3,
        ),
        (
            Brand.normalized_name.startswith(
                normalized_query
            ),
            4,
        ),
        (
            Product.normalized_name.ilike(
                full_pattern
            ),
            5,
        ),
        (
            Brand.normalized_name.ilike(
                full_pattern
            ),
            6,
        ),
        (
            full_alias_match,
            7,
        ),
        (
            Brand.aliases.ilike(
                full_pattern
            ),
            8,
        ),
        (
            Product.keywords.ilike(
                full_pattern
            ),
            9,
        ),
        (
            Category.normalized_name.ilike(
                full_pattern
            ),
            10,
        ),
        else_=11,
    )

    conditions = [
        Product.is_active.is_(True),
        and_(*token_conditions),
    ]

    if excluded_product_ids:
        conditions.append(
            Product.id.notin_(excluded_product_ids)
        )

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id == Brand.id,
        )
        .join(
            Category,
            Product.category_id == Category.id,
        )
        .where(
            *conditions,
        )
        .order_by(
            relevance_order.asc(),
            Brand.name.asc(),
            Product.name.asc(),
            Product.id.asc(),
        )
        .limit(limit)
    )

    result = await session.execute(statement)

    return list(result.all())


async def search_fuzzy_variant(
    session: AsyncSession,
    normalized_query: str,
    *,
    limit: int,
    excluded_product_ids: set[int] | None = None,
) -> list[SearchResult]:
    """
    Выполняет умный поиск через pg_trgm.

    Поддерживает:
    - опечатки;
    - неполные слова;
    - русскую транскрипцию;
    - латинские названия;
    - неправильную раскладку;
    - совпадение с частью длинного названия.
    """

    if len(normalized_query) < 3:
        return []

    excluded_product_ids = (
        excluded_product_ids
        if excluded_product_ids is not None
        else set()
    )

    empty_text = literal("")

    product_name_text = func.coalesce(
        Product.normalized_name,
        empty_text,
    )

    brand_name_text = func.coalesce(
        Brand.normalized_name,
        empty_text,
    )

    brand_aliases_text = func.coalesce(
        Brand.aliases,
        empty_text,
    )

    category_name_text = func.coalesce(
        Category.normalized_name,
        empty_text,
    )

    keywords_text = func.coalesce(
        Product.keywords,
        empty_text,
    )

    subtype_text = func.coalesce(
        Product.subtype,
        empty_text,
    )

    combined_name = func.concat_ws(
        " ",
        brand_name_text,
        product_name_text,
    )

    alias_similarity = build_alias_similarity_score(
        normalized_query
    )

    product_similarity = func.greatest(
        func.similarity(
            product_name_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            product_name_text,
        ),
    )

    brand_similarity = func.greatest(
        func.similarity(
            brand_name_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            brand_name_text,
        ),
    )

    brand_alias_similarity = func.greatest(
        func.similarity(
            brand_aliases_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            brand_aliases_text,
        ),
    )

    category_similarity = func.greatest(
        func.similarity(
            category_name_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            category_name_text,
        ),
    )

    keywords_similarity = func.greatest(
        func.similarity(
            keywords_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            keywords_text,
        ),
    )

    subtype_similarity = func.greatest(
        func.similarity(
            subtype_text,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            subtype_text,
        ),
    )

    combined_similarity = func.greatest(
        func.similarity(
            combined_name,
            normalized_query,
        ),
        func.word_similarity(
            normalized_query,
            combined_name,
        ),
    )

    # Точное начало строки получает дополнительный бонус.
    prefix_bonus = case(
        (
            Product.normalized_name.startswith(
                normalized_query
            ),
            0.40,
        ),
        (
            Brand.normalized_name.startswith(
                normalized_query
            ),
            0.35,
        ),
        else_=0.0,
    )

    # Название товара, связка "бренд + товар"
    # и бренд имеют наибольший вес.
    fuzzy_score = (
        func.greatest(
            product_similarity * 1.40,
            combined_similarity * 1.35,
            brand_similarity * 1.30,
            alias_similarity * 1.20,
            brand_alias_similarity * 1.15,
            category_similarity * 0.65,
            keywords_similarity * 0.60,
            subtype_similarity * 0.55,
        )
        + prefix_bonus
    )

    # Не полагаемся только на оператор %:
    # он может не пропускать короткие и неполные слова.
    fuzzy_condition = or_(
        Product.normalized_name.op("%")(
            normalized_query
        ),
        Brand.normalized_name.op("%")(
            normalized_query
        ),
        Brand.aliases.op("%")(
            normalized_query
        ),
        Category.normalized_name.op("%")(
            normalized_query
        ),
        Product.keywords.op("%")(
            normalized_query
        ),
        Product.subtype.op("%")(
            normalized_query
        ),
        build_alias_trigram_condition(
            normalized_query
        ),
        func.word_similarity(
            normalized_query,
            product_name_text,
        )
        >= 0.45,
        func.word_similarity(
            normalized_query,
            brand_name_text,
        )
        >= 0.45,
        func.word_similarity(
            normalized_query,
            combined_name,
        )
        >= 0.45,
        func.word_similarity(
            normalized_query,
            brand_aliases_text,
        )
        >= 0.50,
        func.word_similarity(
            normalized_query,
            keywords_text,
        )
        >= 0.55,
        build_alias_word_similarity_condition(
            normalized_query
        ),
    )

    conditions = [
        Product.is_active.is_(True),
        fuzzy_condition,
    ]

    if excluded_product_ids:
        conditions.append(
            Product.id.notin_(excluded_product_ids)
        )

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id == Brand.id,
        )
        .join(
            Category,
            Product.category_id == Category.id,
        )
        .where(
            *conditions,
        )
        .order_by(
            cast(
                fuzzy_score,
                Float,
            ).desc(),
            Brand.name.asc(),
            Product.name.asc(),
            Product.id.asc(),
        )
        .limit(limit)
    )

    result = await session.execute(statement)

    return list(result.all())


async def search_products(
    session: AsyncSession,
    query: str,
    limit: int = 20,
) -> list[SearchResult]:
    """
    Главная функция умного поиска.

    Ищет по:
    - штрихкоду;
    - полному и неполному названию;
    - бренду;
    - категории;
    - ключевым словам;
    - синонимам;
    - русской транскрипции;
    - латинскому написанию;
    - неправильной раскладке;
    - опечаткам.

    Порядок:
    1. точный штрихкод;
    2. строгий поиск исходного варианта;
    3. строгий поиск дополнительных вариантов;
    4. нечёткий поиск исходного варианта;
    5. нечёткий поиск транслитерации и раскладки.
    """

    raw_query = query.strip()
    normalized_query = normalize_text(query)

    if not normalized_query:
        return []

    if limit < 1:
        return []

    safe_limit = min(limit, 100)

    # Штрихкод имеет абсолютный приоритет.
    if raw_query.isdigit():
        barcode_products = await search_by_barcode(
            session=session,
            barcode=raw_query,
            limit=safe_limit,
        )

        if barcode_products:
            return barcode_products

    search_variants = build_search_variants(query)

    if not search_variants:
        return []

    collected_results: list[SearchResult] = []
    found_product_ids: set[int] = set()

    # Сначала выполняем строгий поиск.
    for search_variant in search_variants:
        remaining_limit = (
            safe_limit - len(collected_results)
        )

        if remaining_limit <= 0:
            break

        strict_results = await search_strict_variant(
            session=session,
            normalized_query=search_variant,
            limit=remaining_limit,
            excluded_product_ids=found_product_ids,
        )

        for row in strict_results:
            product = row[0]

            if product.id in found_product_ids:
                continue

            found_product_ids.add(product.id)
            collected_results.append(row)

            if len(collected_results) >= safe_limit:
                break

    if len(collected_results) >= safe_limit:
        return deduplicate_results(
            collected_results,
            limit=safe_limit,
        )

    # Затем подключаем нечёткий поиск.
    for search_variant in search_variants:
        remaining_limit = (
            safe_limit - len(collected_results)
        )

        if remaining_limit <= 0:
            break

        fuzzy_results = await search_fuzzy_variant(
            session=session,
            normalized_query=search_variant,
            limit=remaining_limit,
            excluded_product_ids=found_product_ids,
        )

        for row in fuzzy_results:
            product = row[0]

            if product.id in found_product_ids:
                continue

            found_product_ids.add(product.id)
            collected_results.append(row)

            if len(collected_results) >= safe_limit:
                break

    return deduplicate_results(
        collected_results,
        limit=safe_limit,
    )
