from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repositories.product_repository import (
    search_products,
)
from app.services.rating_service import (
    get_products_ratings,
)
from app.services.trust_engine import (
    RecommendationStatus,
    TrustEngineResult,
    evaluate_product,
)


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


MAX_DEEPLY_ANALYZED_CANDIDATES = 12


RatingData = dict[str, float | int]


@dataclass(slots=True)
class DecisionProduct:
    """
    Один товар, подготовленный для выдачи
    помощника выбора MarkaRadar.
    """

    product: Any
    brand: Any
    category: Any

    rating: RatingData

    # Цена не загружается на этапе поиска.
    # Она будет получена при открытии карточки.
    price_stats: dict[str, Any] | None

    trust_result: TrustEngineResult
    search_position: int

    @property
    def product_id(self) -> int:
        return int(
            self.product.id
        )

    @property
    def name(self) -> str:
        return str(
            self.product.name
        )

    @property
    def brand_name(self) -> str:
        brand_name = str(
            self.brand.name or ""
        ).strip()

        if (
            brand_name.lower()
            in UNKNOWN_BRAND_NAMES
        ):
            return ""

        return brand_name

    @property
    def average_rating(self) -> float:
        return float(
            self.trust_result.average_rating
        )

    @property
    def votes_count(self) -> int:
        return int(
            self.trust_result.votes_count
        )

    @property
    def recommendation_score(self) -> float:
        return float(
            self.trust_result.recommendation_score
        )

    @property
    def trust_score(self) -> float:
        return float(
            self.trust_result.trust_score
        )


@dataclass(slots=True)
class DecisionSearchResult:
    """
    Результат поиска, организованный
    вокруг принятия решения.

    best_choice:
        Лучший подтверждённый вариант.

    alternatives:
        Несколько хороших альтернатив.

    insufficient_data:
        Подходящие товары, по которым пока
        недостаточно пользовательских оценок.

    other_products:
        Остальные релевантные результаты.
    """

    query: str
    total_candidates: int

    best_choice: DecisionProduct | None

    alternatives: list[DecisionProduct]
    insufficient_data: list[DecisionProduct]
    other_products: list[DecisionProduct]

    @property
    def has_results(self) -> bool:
        return self.total_candidates > 0

    @property
    def has_confirmed_choice(self) -> bool:
        return self.best_choice is not None


def build_empty_rating() -> RatingData:
    """
    Возвращает пустой рейтинг товара.

    Используется как безопасный резерв,
    если товар отсутствует в пакетном результате.
    """

    return {
        "average_rating": 0.0,
        "votes_count": 0,
        "weighted_rating": 0.0,
    }


def build_empty_result(
    *,
    query: str,
) -> DecisionSearchResult:
    """
    Создаёт пустой результат Decision Search.
    """

    return DecisionSearchResult(
        query=query,
        total_candidates=0,
        best_choice=None,
        alternatives=[],
        insufficient_data=[],
        other_products=[],
    )


def is_real_brand(
    brand_name: str | None,
) -> bool:
    """
    Проверяет, указан ли настоящий бренд.
    """

    normalized = str(
        brand_name or ""
    ).strip().lower()

    return (
        normalized
        not in UNKNOWN_BRAND_NAMES
    )


def calculate_data_quality_score(
    *,
    product,
    brand,
    category,
) -> float:
    """
    Оценивает полноту информации о товаре.

    Цена намеренно не учитывается на этапе поиска:
    её загрузка для каждого кандидата замедляет
    выдачу и почти не влияет на первичное решение.

    Показатель не оценивает качество продукта.
    Он показывает полноту карточки товара.
    """

    score = 0.0

    product_name = str(
        product.name or ""
    ).strip()

    if product_name:
        score += 20.0

    if len(product_name) >= 4:
        score += 5.0

    if is_real_brand(
        brand.name
    ):
        score += 15.0

    category_name = str(
        category.name or ""
    ).strip()

    if category_name:
        score += 10.0

    if product.image_url:
        score += 15.0

    if product.barcode:
        score += 15.0

    if (
        product.package_value is not None
        and product.package_unit
    ):
        score += 10.0

    if product.description:
        score += 5.0

    if product.subtype:
        score += 3.0

    if product.keywords:
        score += 2.0

    return min(
        score,
        100.0,
    )


