from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.category import Category
from app.utils.text import normalize_text


logger = logging.getLogger(__name__)


@dataclass( slots=True, frozen=True, )
class CategoryMappingResult:
    """ Результат определения категории MarkaRadar. category: Найденная категория из БД MarkaRadar. matched_by: Способ сопоставления. confidence: Условная уверенность 0..100. source_value: Внешнее значение, которое помогло определить категорию. """

    category: Category | None
    matched_by: str
    confidence: float
    source_value: str | None


# ------------------------------------------------------------------
# СЛОВАРЬ КАНОНИЧЕСКИХ ТИПОВ
# ------------------------------------------------------------------
#
# Ключ — логический тип товара.
#
# target_names — допустимые категории в БД MarkaRadar.
# Mapper идёт слева направо и берёт первую реально
# существующую категорию.
#
# aliases — слова и фразы, по которым распознаётся товар.
#
# Благодаря target_names система не ломается, если в БД нет
# отдельной категории "Сметана", но есть "Молочные продукты".
# ------------------------------------------------------------------

CATEGORY_RULES: dict[
    str,
    dict[str, tuple[str, ...]],
] = {
    # ------------------------- МОЛОЧНЫЕ -------------------------

    "сметана": {
        "target_names": (
            "сметана",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "сметана",
            "smetana",
            "sour cream",
            "сметанный продукт",
            "сметанный",
        ),
    },

    "молоко": {
        "target_names": (
            "молоко",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "молоко",
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
            "безлактозное молоко",
        ),
    },

    "кефир": {
        "target_names": (
            "кефир",
            "кисломолочные продукты",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "кефир",
            "kefir",
        ),
    },

    "йогурт": {
        "target_names": (
            "йогурт",
            "йогурты",
            "кисломолочные продукты",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "йогурт",
            "йогурты",
            "yogurt",
            "yoghurt",
            "yogurts",
            "yoghurts",
        ),
    },

    "творог": {
        "target_names": (
            "творог",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "творог",
            "творожный",
            "cottage cheese",
            "curd",
            "quark",
        ),
    },

    "сыр": {
        "target_names": (
            "сыр",
            "сыры",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "сыр",
            "сыры",
            "cheese",
            "cheeses",
            "hard cheese",
            "soft cheese",
            "processed cheese",
            "плавленый сыр",
            "твердый сыр",
            "твёрдый сыр",
        ),
    },

    "масло сливочное": {
        "target_names": (
            "масло",
            "масло сливочное",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "масло сливочное",
            "сливочное масло",
            "butter",
        ),
    },

    "сливки": {
        "target_names": (
            "сливки",
            "молочные продукты",
            "молочная продукция",
        ),
        "aliases": (
            "сливки",
            "cream",
            "drinking cream",
        ),
    },

    # ------------------------- НАПИТКИ -------------------------

    "кофе": {
        "target_names": (
            "кофе",
            "напитки",
            "бакалея",
        ),
        "aliases": (
            "кофе",
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
            "зерновой кофе",
        ),
    },

    "чай": {
        "target_names": (
            "чай",
            "напитки",
            "бакалея",
        ),
        "aliases": (
            "чай",
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
    },

    "вода": {
        "target_names": (
            "вода",
            "напитки",
        ),
        "aliases": (
            "вода",
            "water",
            "waters",
            "mineral water",
            "drinking water",
            "sparkling water",
            "still water",
            "минеральная вода",
            "питьевая вода",
            "газированная вода",
            "негазированная вода",
        ),
    },

    "сок": {
        "target_names": (
            "сок",
            "соки",
            "напитки",
        ),
        "aliases": (
            "сок",
            "соки",
            "juice",
            "juices",
            "fruit juice",
            "vegetable juice",
            "нектар",
            "морс",
        ),
    },

    "газированные напитки": {
        "target_names": (
            "газированные напитки",
            "напитки",
        ),
        "aliases": (
            "газированный напиток",
            "газированные напитки",
            "лимонад",
            "cola",
            "soda",
            "soft drink",
            "soft drinks",
        ),
    },

    # ------------------------- РЫБА / МЯСО -------------------------

    "сельдь": {
        "target_names": (
            "сельдь",
            "рыба",
            "рыба и морепродукты",
        ),
        "aliases": (
            "сельдь",
            "селедка",
            "селёдка",
            "herring",
            "herrings",
            "salted herring",
            "herring fillet",
            "herring fillets",
            "сельдь филе",
            "филе сельди",
        ),
    },

    "рыба": {
        "target_names": (
            "рыба",
            "рыба и морепродукты",
        ),
        "aliases": (
            "рыба",
            "fish",
            "salmon",
            "лосось",
            "семга",
            "сёмга",
            "форель",
            "скумбрия",
            "тунец",
            "горбуша",
            "минтай",
            "треска",
        ),
    },

    "морепродукты": {
        "target_names": (
            "морепродукты",
            "рыба и морепродукты",
        ),
        "aliases": (
            "морепродукты",
            "seafood",
            "креветки",
            "shrimp",
            "prawns",
            "кальмар",
            "мидии",
        ),
    },

    "колбаса": {
        "target_names": (
            "колбаса",
            "колбасы",
            "мясо и колбасы",
            "мясные продукты",
        ),
        "aliases": (
            "колбаса",
            "колбасы",
            "sausage",
            "sausages",
            "сервелат",
            "салями",
            "ветчина",
        ),
    },

    "мясо": {
        "target_names": (
            "мясо",
            "мясо и колбасы",
            "мясные продукты",
        ),
        "aliases": (
            "мясо",
            "meat",
            "говядина",
            "свинина",
            "баранина",
            "телятина",
            "beef",
            "pork",
            "lamb",
        ),
    },

    "птица": {
        "target_names": (
            "птица",
            "мясо",
            "мясо и колбасы",
        ),
        "aliases": (
            "курица",
            "куриный",
            "индейка",
            "утка",
            "chicken",
            "turkey",
            "duck",
        ),
    },

    # ------------------------- ЗАМОРОЗКА / ГОТОВАЯ ЕДА -------------------------

    "пицца": {
        "target_names": (
            "пицца",
            "замороженные продукты",
            "готовая еда",
        ),
        "aliases": (
            "пицца",
            "pizza",
            "pizzas",
            "frozen pizza",
            "fresh pizza",
            "пицца замороженная",
            "замороженная пицца",
        ),
    },

    "пельмени": {
        "target_names": (
            "пельмени",
            "замороженные продукты",
            "полуфабрикаты",
        ),
        "aliases": (
            "пельмени",
            "pelmeni",
            "dumplings",
        ),
    },

    "вареники": {
        "target_names": (
            "вареники",
            "замороженные продукты",
            "полуфабрикаты",
        ),
        "aliases": (
            "вареники",
            "vareniki",
        ),
    },

    "мороженое": {
        "target_names": (
            "мороженое",
            "замороженные продукты",
        ),
        "aliases": (
            "мороженое",
            "ice cream",
            "пломбир",
            "эскимо",
        ),
    },

    # ------------------------- БАКАЛЕЯ -------------------------

    "макароны": {
        "target_names": (
            "макароны",
            "макаронные изделия",
            "бакалея",
        ),
        "aliases": (
            "макароны",
            "макаронные изделия",
            "pasta",
            "spaghetti",
            "спагетти",
            "лапша",
        ),
    },

    "рис": {
        "target_names": (
            "рис",
            "крупы",
            "бакалея",
        ),
        "aliases": (
            "рис",
            "rice",
        ),
    },

    "гречка": {
        "target_names": (
            "гречка",
            "крупы",
            "бакалея",
        ),
        "aliases": (
            "гречка",
            "гречневая крупа",
            "buckwheat",
        ),
    },

    "крупы": {
        "target_names": (
            "крупы",
            "бакалея",
        ),
        "aliases": (
            "крупа",
            "крупы",
            "groats",
            "cereal grain",
            "булгур",
            "кус кус",
            "кускус",
            "перловка",
            "пшено",
        ),
    },

    "мука": {
        "target_names": (
            "мука",
            "бакалея",
        ),
        "aliases": (
            "мука",
            "flour",
        ),
    },

    "сахар": {
        "target_names": (
            "сахар",
            "бакалея",
        ),
        "aliases": (
            "сахар",
            "sugar",
        ),
    },

    "соль": {
        "target_names": (
            "соль",
            "бакалея",
        ),
        "aliases": (
            "соль",
            "salt",
        ),
    },

    "растительное масло": {
        "target_names": (
            "растительное масло",
            "масло растительное",
            "бакалея",
        ),
        "aliases": (
            "подсолнечное масло",
            "масло подсолнечное",
            "оливковое масло",
            "масло оливковое",
            "vegetable oil",
            "sunflower oil",
            "olive oil",
        ),
    },

    # ------------------------- КОНСЕРВЫ / СОУСЫ -------------------------

    "консервы": {
        "target_names": (
            "консервы",
            "бакалея",
        ),
        "aliases": (
            "консервы",
            "консервированный",
            "canned",
            "canned food",
        ),
    },

    "соус": {
        "target_names": (
            "соусы",
            "соус",
            "бакалея",
        ),
        "aliases": (
            "соус",
            "соусы",
            "sauce",
            "sauces",
            "кетчуп",
            "ketchup",
            "майонез",
            "mayonnaise",
            "горчица",
            "mustard",
        ),
    },

    # ------------------------- ХЛЕБ / СЛАДОСТИ -------------------------

    "хлеб": {
        "target_names": (
            "хлеб",
            "хлебобулочные изделия",
            "выпечка",
        ),
        "aliases": (
            "хлеб",
            "bread",
            "батон",
            "булка",
            "багет",
        ),
    },

    "печенье": {
        "target_names": (
            "печенье",
            "кондитерские изделия",
            "сладости",
        ),
        "aliases": (
            "печенье",
            "cookie",
            "cookies",
            "biscuit",
            "biscuits",
        ),
    },

    "шоколад": {
        "target_names": (
            "шоколад",
            "кондитерские изделия",
            "сладости",
        ),
        "aliases": (
            "шоколад",
            "chocolate",
        ),
    },

    "конфеты": {
        "target_names": (
            "конфеты",
            "кондитерские изделия",
            "сладости",
        ),
        "aliases": (
            "конфеты",
            "candy",
            "candies",
            "sweets",
        ),
    },

    # ------------------------- ОВОЩИ / ФРУКТЫ -------------------------

    "овощи": {
        "target_names": (
            "овощи",
            "овощи и фрукты",
        ),
        "aliases": (
            "овощи",
            "vegetables",
            "томат",
            "помидор",
            "огурец",
            "картофель",
            "морковь",
            "лук",
            "капуста",
            "перец",
        ),
    },

    "фрукты": {
        "target_names": (
            "фрукты",
            "овощи и фрукты",
        ),
        "aliases": (
            "фрукты",
            "fruit",
            "fruits",
            "яблоко",
            "банан",
            "апельсин",
            "мандарин",
            "лимон",
            "груша",
            "виноград",
        ),
    },

    # ------------------------- ЯЙЦА -------------------------

    "яйца": {
        "target_names": (
            "яйца",
            "яйцо",
            "молочные продукты",
        ),
        "aliases": (
            "яйца",
            "яйцо",
            "egg",
            "eggs",
            "яйцо куриное",
            "яйца куриные",
        ),
    },

    # ------------------------- ДЕТСКОЕ / ПРОЧЕЕ -------------------------

    "детское питание": {
        "target_names": (
            "детское питание",
            "продукты",
        ),
        "aliases": (
            "детское питание",
            "baby food",
            "детская смесь",
            "молочная смесь",
            "пюре детское",
        ),
    },
}


# Категории, которые слишком общие, чтобы использовать их
# как сильный сигнал при автоматическом определении.
GENERIC_EXTERNAL_CATEGORY_NAMES = {
    "",
    "продукты",
    "продукт",
    "food",
    "foods",
    "product",
    "products",
    "еда",
    "каталог",
    "товары",
    "прочее",
    "другое",
    "other",
}


def clean_category_text( value: str | None, ) -> str:
    """ Убирает лишние пробелы. """

    if not value:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def normalize_category_text( value: str | None, ) -> str:
    """ Нормализует текст внешней категории. """

    cleaned = clean_category_text(
        value
    )

    if not cleaned:
        return ""

    normalized_value = (
        normalize_text(
            cleaned
        )
        .replace(
            "ё",
            "е",
        )
        .replace(
            "_",
            " ",
        )
        .replace(
            "-",
            " ",
        )
        .replace(
            "/",
            " ",
        )
        .replace(
            "\\",
            " ",
        )
        .strip()
    )

    normalized_value = re.sub(
        r"^(?:ru|en|de|fr|es|it):",
        "",
        normalized_value,
    )

    return " ".join(
        normalized_value.split()
    )


def normalize_external_categories( categories: Iterable[str], ) -> list[str]:
    """ Очищает внешние подсказки и удаляет дубли. """

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


def _normalized_words( value: str, ) -> set[str]:
    """ Возвращает слова строки. Нужна для безопасного совпадения: например "чай" не должен находиться внутри случайного длинного слова. """

    return {
        token
        for token in re.findall(
            r"[a-zа-я0-9]+",
            normalize_category_text(
                value
            ),
            flags=re.IGNORECASE,
        )
        if token
    }


async def find_category_exact( *, session: AsyncSession, value: str, ) -> Category | None:
    """ Точное совпадение с категорией БД. """

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
                    func.trim(
                        Category.name
                    )
                )
                == normalized_value,
            )
        )
        .order_by(
            Category.id.asc()
        )
        .limit(
            1
        )
    )

    return result.scalar_one_or_none()


