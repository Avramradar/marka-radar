from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.category import Category
from app.utils.text import normalize_text


@dataclass(slots=True, frozen=True)
class CategoryMappingResult:
    """
    Результат определения категории.

    category:
        Найденная категория MarkaRadar.

    matched_by:
        Каким способом она была найдена.

    confidence:
        Условная уверенность 0..100.

    source_value:
        Исходная внешняя категория,
        по которой произошло совпадение.
    """

    category: Category | None
    matched_by: str
    confidence: float
    source_value: str | None


CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "молоко": (
        "milk",
        "milks",
        "drinking milk",
        "whole milk",
        "skimmed milk",
        "semi skimmed milk",
        "pasteurized milk",
        "pasteurised milk",
        "uht milk",
        "dairy milk",
        "молоко питьевое",
        "питьевое молоко",
        "ультрапастеризованное молоко",
        "пастеризованное молоко",
    ),

    "кофе": (
        "coffee",
        "coffees",
        "instant coffee",
        "ground coffee",
        "coffee beans",
        "roasted coffee",
        "soluble coffee",
        "кофе растворимый",
        "растворимый кофе",
        "кофе молотый",
        "молотый кофе",
        "кофе в зернах",
        "кофе в зёрнах",
    ),

    "пицца": (
        "pizza",
        "pizzas",
        "frozen pizza",
        "fresh pizza",
        "пицца замороженная",
        "замороженная пицца",
    ),

    "сельдь": (
        "herring",
        "herrings",
        "salted herring",
        "herring fillet",
        "herring fillets",
        "сельдь филе",
        "филе сельди",
    ),

    "чай": (
        "tea",
        "teas",
        "black tea",
        "green tea",
        "herbal tea",
        "чай черный",
        "чай чёрный",
        "чай зеленый",
        "чай зелёный",
    ),

    "сыр": (
        "cheese",
        "cheeses",
        "hard cheese",
        "soft cheese",
        "processed cheese",
    ),

    "йогурт": (
        "yogurt",
        "yoghurt",
        "yogurts",
        "yoghurts",
    ),

    "кефир": (
        "kefir",
        "кефир",
    ),

    "вода": (
        "water",
        "waters",
        "mineral water",
        "drinking water",
        "sparkling water",
        "still water",
    ),

    "сок": (
        "juice",
        "juices",
        "fruit juice",
        "vegetable juice",
    ),

    "масло": (
        "butter",
        "масло сливочное",
        "сливочное масло",
    ),
}


def clean_category_text(
    value: str | None,
) -> str:
    """
    Убирает лишние пробелы.
    """

    if not value:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def normalize_category_text(
    value: str | None,
) -> str:
    """
    Нормализует название категории
    для сравнения.
    """

    cleaned = clean_category_text(
        value
    )

    if not cleaned:
        return ""

    return (
        normalize_text(
            cleaned
        )
        .replace(
            "ё",
            "е",
        )
        .strip()
    )


def normalize_external_categories(
    categories: Iterable[str],
) -> list[str]:
    """
    Очищает список внешних категорий.

    Удаляет пустые значения и дубли.
    """

    result: list[str] = []
    seen: set[str] = set()

    for value in categories:
        cleaned = clean_category_text(
            value
        )

        normalized_value = (
            normalize_category_text(
                cleaned
            )
        )

        if not normalized_value:
            continue

        if normalized_value in seen:
            continue

        seen.add(
            normalized_value
        )

        result.append(
            cleaned
        )

    return result