def calculate_relevance_score(
    *,
    position: int,
    candidates_count: int,
) -> float:
    """
    Оценивает релевантность по позиции
    в результате search_products.

    Верхние позиции получают больший балл.
    """

    if candidates_count <= 1:
        return 100.0

    safe_position = max(
        0,
        position,
    )

    position_ratio = (
        safe_position
        / max(
            candidates_count - 1,
            1,
        )
    )

    score = (
        100.0
        - position_ratio * 35.0
    )

    return max(
        65.0,
        min(
            score,
            100.0,
        ),
    )


def calculate_popularity_score(
    *,
    votes_count: int,
) -> float:
    """
    Рассчитывает осторожный сигнал популярности
    на основании количества оценок.
    """

    safe_votes = max(
        0,
        votes_count,
    )

    if safe_votes == 0:
        return 0.0

    if safe_votes < 5:
        return 15.0

    if safe_votes < 20:
        return 35.0

    if safe_votes < 50:
        return 50.0

    if safe_votes < 100:
        return 65.0

    if safe_votes < 300:
        return 80.0

    return 100.0


def decision_sort_key(
    item: DecisionProduct,
) -> tuple[
    float,
    float,
    int,
    float,
    int,
]:
    """
    Сортирует товары по полезности для выбора.

    Приоритет:

    1. Recommendation Score;
    2. Trust Score;
    3. количество оценок;
    4. средняя оценка;
    5. исходная позиция поиска.
    """

    return (
        item.recommendation_score,
        item.trust_score,
        item.votes_count,
        item.average_rating,
        -item.search_position,
    )


def is_confirmed_positive_choice(
    item: DecisionProduct,
) -> bool:
    """
    Проверяет, можно ли показывать товар
    как подтверждённый положительный вариант.
    """

    return (
        item.trust_result.recommendation_status
        in {
            RecommendationStatus.RECOMMENDED,
            RecommendationStatus.GOOD_CHOICE,
        }
    )


def has_enough_data(
    item: DecisionProduct,
) -> bool:
    """
    Проверяет, достаточно ли пользовательских данных
    для содержательного сравнения.
    """

    return (
        item.trust_result.recommendation_status
        != RecommendationStatus.NOT_ENOUGH_DATA
    )


def prepare_decision_product(
    *,
    product,
    brand,
    category,
    rating: RatingData,
    position: int,
    candidates_count: int,
) -> DecisionProduct:
    """
    Подготавливает товар и запускает Trust Engine.

    Функция больше не делает SQL-запросов.
    Рейтинг заранее передаётся из пакетной выборки.
    """

    average_rating = float(
        rating.get(
            "average_rating",
            0.0,
        )
    )

    votes_count = int(
        rating.get(
            "votes_count",
            0,
        )
    )

    data_quality_score = (
        calculate_data_quality_score(
            product=product,
            brand=brand,
            category=category,
        )
    )

    relevance_score = (
        calculate_relevance_score(
            position=position,
            candidates_count=candidates_count,
        )
    )

    popularity_score = (
        calculate_popularity_score(
            votes_count=votes_count,
        )
    )

    trust_result = evaluate_product(
        average_rating=average_rating,
        votes_count=votes_count,
        data_quality_score=data_quality_score,
        popularity_score=popularity_score,
        relevance_score=relevance_score,
    )

    return DecisionProduct(
        product=product,
        brand=brand,
        category=category,
        rating=rating,
        price_stats=None,
        trust_result=trust_result,
        search_position=position,
    )