async def find_first_existing_category( *, session: AsyncSession, names: Iterable[str], ) -> Category | None:
    """ Берёт первую реально существующую категорию из списка fallback-категорий. Например для сметаны: сметана ↓ если такой категории нет молочные продукты ↓ молочная продукция """

    for name in names:
        category = await find_category_exact(
            session=session,
            value=name,
        )

        if category is not None:
            return category

    return None


def detect_rule_exact( value: str, ) -> str | None:
    """ Точное совпадение с canonical key или alias. """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    for (
        rule_name,
        rule,
    ) in CATEGORY_RULES.items():
        terms = (
            rule_name,
            *rule["aliases"],
        )

        for term in terms:
            if (
                normalized_value
                == normalize_category_text(
                    term
                )
            ):
                return rule_name

    return None


def detect_rule_contains( value: str, ) -> str | None:
    """ Ищет тип продукта внутри длинной строки. Более длинные aliases получают преимущество. """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    words = _normalized_words(
        normalized_value
    )

    matches: list[
        tuple[
            int,
            str,
        ]
    ] = []

    for (
        rule_name,
        rule,
    ) in CATEGORY_RULES.items():
        terms = (
            rule_name,
            *rule["aliases"],
        )

        for term in terms:
            normalized_term = (
                normalize_category_text(
                    term
                )
            )

            if not normalized_term:
                continue

            term_words = _normalized_words(
                normalized_term
            )

            if not term_words:
                continue

            # Все слова alias должны присутствовать.
            if term_words <= words:
                matches.append(
                    (
                        len(
                            normalized_term
                        ),
                        rule_name,
                    )
                )

    if not matches:
        return None

    matches.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return matches[0][1]


