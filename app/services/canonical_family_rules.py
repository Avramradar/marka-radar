import re
from dataclasses import dataclass

from app.utils.text import normalize_text


@dataclass(frozen=True)
class CanonicalFamilyRule:
    """
    Правило определения понятного семейства товара.

    Все слова из required должны присутствовать.
    Достаточно одного совпадения из optional.
    Ни одно слово из excluded не должно присутствовать.
    """

    family_name: str
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()


CANONICAL_FAMILY_RULES: tuple[
    CanonicalFamilyRule,
    ...
] = (
    # Молоко
    CanonicalFamilyRule(
        family_name="Молоко безлактозное",
        required=("молок",),
        optional=("безлактозн", "без лактоз"),
    ),
    CanonicalFamilyRule(
        family_name="Молоко ультрапастеризованное",
        required=("молок",),
        optional=(
            "ультрапастеризован",
            "ультра пастеризован",
            "uht",
        ),
    ),
    CanonicalFamilyRule(
        family_name="Молоко пастеризованное",
        required=("молок",),
        optional=("пастеризован",),
        excluded=("ультрапастеризован",),
    ),
    CanonicalFamilyRule(
        family_name="Молоко топлёное",
        required=("молок",),
        optional=("топлен", "топлён"),
    ),
    CanonicalFamilyRule(
        family_name="Молоко питьевое",
        required=("молок",),
        optional=("питьев",),
    ),
    CanonicalFamilyRule(
        family_name="Молоко козье",
        required=("молок",),
        optional=("козь",),
    ),
    CanonicalFamilyRule(
        family_name="Молоко овсяное",
        required=(),
        optional=("овсяное молоко", "овсяный напиток"),
    ),
    CanonicalFamilyRule(
        family_name="Молоко кокосовое",
        required=(),
        optional=("кокосовое молоко", "кокосовый напиток"),
    ),
    CanonicalFamilyRule(
        family_name="Молоко соевое",
        required=(),
        optional=("соевое молоко", "соевый напиток"),
    ),
    CanonicalFamilyRule(
        family_name="Молоко",
        required=("молок",),
    ),

    # Кофе
    CanonicalFamilyRule(
        family_name="Кофе растворимый",
        required=("коф",),
        optional=(
            "растворим",
            "instant coffee",
            "instant coffees",
        ),
    ),
    CanonicalFamilyRule(
        family_name="Кофе молотый",
        required=("коф",),
        optional=("молот", "ground coffee"),
    ),
    CanonicalFamilyRule(
        family_name="Кофе в зёрнах",
        required=("коф",),
        optional=(
            "в зернах",
            "в зернах",
            "зернов",
            "coffee beans",
        ),
    ),
    CanonicalFamilyRule(
        family_name="Кофе в капсулах",
        required=("коф",),
        optional=("капсул", "capsule"),
    ),
    CanonicalFamilyRule(
        family_name="Кофе сублимированный",
        required=("коф",),
        optional=("сублимирован", "freeze dried"),
    ),
    CanonicalFamilyRule(
        family_name="Кофе",
        required=("коф",),
    ),

    # Сельдь
    CanonicalFamilyRule(
        family_name="Сельдь в масле",
        required=("сельд",),
        optional=("в масле", "маслян"),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь слабосолёная",
        required=("сельд",),
        optional=(
            "слабосолен",
            "слабосолён",
            "малосолен",
            "малосолён",
        ),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь пряного посола",
        required=("сельд",),
        optional=("пряного посола", "пряный посол"),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь специального посола",
        required=("сельд",),
        optional=("специального посола",),
    ),
    CanonicalFamilyRule(
        family_name="Филе сельди",
        required=("сельд",),
        optional=("филе", "филейн"),
    ),
    CanonicalFamilyRule(
        family_name="Пресервы из сельди",
        required=("сельд",),
        optional=("пресерв",),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь с укропом",
        required=("сельд",),
        optional=("укроп",),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь в горчичном соусе",
        required=("сельд",),
        optional=("горчиц",),
    ),
    CanonicalFamilyRule(
        family_name="Сельдь",
        required=("сельд",),
    ),

    # Сыр
    CanonicalFamilyRule(
        family_name="Сыр плавленый",
        required=("сыр",),
        optional=("плавлен",),
    ),
    CanonicalFamilyRule(
        family_name="Сыр творожный",
        required=("сыр",),
        optional=("творожн",),
    ),
    CanonicalFamilyRule(
        family_name="Сыр твёрдый",
        required=("сыр",),
        optional=("тверд", "твёрд"),
    ),
    CanonicalFamilyRule(
        family_name="Сыр полутвёрдый",
        required=("сыр",),
        optional=("полутверд", "полутвёрд"),
    ),
    CanonicalFamilyRule(
        family_name="Сыр с голубой плесенью",
        required=("сыр",),
        optional=("голубой плесен", "голубой плесён"),
    ),
    CanonicalFamilyRule(
        family_name="Моцарелла",
        required=(),
        optional=("моцарелл",),
    ),
    CanonicalFamilyRule(
        family_name="Чеддер",
        required=(),
        optional=("чеддер",),
    ),
    CanonicalFamilyRule(
        family_name="Сыр",
        required=("сыр",),
    ),

    # Макароны
    CanonicalFamilyRule(
        family_name="Спагетти",
        required=(),
        optional=("спагетти", "spaghetti"),
    ),
    CanonicalFamilyRule(
        family_name="Макароны рожки",
        required=("макарон",),
        optional=("рожк",),
    ),
    CanonicalFamilyRule(
        family_name="Макароны перья",
        required=("макарон",),
        optional=("перья", "пенне", "penne"),
    ),
    CanonicalFamilyRule(
        family_name="Макароны бантики",
        required=("макарон",),
        optional=("бантик", "фарфалле", "farfalle"),
    ),
    CanonicalFamilyRule(
        family_name="Вермишель",
        required=(),
        optional=("вермишель",),
    ),
    CanonicalFamilyRule(
        family_name="Макароны",
        required=("макарон",),
    ),
)