async def run_decision_search(
    *,
    session: AsyncSession,
    query: str,
    candidates_limit: int = 20,
    alternatives_limit: int = 3,
    insufficient_limit: int = 3,
    other_limit: int = 8,
) -> DecisionSearchResult:
    """
    Главная точка входа Decision Search.

    Быстрая последовательность:

    1. получает релевантных кандидатов;
    2. оставляет верхние результаты;
    3. загружает все рейтинги одной пачкой;
    4. запускает Trust Engine в памяти;
    5. сортирует товары по полезности;
    6. формирует лучший выбор и альтернативы.

    Цены здесь намеренно не загружаются.
    """

    cleaned_query = " ".join(
        query.strip().split()
    )

    if not cleaned_query:
        return build_empty_result(
            query=query,
        )

    requested_candidates_limit = max(
        1,
        min(
            candidates_limit,
            50,
        ),
    )

    analyzed_candidates_limit = min(
        requested_candidates_limit,
        MAX_DEEPLY_ANALYZED_CANDIDATES,
    )

    safe_alternatives_limit = max(
        0,
        min(
            alternatives_limit,
            10,
        ),
    )

    safe_insufficient_limit = max(
        0,
        min(
            insufficient_limit,
            10,
        ),
    )

    safe_other_limit = max(
        0,
        min(
            other_limit,
            20,
        ),
    )

    rows = await search_products(
        session=session,
        query=cleaned_query,
        limit=analyzed_candidates_limit,
    )

    if not rows:
        return build_empty_result(
            query=cleaned_query,
        )

    candidates_count = len(
        rows
    )

    product_ids = [
        int(product.id)
        for product, _brand, _category
        in rows
    ]

    # Два SQL-запроса на весь набор:
    # 1. глобальная средняя;
    # 2. агрегаты рейтингов по product_id.
    ratings_by_product_id = (
        await get_products_ratings(
            session=session,
            product_ids=product_ids,
        )
    )

    prepared_products: list[
        DecisionProduct
    ] = []

    # Здесь SQL-запросов больше нет.
    for position, row in enumerate(
        rows
    ):
        product, brand, category = row

        product_id = int(
            product.id
        )

        rating = ratings_by_product_id.get(
            product_id,
            build_empty_rating(),
        )

        decision_product = (
            prepare_decision_product(
                product=product,
                brand=brand,
                category=category,
                rating=rating,
                position=position,
                candidates_count=(
                    candidates_count
                ),
            )
        )

        prepared_products.append(
            decision_product
        )

    confirmed_products = [
        item
        for item in prepared_products
        if has_enough_data(
            item
        )
    ]

    confirmed_products.sort(
        key=decision_sort_key,
        reverse=True,
    )

    positive_products = [
        item
        for item in confirmed_products
        if is_confirmed_positive_choice(
            item
        )
    ]

    best_choice: DecisionProduct | None = None

    if positive_products:
        best_choice = positive_products[0]

    alternatives: list[
        DecisionProduct
    ] = []

    if best_choice is not None:
        alternatives = [
            item
            for item in positive_products
            if (
                item.product_id
                != best_choice.product_id
            )
        ][:safe_alternatives_limit]

    insufficient_data = [
        item
        for item in prepared_products
        if not has_enough_data(
            item
        )
    ]

    insufficient_data.sort(
        key=lambda item: (
            item.average_rating,
            item.votes_count,
            item.trust_result.relevance_score,
            -item.search_position,
        ),
        reverse=True,
    )

    insufficient_data = (
        insufficient_data[
            :safe_insufficient_limit
        ]
    )

    excluded_product_ids = {
        item.product_id
        for item in alternatives
    }

    excluded_product_ids.update(
        item.product_id
        for item in insufficient_data
    )

    if best_choice is not None:
        excluded_product_ids.add(
            best_choice.product_id
        )

    other_products = [
        item
        for item in confirmed_products
        if (
            item.product_id
            not in excluded_product_ids
        )
    ][:safe_other_limit]

    return DecisionSearchResult(
        query=cleaned_query,
        total_candidates=candidates_count,
        best_choice=best_choice,
        alternatives=alternatives,
        insufficient_data=insufficient_data,
        other_products=other_products,
    )
