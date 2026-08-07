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


SearchResult = tuple[
    Product,
    Brand,
    Category,
]


UNKNOWN_BRAND_NAMES = (
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
)


def build_alias_ilike_condition( pattern: str, ):
    return exists(
        select(
            ProductAlias.id
        ).where(
            ProductAlias.product_id
            == Product.id,
            ProductAlias.normalized_alias.ilike(
                pattern
            ),
        )
    )


def build_alias_exact_condition( normalized_query: str, ):
    return exists(
        select(
            ProductAlias.id
        ).where(
            ProductAlias.product_id
            == Product.id,
            ProductAlias.normalized_alias
            == normalized_query,
        )
    )


def build_alias_trigram_condition( normalized_query: str, ):
    return exists(
        select(
            ProductAlias.id
        ).where(
            ProductAlias.product_id
            == Product.id,
            ProductAlias.normalized_alias.op(
                "%"
            )(
                normalized_query
            ),
        )
    )


def build_alias_word_similarity_condition( normalized_query: str, ):
    return exists(
        select(
            ProductAlias.id
        ).where(
            ProductAlias.product_id
            == Product.id,
            func.word_similarity(
                normalized_query,
                ProductAlias.normalized_alias,
            )
            >= 0.40,
        )
    )


def build_alias_similarity_score( normalized_query: str, ):
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
            ProductAlias.product_id
            == Product.id,
        )
        .correlate(
            Product
        )
        .scalar_subquery()
    )


def build_token_condition( token: str, ):
    """ Одно слово запроса может находиться в любом пользовательском или индексном поле товара. ВАЖНО: Product.search_text добавлен намеренно. В MarkaRadar это канонический собранный индекс, куда Product Merge Engine складывает имя, бренд, категорию, subtype, keywords, barcode и другие признаки. Именно его отсутствие раньше могло терять товары вроде Poetti Leggenda. """

    pattern = f"%{token}%"

    return or_(
        Product.normalized_name.ilike(
            pattern
        ),
        Brand.normalized_name.ilike(
            pattern
        ),
        Brand.aliases.ilike(
            pattern
        ),
        Category.normalized_name.ilike(
            pattern
        ),
        Product.keywords.ilike(
            pattern
        ),
        Product.subtype.ilike(
            pattern
        ),
        Product.search_text.ilike(
            pattern
        ),
        build_alias_ilike_condition(
            pattern
        ),
    )


def build_real_brand_order():
    normalized_brand_name = func.lower(
        func.trim(
            func.coalesce(
                Brand.name,
                "",
            )
        )
    )

    return case(
        (
            normalized_brand_name.in_(
                UNKNOWN_BRAND_NAMES
            ),
            1,
        ),
        else_=0,
    )


def build_generic_name_order( *, normalized_query: str, ):
    is_broad_single_word_query = (
        len(
            normalized_query.split()
        )
        == 1
    )

    return case(
        (
            and_(
                literal(
                    is_broad_single_word_query
                ),
                Product.normalized_name
                == normalized_query,
            ),
            1,
        ),
        else_=0,
    )


def build_informative_name_order( *, normalized_query: str, ):
    clean_product_name = func.trim(
        func.coalesce(
            Product.name,
            "",
        )
    )

    minimum_informative_length = (
        len(normalized_query) + 3
    )

    return case(
        (
            func.length(
                clean_product_name
            )
            > minimum_informative_length,
            0,
        ),
        else_=1,
    )


def build_card_quality_order():
    return (
        case(
            (
                Product.image_url.isnot(
                    None
                ),
                0,
            ),
            else_=1,
        )
        + case(
            (
                Product.barcode.isnot(
                    None
                ),
                0,
            ),
            else_=1,
        )
        + case(
            (
                and_(
                    Product.package_value.isnot(
                        None
                    ),
                    Product.package_unit.isnot(
                        None
                    ),
                ),
                0,
            ),
            else_=1,
        )
        + case(
            (
                Product.description.isnot(
                    None
                ),
                0,
            ),
            else_=1,
        )
    )