TECHNICAL_PHRASES: tuple[str, ...] = (
    "массовая доля жира",
    "массовой долей жира",
    "массовой доли жира",
    "массовая доля",
    "массовой долей",
    "массовой доли",
    "пищевая продукция",
    "продукт пищевой",
    "готовый продукт",
    "товар",
    "продукт",
)


def prepare_family_source_text(
    *values: str | None,
) -> str:
    """
    Объединяет данные товара и удаляет технические фразы.
    """

    source = " ".join(
        value
        for value in values
        if value
    )

    normalized = normalize_text(source)

    for phrase in TECHNICAL_PHRASES:
        normalized_phrase = normalize_text(phrase)

        normalized = re.sub(
            rf"\b{re.escape(normalized_phrase)}\b",
            " ",
            normalized,
        )

    return " ".join(
        normalized.split()
    )


def _matches_part(
    source: str,
    part: str,
) -> bool:
    normalized_part = normalize_text(part)

    if not normalized_part:
        return False

    return normalized_part in source


def rule_matches(
    source: str,
    rule: CanonicalFamilyRule,
) -> bool:
    """
    Проверяет соответствие текста одному правилу.
    """

    if any(
        not _matches_part(source, required)
        for required in rule.required
    ):
        return False

    if any(
        _matches_part(source, excluded)
        for excluded in rule.excluded
    ):
        return False

    if rule.optional:
        return any(
            _matches_part(source, optional)
            for optional in rule.optional
        )

    return bool(rule.required)


def find_canonical_family_name(
    *,
    product_name: str,
    brand_name: str | None = None,
    category_name: str | None = None,
    subtype: str | None = None,
    keywords: str | None = None,
) -> str | None:
    """
    Возвращает короткое каноническое семейство товара.

    Важно: бренд передаётся только для полноты сигнатуры,
    но намеренно не используется в тексте поиска.
    Бренд является отдельным уровнем каталога.
    """

    source = prepare_family_source_text(
        product_name,
        category_name,
        subtype,
        keywords,
    )

    if not source:
        return None

    for rule in CANONICAL_FAMILY_RULES:
        if rule_matches(source, rule):
            return rule.family_name

    return None
