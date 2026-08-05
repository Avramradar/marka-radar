from dataclasses import dataclass
from enum import StrEnum
from math import log1p


class TrustLevel(StrEnum):
    NO_DATA = "no_data"
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendationStatus(StrEnum):
    RECOMMENDED = "recommended"
    GOOD_CHOICE = "good_choice"
    NEUTRAL = "neutral"
    BETTER_ALTERNATIVES = "better_alternatives"
    NOT_ENOUGH_DATA = "not_enough_data"


@dataclass(slots=True, frozen=True)
class TrustEngineResult:
    """
    Итог анализа товара системой доверия MarkaRadar.

    Все внутренние показатели находятся
    в диапазоне от 0 до 100.
    """

    average_rating: float
    votes_count: int

    rating_score: float
    trust_score: float
    data_quality_score: float
    popularity_score: float
    relevance_score: float
    recommendation_score: float

    trust_level: TrustLevel
    recommendation_status: RecommendationStatus

    trust_title: str
    recommendation_title: str
    explanation: tuple[str, ...]


def clamp(
    value: float,
    minimum: float = 0.0,
    maximum: float = 100.0,
) -> float:
    """
    Ограничивает число заданным диапазоном.
    """

    return max(
        minimum,
        min(value, maximum),
    )


def calculate_rating_score(
    average_rating: float,
) -> float:
    """
    Преобразует рейтинг 0–10 в шкалу 0–100.
    """

    normalized_rating = clamp(
        average_rating,
        minimum=0.0,
        maximum=10.0,
    )

    return normalized_rating * 10.0


def calculate_votes_trust_score(
    votes_count: int,
) -> float:
    """
    Рассчитывает доверие по количеству оценок.

    Используется логарифмический рост:
    первые оценки заметно повышают доверие,
    а затем рост постепенно замедляется.

    При 100 оценках показатель близок к 100.
    """

    safe_votes_count = max(
        0,
        votes_count,
    )

    if safe_votes_count == 0:
        return 0.0

    score = (
        log1p(safe_votes_count)
        / log1p(100)
        * 100
    )

    return clamp(score)


def calculate_trust_score(
    *,
    votes_count: int,
    data_quality_score: float,
) -> float:
    """
    Первая версия Trust Score.

    На данном этапе учитываются:
    - количество оценок — 85%;
    - полнота данных товара — 15%.

    Средняя оценка намеренно не участвует:
    Trust Score отражает надёжность данных,
    а не качество самого продукта.
    """

    votes_score = calculate_votes_trust_score(
        votes_count
    )

    normalized_data_quality = clamp(
        data_quality_score
    )

    score = (
        votes_score * 0.85
        + normalized_data_quality * 0.15
    )

    if votes_count == 0:
        return 0.0

    if votes_count < 5:
        score = min(score, 30.0)

    elif votes_count < 20:
        score = min(score, 55.0)

    elif votes_count < 100:
        score = min(score, 79.0)

    return clamp(score)


def determine_trust_level(
    votes_count: int,
) -> TrustLevel:
    """
    Определяет понятный пользователю уровень доверия.
    """

    if votes_count <= 0:
        return TrustLevel.NO_DATA

    if votes_count < 5:
        return TrustLevel.VERY_LOW

    if votes_count < 20:
        return TrustLevel.LOW

    if votes_count < 100:
        return TrustLevel.MEDIUM

    return TrustLevel.HIGH


def get_trust_title(
    trust_level: TrustLevel,
) -> str:
    titles = {
        TrustLevel.NO_DATA: (
            "⚪ Оценок пока нет"
        ),
        TrustLevel.VERY_LOW: (
            "🔵 Первые оценки"
        ),
        TrustLevel.LOW: (
            "🟡 Данных пока немного"
        ),
        TrustLevel.MEDIUM: (
            "🟢 Достоверность средняя"
        ),
        TrustLevel.HIGH: (
            "🛡 Высокая достоверность"
        ),
    }

    return titles[trust_level]


def calculate_recommendation_score(
    *,
    rating_score: float,
    trust_score: float,
    relevance_score: float,
    data_quality_score: float,
    popularity_score: float,
) -> float:
    """
    Рассчитывает общий Recommendation Score.

    Рейтинг и доверие имеют наибольший вес.
    Релевантность важна для поисковой выдачи.
    """

    score = (
        clamp(rating_score) * 0.35
        + clamp(trust_score) * 0.30
        + clamp(relevance_score) * 0.20
        + clamp(data_quality_score) * 0.10
        + clamp(popularity_score) * 0.05
    )

    return clamp(score)


def determine_recommendation_status(
    *,
    average_rating: float,
    votes_count: int,
    trust_score: float,
    recommendation_score: float,
) -> RecommendationStatus:
    """
    Определяет итоговый статус рекомендации.

    Система не делает уверенных выводов,
    если оценок недостаточно.
    """

    if votes_count < 5:
        return RecommendationStatus.NOT_ENOUGH_DATA

    if (
        average_rating >= 8.5
        and votes_count >= 20
        and trust_score >= 75
        and recommendation_score >= 80
    ):
        return RecommendationStatus.RECOMMENDED

    if (
        average_rating >= 7.5
        and votes_count >= 10
        and trust_score >= 50
        and recommendation_score >= 65
    ):
        return RecommendationStatus.GOOD_CHOICE

    if (
        average_rating < 6.0
        and votes_count >= 20
        and trust_score >= 55
    ):
        return (
            RecommendationStatus
            .BETTER_ALTERNATIVES
        )

    return RecommendationStatus.NEUTRAL