def deduplicate_results( rows: list[SearchResult], *, limit: int, ) -> list[SearchResult]:
    unique_rows: list[
        SearchResult
    ] = []

    seen_product_ids: set[
        int
    ] = set()

    for row in rows:
        product = row[0]

        product_id = int(
            product.id
        )

        if product_id in seen_product_ids:
            continue

        seen_product_ids.add(
            product_id
        )

        unique_rows.append(
            row
        )

        if len(unique_rows) >= limit:
            break

    return unique_rows


async def search_by_barcode( session: AsyncSession, barcode: str, *, limit: int, ) -> list[SearchResult]:
    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id
            == Brand.id,
        )
        .join(
            Category,
            Product.category_id
            == Category.id,
        )
        .where(
            Product.is_active.is_(
                True
            ),
            Product.barcode
            == barcode,
        )
        .limit(
            limit
        )
    )

    result = await session.execute(
        statement
    )

    return list(
        result.all()
    )


async def search_strict_variant( session: AsyncSession, normalized_query: str, *, limit: int, excluded_product_ids: ( set[int] | None ) = None, ) -> list[SearchResult]:
    """ Строгий поиск. Для многословного запроса каждый токен обязан встретиться хотя бы в одном поле карточки. Например: Poetti Leggenda может совпасть так: Poetti -> Brand.normalized_name Leggenda -> Product.normalized_name или оба слова могут находиться в search_text. """

    tokens = [
        token
        for token
        in normalized_query.split()
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
        build_token_condition(
            token
        )
        for token in tokens
    ]

    full_pattern = (
        f"%{normalized_query}%"
    )

    exact_alias_match = (
        build_alias_exact_condition(
            normalized_query
        )
    )

    full_alias_match = (
        build_alias_ilike_condition(
            full_pattern
        )
    )

    search_text_exact = (
        Product.search_text.ilike(
            full_pattern
        )
    )

    # Комбинация бренд + название — главный
    # пользовательский сценарий для запросов
    # "Poetti Leggenda", "Jacobs Monarch" и т.п.
    combined_name = func.concat_ws(
        " ",
        func.coalesce(
            Brand.normalized_name,
            "",
        ),
        func.coalesce(
            Product.normalized_name,
            "",
        ),
    )

    combined_full_match = (
        combined_name.ilike(
            full_pattern
        )
    )

    is_broad_single_word_query = (
        len(tokens) == 1
    )

    if is_broad_single_word_query:
        relevance_order = case(
            (
                Brand.normalized_name
                == normalized_query,
                0,
            ),
            (
                Product.normalized_name
                == normalized_query,
                1,
            ),
            (
                exact_alias_match,
                2,
            ),
            (
                Product.search_text.ilike(
                    full_pattern
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
                Product.normalized_name.startswith(
                    normalized_query
                ),
                5,
            ),
            (
                Product.normalized_name.ilike(
                    full_pattern
                ),
                6,
            ),
            (
                Brand.normalized_name.ilike(
                    full_pattern
                ),
                7,
            ),
            (
                full_alias_match,
                8,
            ),
            (
                Brand.aliases.ilike(
                    full_pattern
                ),
                9,
            ),
            (
                Product.keywords.ilike(
                    full_pattern
                ),
                10,
            ),
            (
                Category.normalized_name.ilike(
                    full_pattern
                ),
                11,
            ),
            else_=12,
        )

    else:
        relevance_order = case(
            (
                combined_full_match,
                0,
            ),
            (
                search_text_exact,
                1,
            ),
            (
                Product.normalized_name
                == normalized_query,
                2,
            ),
            (
                exact_alias_match,
                3,
            ),
            (
                Product.normalized_name.startswith(
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
                full_alias_match,
                6,
            ),
            (
                Product.keywords.ilike(
                    full_pattern
                ),
                7,
            ),
            else_=8,
        )

    generic_name_order = (
        build_generic_name_order(
            normalized_query=normalized_query,
        )
    )

    real_brand_order = (
        build_real_brand_order()
    )

    informative_name_order = (
        build_informative_name_order(
            normalized_query=normalized_query,
        )
    )

    card_quality_order = (
        build_card_quality_order()
    )

    conditions = [
        Product.is_active.is_(
            True
        ),
        and_(
            *token_conditions
        ),
    ]

    if excluded_product_ids:
        conditions.append(
            Product.id.notin_(
                excluded_product_ids
            )
        )

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id
            == Brand.id,
        )
        .join(
            Category,
            Product.category_id
            == Category.id,
        )
        .where(
            *conditions
        )
        .order_by(
            generic_name_order.asc(),
            relevance_order.asc(),
            real_brand_order.asc(),
            informative_name_order.asc(),
            card_quality_order.asc(),
            func.length(
                func.coalesce(
                    Product.name,
                    "",
                )
            ).desc(),
            Brand.name.asc(),
            Product.name.asc(),
            Product.id.asc(),
        )
        .limit(
            limit
        )
    )

    result = await session.execute(
        statement
    )

    return list(
        result.all()
    )


