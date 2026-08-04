from app.utils.text import (
    build_search_variants,
    normalize_text,
)


def _split_words(text: str) -> list[str]:
    """
    Разбивает строку на отдельные слова.
    """

    return [
        word
        for word in normalize_text(text).split()
        if word
    ]


def build_search_index(
    *,
    name: str,
    brand: str,
    category: str,
    keywords: str | None = None,
) -> str:
    """
    Формирует единый поисковый индекс товара.

    Индекс включает:
    - название;
    - бренд;
    - категорию;
    - транслитерации;
    - исправленные раскладки;
    - ключевые слова;
    - отдельные слова.

    Результат хранится как одна строка.
    """

    values: list[str] = []

    def add(text: str | None) -> None:
        if not text:
            return

        normalized = normalize_text(text)

        if not normalized:
            return

        values.append(normalized)

        # Добавляем все варианты поиска
        for variant in build_search_variants(normalized):
            values.append(variant)

        # Добавляем отдельные слова
        values.extend(_split_words(normalized))

    add(name)
    add(brand)
    add(category)
    add(keywords)

    # Удаляем дубликаты, сохраняя порядок
    unique: list[str] = []
    seen: set[str] = set()

    for value in values:
        value = normalize_text(value)

        if not value:
            continue

        if value in seen:
            continue

        seen.add(value)
        unique.append(value)

    return " ".join(unique)
