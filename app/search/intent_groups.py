import re
from collections import Counter
from typing import TypedDict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.brand import Brand
from app.database.models.category import Category
from app.database.models.product import Product
from app.database.repositories.product_repository import (
    search_products,
)
from app.utils.text import normalize_text


class IntentGroup(TypedDict):
    title: str
    query: str
    count: int


STOP_WORDS = {
    "и",
    "в",
    "во",
    "на",
    "с",
    "со",
    "из",
    "для",
    "по",
    "под",
    "без",
    "не",
    "или",
    "от",
    "до",
    "к",
    "у",
    "а",
    "я",
    "это",
    "продукт",
    "продукты",
    "товар",
    "товары",
    "бренд",
    "упаковка",
    "г",
    "гр",
    "кг",
    "мл",
    "л",
    "шт",
}


def clean_group_words(
    text: str,
    *,
    base_tokens: set[str],
) -> list[str]:
    """
    Выделяет полезные слова для уточняющей группы.
    """

    normalized = normalize_text(text)

    words = re.findall(
        r"[a-zа-я0-9]+",
        normalized,
    )

    result: list[str] = []

    for word in words:
        if len(word) < 3:
            continue

        if word in STOP_WORDS:
            continue

        if word in base_tokens:
            continue

        if word.isdigit():
            continue

        result.append(word)

    return result


def build_candidate_phrases(
    *,
    product_name: str,
    subtype: str | None,
    category_name: str,
    base_tokens: set[str],
) -> list[str]:
    """
    Формирует возможные уточняющие фразы товара.
    """

    phrases: list[str] = []

    name_words = clean_group_words(
        product_name,
        base_tokens=base_tokens,
    )

    subtype_words = clean_group_words(
        subtype or "",
        base_tokens=base_tokens,
    )

    category_words = clean_group_words(
        category_name,
        base_tokens=base_tokens,
    )

    for word in name_words:
        phrases.append(word)

    for word in subtype_words:
        phrases.append(word)

    for word in category_words:
        phrases.append(word)

    # Добавляем пары соседних слов.
    for words in (
        name_words,
        subtype_words,
        category_words,
    ):
        for index in range(len(words) - 1):
            phrase = (
                f"{words[index]} "
                f"{words[index + 1]}"
            )
            phrases.append(phrase)

    return phrases


def format_group_title(
    base_query: str,
    phrase: str,
) -> str:
    """
    Создаёт читаемый заголовок кнопки.

    Пример:
    base_query = "сельдь"
    phrase = "в масле"

    Результат:
    "Сельдь в масле"
    """

    normalized_base = normalize_text(base_query)
    normalized_phrase = normalize_text(phrase)

    if not normalized_phrase:
        return base_query.capitalize()

    title = (
        f"{normalized_base} "
        f"{normalized_phrase}"
    )

    return title.capitalize()


async def get_intent_groups(
    session: AsyncSession,
    query: str,
    *,
    limit: int = 8,
    products_limit: int = 100,
) -> list[IntentGroup]:
    """
    Строит уточняющие группы на основе найденных товаров.

    Например запрос:
    "сельдь"

    Может вернуть:
    - Сельдь атлантическая
    - Сельдь тихоокеанская
    - Сельдь в масле
    - Сельдь филе
    """

    normalized_query = normalize_text(query)

    if len(normalized_query) < 3:
        return []

    safe_limit = max(
        1,
        min(limit, 12),
    )

    safe_products_limit = max(
        safe_limit,
        min(products_limit, 200),
    )

    base_tokens = {
        token
        for token in normalized_query.split()
        if token
    }

    products = await search_products(
        session=session,
        query=query,
        limit=safe_products_limit,
    )

    if len(products) < 6:
        return []

    phrase_counter: Counter[str] = Counter()

    for product, _brand, category in products:
        phrases = build_candidate_phrases(
            product_name=product.name,
            subtype=product.subtype,
            category_name=category.name,
            base_tokens=base_tokens,
        )

        # Один товар должен учитываться
        # только один раз для каждой фразы.
        unique_phrases = set(phrases)

        for phrase in unique_phrases:
            phrase_counter[phrase] += 1

    groups: list[IntentGroup] = []

    for phrase, count in phrase_counter.most_common():
        # Слишком редкая группа создаёт шум.
        if count < 2:
            continue

        group_query = (
            f"{normalized_query} {phrase}"
        ).strip()

        groups.append(
            {
                "title": format_group_title(
                    normalized_query,
                    phrase,
                ),
                "query": group_query,
                "count": count,
            }
        )

        if len(groups) >= safe_limit:
            break

    return groups