async def resolve_rule_category( *, session: AsyncSession, rule_name: str, ) -> Category | None:
    """ Преобразует логический тип продукта в реально существующую категорию БД. """

    rule = CATEGORY_RULES.get(
        rule_name
    )

    if not rule:
        return None

    return await find_first_existing_category(
        session=session,
        names=rule["target_names"],
    )


async def find_dynamic_database_category( *, session: AsyncSession, value: str, ) -> Category | None:
    """ Последний осторожный fallback. Проверяет, содержится ли название уже существующей категории БД в длинной строке. Это помогает новым категориям работать даже до добавления отдельного alias. Пример: в БД есть категория "Авокадо" внешний товар называется "Авокадо Hass 2 шт" Mapper сможет сопоставить его автоматически. Очень короткие и общие категории игнорируются. """

    normalized_value = (
        normalize_category_text(
            value
        )
    )

    if not normalized_value:
        return None

    value_words = _normalized_words(
        normalized_value
    )

    if not value_words:
        return None

    result = await session.execute(
        select(
            Category
        )
        .order_by(
            func.length(
                Category.name
            ).desc(),
            Category.id.asc(),
        )
    )

    categories = list(
        result.scalars().all()
    )

    candidates: list[
        tuple[
            int,
            Category,
        ]
    ] = []

    for category in categories:
        category_name = (
            normalize_category_text(
                category.name
            )
        )

        if not category_name:
            continue

        if (
            category_name
            in GENERIC_EXTERNAL_CATEGORY_NAMES
        ):
            continue

        category_words = (
            _normalized_words(
                category_name
            )
        )

        if not category_words:
            continue

        # Не используем слишком короткие
        # односимвольные/двухсимвольные категории.
        if (
            len(category_words) == 1
            and len(category_name) < 3
        ):
            continue

        if category_words <= value_words:
            candidates.append(
                (
                    len(category_name),
                    category,
                )
            )

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return candidates[0][1]