def get_recommendation_title(
    status: RecommendationStatus,
) -> str:
    titles = {
        RecommendationStatus.RECOMMENDED: (
            "🟢 MarkaRadar рекомендует"
        ),
        RecommendationStatus.GOOD_CHOICE: (
            "👍 Хороший выбор"
        ),
        RecommendationStatus.NEUTRAL: (
            "🟡 Нейтральный выбор"
        ),
        RecommendationStatus.BETTER_ALTERNATIVES: (
            "🟠 Есть более удачные альтернативы"
        ),
        RecommendationStatus.NOT_ENOUGH_DATA: (
            "⚪ Недостаточно данных"
        ),
    }

    return titles[status]


def build_explanation(
    *,
    average_rating: float,
    votes_count: int,
    trust_level: TrustLevel,
    status: RecommendationStatus,
    data_quality_score: float,
) -> tuple[str, ...]:
    """
    Формирует краткое и объяснимое обоснование.
    """

    explanations: list[str] = []

    if votes_count == 0:
        explanations.append(
            "Товар пока никто не оценил."
        )

        explanations.append(
            "MarkaRadar не может сделать "
            "уверенный вывод."
        )

        return tuple(explanations)

    if votes_count < 5:
        explanations.append(
            "Рейтинг сформирован по очень "
            "небольшому количеству оценок."
        )

    elif votes_count < 20:
        explanations.append(
            "Общее мнение уже видно, "
            "но данных пока немного."
        )

    elif votes_count < 100:
        explanations.append(
            "Рейтинг основан на заметном "
            "количестве оценок."
        )

    else:
        explanations.append(
            "Результат подтверждён большим "
            "количеством пользователей."
        )

    if average_rating >= 8.5:
        explanations.append(
            "Пользователи оценивают товар "
            "очень высоко."
        )

    elif average_rating >= 7.5:
        explanations.append(
            "Большинство пользователей "
            "оценивают товар положительно."
        )

    elif average_rating >= 6.0:
        explanations.append(
            "Оценки товара находятся "
            "в среднем диапазоне."
        )

    else:
        explanations.append(
            "Оценка ниже, чем у многих "
            "хорошо оценённых товаров."
        )

    if data_quality_score < 50:
        explanations.append(
            "Информация о товаре пока неполная."
        )

    if status == RecommendationStatus.RECOMMENDED:
        explanations.append(
            "Данных достаточно для уверенной "
            "положительной рекомендации."
        )

    elif (
        status
        == RecommendationStatus.BETTER_ALTERNATIVES
    ):
        explanations.append(
            "Стоит сравнить товар "
            "с более высоко оценёнными аналогами."
        )

    elif (
        status
        == RecommendationStatus.NOT_ENOUGH_DATA
    ):
        explanations.append(
            "Рекомендация может измениться "
            "после появления новых оценок."
        )

    return tuple(explanations)


def evaluate_product(
    *,
    average_rating: float,
    votes_count: int,
    data_quality_score: float = 50.0,
    popularity_score: float = 0.0,
    relevance_score: float = 100.0,
) -> TrustEngineResult:
    """
    Главная точка входа Trust Engine.

    Не выполняет запросов к БД и не зависит
    от Telegram, поэтому функцию легко тестировать.
    """

    safe_average_rating = clamp(
        average_rating,
        minimum=0.0,
        maximum=10.0,
    )

    safe_votes_count = max(
        0,
        votes_count,
    )

    normalized_data_quality = clamp(
        data_quality_score
    )

    normalized_popularity = clamp(
        popularity_score
    )

    normalized_relevance = clamp(
        relevance_score
    )

    rating_score = calculate_rating_score(
        safe_average_rating
    )

    trust_score = calculate_trust_score(
        votes_count=safe_votes_count,
        data_quality_score=(
            normalized_data_quality
        ),
    )

    recommendation_score = (
        calculate_recommendation_score(
            rating_score=rating_score,
            trust_score=trust_score,
            relevance_score=normalized_relevance,
            data_quality_score=(
                normalized_data_quality
            ),
            popularity_score=(
                normalized_popularity
            ),
        )
    )

    trust_level = determine_trust_level(
        safe_votes_count
    )

    recommendation_status = (
        determine_recommendation_status(
            average_rating=safe_average_rating,
            votes_count=safe_votes_count,
            trust_score=trust_score,
            recommendation_score=(
                recommendation_score
            ),
        )
    )

    explanation = build_explanation(
        average_rating=safe_average_rating,
        votes_count=safe_votes_count,
        trust_level=trust_level,
        status=recommendation_status,
        data_quality_score=(
            normalized_data_quality
        ),
    )

    return TrustEngineResult(
        average_rating=safe_average_rating,
        votes_count=safe_votes_count,
        rating_score=round(
            rating_score,
            1,
        ),
        trust_score=round(
            trust_score,
            1,
        ),
        data_quality_score=round(
            normalized_data_quality,
            1,
        ),
        popularity_score=round(
            normalized_popularity,
            1,
        ),
        relevance_score=round(
            normalized_relevance,
            1,
        ),
        recommendation_score=round(
            recommendation_score,
            1,
        ),
        trust_level=trust_level,
        recommendation_status=(
            recommendation_status
        ),
        trust_title=get_trust_title(
            trust_level
        ),
        recommendation_title=(
            get_recommendation_title(
                recommendation_status
            )
        ),
        explanation=explanation,
    )