async def search_fuzzy_variant( session: AsyncSession, normalized_query: str, *, limit: int, excluded_product_ids: ( set[int] | None ) = None, ) -> list[SearchResult]:
    """ Нечёткий поиск pg_trgm. Дополнительно учитывает Product.search_text, чтобы данные, собранные Product Merge Engine, реально участвовали в поиске. """

    if len(normalized_query) < 3:
        return []

    excluded_product_ids = (
        excluded_product_ids
        if excluded_product_ids is not None
        else set()
    )

    empty_text = literal(
        ""
    )

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

    search_text = func.coalesce(
        Product.search_text,
        empty_text,
    )

    combined_name = func.concat_ws(
        " ",
        brand_name_text,
        product_name_text,
    )

    alias_similarity = (
        build_alias_similarity_score(
            normalized_query
        )
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

    brand_alias_similarity = (
        func.greatest(
            func.similarity(
                brand_aliases_text,
                normalized_query,
            ),
            func.word_similarity(
                normalized_query,
                brand_aliases_text,
            ),
        )
    )

    category_similarity = (
        func.greatest(
            func.similarity(
                category_name_text,
                normalized_query,
            ),
            func.word_similarity(
                normalized_query,
                category_name_text,
            ),
        )
    )

    keywords_similarity = (
        func.greatest(
            func.similarity(
                keywords_text,
                normalized_query,
            ),
            func.word_similarity(
                normalized_query,
                keywords_text,
            ),
        )
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

    combined_similarity = (
        func.greatest(
            func.similarity(
                combined_name,
                normalized_query,
            ),
            func.word_similarity(
                normalized_query,
                combined_name,
            ),
        )
    )

    search_text_similarity = (
        func.greatest(
            func.similarity(
                search_text,
                normalized_query,
            ),
            func.word_similarity(
                normalized_query,
                search_text,
            ),
        )
    )

    prefix_bonus = case(
        (
            combined_name.startswith(
                normalized_query
            ),
            0.45,
        ),
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

    fuzzy_score = (
        func.greatest(
            combined_similarity * 1.55,
            search_text_similarity * 1.50,
            product_similarity * 1.40,
            brand_similarity * 1.30,
            alias_similarity * 1.20,
            brand_alias_similarity * 1.15,
            category_similarity * 0.65,
            keywords_similarity * 0.60,
            subtype_similarity * 0.55,
        )
        + prefix_bonus
    )

    fuzzy_condition = or_(
        Product.normalized_name.op(
            "%"
        )(
            normalized_query
        ),
        Brand.normalized_name.op(
            "%"
        )(
            normalized_query
        ),
        Product.search_text.op(
            "%"
        )(
            normalized_query
        ),
        Brand.aliases.op(
            "%"
        )(
            normalized_query
        ),
        Category.normalized_name.op(
            "%"
        )(
            normalized_query
        ),
        Product.keywords.op(
            "%"
        )(
            normalized_query
        ),
        Product.subtype.op(
            "%"
        )(
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
        >= 0.42,
        func.word_similarity(
            normalized_query,
            search_text,
        )
        >= 0.40,
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

    generic_name_order = (
        build_generic_name_order(
            normalized_query=normalized_query,
        )
    )

    real_brand_order = (
        build_real_brand_order()
    )

    informative_name_order = (
        build_informative_name_order(
            normalized_query=normalized_query,
        )
    )

    card_quality_order = (
        build_card_quality_order()
    )

    conditions = [
        Product.is_active.is_(
            True
        ),
        fuzzy_condition,
    ]

    if excluded_product_ids:
        conditions.append(
            Product.id.notin_(
                excluded_product_ids
            )
        )

    statement = (
        select(
            Product,
            Brand,
            Category,
        )
        .join(
            Brand,
            Product.brand_id
            == Brand.id,
        )
        .join(
            Category,
            Product.category_id
            == Category.id,
        )
        .where(
            *conditions
        )
        .order_by(
            generic_name_order.asc(),
            cast(
                fuzzy_score,
                Float,
            ).desc(),
            real_brand_order.asc(),
            informative_name_order.asc(),
            card_quality_order.asc(),
            func.length(
                func.coalesce(
                    Product.name,
                    "",
                )
            ).desc(),
            Brand.name.asc(),
            Product.name.asc(),
            Product.id.asc(),
        )
        .limit(
            limit
        )
    )

    result = await session.execute(
        statement
    )

    return list(
        result.all()
    )


async def search_products( session: AsyncSession, query: str, limit: int = 20, ) -> list[SearchResult]:
    """ Главная функция поиска MarkaRadar. Порядок: 1. штрихкод; 2. строгий поиск; 3. дополнительные варианты; 4. fuzzy pg_trgm. Поиск теперь использует Product.search_text наравне с названием, брендом и aliases. """

    raw_query = query.strip()

    normalized_query = normalize_text(
        query
    )

    if not normalized_query:
        return []

    if limit < 1:
        return []

    safe_limit = min(
        limit,
        100,
    )

    if raw_query.isdigit():
        barcode_products = (
            await search_by_barcode(
                session=session,
                barcode=raw_query,
                limit=safe_limit,
            )
        )

        if barcode_products:
            return barcode_products

    search_variants = build_search_variants(
        query
    )

    if not search_variants:
        return []

    # Гарантируем, что исходный нормализованный
    # запрос всегда проверяется первым.
    ordered_variants: list[str] = [
        normalized_query
    ]

    for variant in search_variants:
        if (
            variant
            and variant
            not in ordered_variants
        ):
            ordered_variants.append(
                variant
            )

    collected_results: list[
        SearchResult
    ] = []

    found_product_ids: set[
        int
    ] = set()

    for search_variant in ordered_variants:
        remaining_limit = (
            safe_limit
            - len(
                collected_results
            )
        )

        if remaining_limit <= 0:
            break

        strict_results = (
            await search_strict_variant(
                session=session,
                normalized_query=search_variant,
                limit=remaining_limit,
                excluded_product_ids=(
                    found_product_ids
                ),
            )
        )

        for row in strict_results:
            product = row[0]
            product_id = int(
                product.id
            )

            if product_id in found_product_ids:
                continue

            found_product_ids.add(
                product_id
            )

            collected_results.append(
                row
            )

            if (
                len(
                    collected_results
                )
                >= safe_limit
            ):
                break

    if len(collected_results) >= safe_limit:
        return deduplicate_results(
            collected_results,
            limit=safe_limit,
        )

    for search_variant in ordered_variants:
        remaining_limit = (
            safe_limit
            - len(
                collected_results
            )
        )

        if remaining_limit <= 0:
            break

        fuzzy_results = (
            await search_fuzzy_variant(
                session=session,
                normalized_query=search_variant,
                limit=remaining_limit,
                excluded_product_ids=(
                    found_product_ids
                ),
            )
        )

        for row in fuzzy_results:
            product = row[0]
            product_id = int(
                product.id
            )

            if product_id in found_product_ids:
                continue

            found_product_ids.add(
                product_id
            )

            collected_results.append(
                row
            )

            if (
                len(
                    collected_results
                )
                >= safe_limit
            ):
                break

    return deduplicate_results(
        collected_results,
        limit=safe_limit,
    )