async def find_category_exact(
    *,
    session: AsyncSession,
    value: str,
) -> Category | None:
    """
    Ищет точное совпадение
    по name или normalized_name.
    """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    result = await session.execute(
        select(
            Category
        )
        .where(
            or_(
                Category.normalized_name
                == normalized_value,
                func.lower(
                    Category.name
                )
                == normalized_value,
            )
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


def detect_canonical_category_name(
    value: str,
) -> str | None:
    """
    Преобразует внешнее название
    в канонический тип категории MarkaRadar.

    Например:

        instant coffee -> кофе
        uht milk       -> молоко
        frozen pizza   -> пицца
    """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    for (
        canonical_name,
        aliases,
    ) in CATEGORY_ALIASES.items():
        canonical_normalized = (
            normalize_category_text(
                canonical_name
            )
        )

        if (
            normalized_value
            == canonical_normalized
        ):
            return canonical_name

        for alias in aliases:
            normalized_alias = (
                normalize_category_text(
                    alias
                )
            )

            if (
                normalized_value
                == normalized_alias
            ):
                return canonical_name

    return None


def detect_canonical_category_contains(
    value: str,
) -> str | None:
    """
    Более мягкий fallback.

    Используется только после точных совпадений.

    Например:

        "en:instant-coffees"
        "instant coffee beverages"

    могут быть отнесены к кофе.
    """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    # Некоторые API используют
    # префиксы языков вроде en:, ru:.
    normalized_value = (
        normalized_value
        .replace(
            "en:",
            "",
        )
        .replace(
            "ru:",
            "",
        )
        .replace(
            "-",
            " ",
        )
    )

    normalized_value = " ".join(
        normalized_value.split()
    )

    matches: list[
        tuple[int, str]
    ] = []

    for (
        canonical_name,
        aliases,
    ) in CATEGORY_ALIASES.items():
        terms = (
            canonical_name,
            *aliases,
        )

        for term in terms:
            normalized_term = (
                normalize_category_text(
                    term
                )
            )

            if not normalized_term:
                continue

            if (
                normalized_term
                in normalized_value
            ):
                matches.append(
                    (
                        len(
                            normalized_term
                        ),
                        canonical_name,
                    )
                )

    if not matches:
        return None

    # Самое длинное совпадение
    # обычно наиболее специфично.
    matches.sort(
        reverse=True
    )

    return matches[0][1]


async def find_canonical_category(
    *,
    session: AsyncSession,
    canonical_name: str,
) -> Category | None:
    """
    Ищет реальную категорию MarkaRadar
    по каноническому имени.

    Важно:
    Mapper не создаёт категории самостоятельно.
    """

    normalized_name = (
        normalize_category_text(
            canonical_name
        )
    )

    result = await session.execute(
        select(
            Category
        )
        .where(
            Category.normalized_name
            == normalized_name
        )
        .order_by(
            Category.parent_id
            .isnot(
                None
            )
            .asc(),
            Category.id.asc(),
        )
        .limit(
            1
        )
    )

    category = (
        result.scalar_one_or_none()
    )

    if category is not None:
        return category

    # Fallback для старой базы,
    # где normalized_name мог быть
    # сформирован иначе.
    result = await session.execute(
        select(
            Category
        )
        .where(
            func.lower(
                Category.name
            )
            == canonical_name.lower()
        )
        .order_by(
            Category.id.asc()
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def map_external_category(
    *,
    session: AsyncSession,
    category_name: str | None = None,
    categories: Iterable[str] | None = None,
) -> CategoryMappingResult:
    """
    Главная функция Category Mapper.

    Приоритет:

    1. точное совпадение с категорией MarkaRadar;
    2. точное совпадение через контролируемые aliases;
    3. осторожное совпадение по содержимому;
    4. ничего не угадываем.

    Mapper намеренно НЕ создаёт новую категорию
    автоматически.

    Ошибочно объединённые категории опаснее,
    чем временно неразобранный товар.
    """

    incoming_values: list[str] = []

    if category_name:
        incoming_values.append(
            category_name
        )

    if categories:
        incoming_values.extend(
            categories
        )

    incoming_values = (
        normalize_external_categories(
            incoming_values
        )
    )

    if not incoming_values:
        return CategoryMappingResult(
            category=None,
            matched_by="none",
            confidence=0.0,
            source_value=None,
        )

    # 1. Сначала ищем прямое совпадение
    # с уже существующей категорией.
    for value in incoming_values:
        category = await find_category_exact(
            session=session,
            value=value,
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by="exact",
                confidence=100.0,
                source_value=value,
            )

    # 2. Контролируемый словарь aliases.
    for value in incoming_values:
        canonical_name = (
            detect_canonical_category_name(
                value
            )
        )

        if canonical_name is None:
            continue

        category = (
            await find_canonical_category(
                session=session,
                canonical_name=canonical_name,
            )
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by="alias",
                confidence=95.0,
                source_value=value,
            )

    # 3. Более мягкое совпадение.
    for value in incoming_values:
        canonical_name = (
            detect_canonical_category_contains(
                value
            )
        )

        if canonical_name is None:
            continue

        category = (
            await find_canonical_category(
                session=session,
                canonical_name=canonical_name,
            )
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by="contains",
                confidence=80.0,
                source_value=value,
            )

    return CategoryMappingResult(
        category=None,
        matched_by="not_found",
        confidence=0.0,
        source_value=(
            incoming_values[0]
            if incoming_values
            else None
        ),
    )


async def map_external_category_id(
    *,
    session: AsyncSession,
    category_name: str | None = None,
    categories: Iterable[str] | None = None,
) -> int | None:
    """
    Удобная сокращённая функция.

    Возвращает только category_id.
    """

    result = await map_external_category(
        session=session,
        category_name=category_name,
        categories=categories,
    )

    if result.category is None:
        return None

    return int(
        result.category.id
  )