async def map_external_category( *, session: AsyncSession, category_name: str | None = None, categories: Iterable[str] | None = None, ) -> CategoryMappingResult:
    """ Главная функция Category Mapper. Приоритет: 1. точное совпадение с категорией БД; 2. точный controlled alias; 3. controlled alias внутри длинного текста; 4. динамическое совпадение с существующей категорией БД; 5. None. Важное правило: Mapper НЕ создаёт новую категорию автоматически. Это защищает базу от мусора вроде: "Скидки" "Новинки" "Лучшее" "Акция" "500 г" """

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

    # ----------------------------------------------------------
    # 1. Точное совпадение с БД.
    # ----------------------------------------------------------

    for value in incoming_values:
        category = await find_category_exact(
            session=session,
            value=value,
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by="database_exact",
                confidence=100.0,
                source_value=value,
            )

    # ----------------------------------------------------------
    # 2. Точный controlled alias.
    # ----------------------------------------------------------

    for value in incoming_values:
        rule_name = detect_rule_exact(
            value
        )

        if rule_name is None:
            continue

        category = await resolve_rule_category(
            session=session,
            rule_name=rule_name,
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by=(
                    f"rule_exact:{rule_name}"
                ),
                confidence=97.0,
                source_value=value,
            )

    # ----------------------------------------------------------
    # 3. Alias внутри названия товара / длинной категории.
    # ----------------------------------------------------------

    for value in incoming_values:
        rule_name = detect_rule_contains(
            value
        )

        if rule_name is None:
            continue

        category = await resolve_rule_category(
            session=session,
            rule_name=rule_name,
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by=(
                    f"rule_contains:{rule_name}"
                ),
                confidence=90.0,
                source_value=value,
            )

    # ----------------------------------------------------------
    # 4. Новые категории без ручного alias.
    #
    # Если название существующей категории БД
    # явно присутствует в названии товара,
    # используем её.
    # ----------------------------------------------------------

    for value in incoming_values:
        category = (
            await find_dynamic_database_category(
                session=session,
                value=value,
            )
        )

        if category is not None:
            return CategoryMappingResult(
                category=category,
                matched_by="database_contains",
                confidence=82.0,
                source_value=value,
            )

    logger.info(
        "Category mapper unresolved: values=%r",
        incoming_values,
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


async def map_external_category_id( *, session: AsyncSession, category_name: str | None = None, categories: Iterable[str] | None = None, ) -> int | None:
    """ Сокращённая точка входа: возвращает только category_id. """

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
