from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


UNKNOWN_BRAND_NAMES = {
    "",
    "бренд не указан",
    "не указан",
    "unknown",
    "no brand",
    "без бренда",
}


GENERIC_CATEGORY_NAMES = {
    "",
    "продукты",
    "продукт",
    "еда",
    "food",
    "foods",
    "products",
    "product",
    "прочее",
    "другое",
    "other",
}


GENERIC_PRODUCT_NAMES = {
    "",
    "кофе",
    "молоко",
    "чай",
    "вода",
    "пицца",
    "сыр",
    "масло",
    "йогурт",
    "кефир",
    "сельдь",
    "сок",
    "сметана",
    "творог",
    "сливки",
    "колбаса",
    "майонез",
    "паштет",
    "печенье",
    "шоколад",
    "мороженое",
}


PLACEHOLDER_IMAGE_MARKERS = (
    "placeholder",
    "no-image",
    "no_image",
    "default-product",
    "default_product",
    "image-not-found",
    "image_not_found",
    "stub",
)


BARCODE_PATTERN = re.compile(
    r"^\d{8,14}$"
)


@dataclass( slots=True, frozen=True, )
class ProductCompletenessResult:
    """ Единая оценка полноты карточки MarkaRadar. score: Общая полнота карточки 0..100. identity_score: Насколько хорошо товар идентифицирован. Здесь особенно важны: barcode, brand, name, package. presentation_score: Насколько карточка готова к показу пользователю: image, description, category и т.д. missing_fields: Поля, которых реально нет. weak_fields: Поля есть, но качество недостаточное. critical_missing_fields: Поля, без которых карточка не считается полноценной для обычного показа. next_priority_fields: В каком порядке Enrichment Orchestrator должен пытаться улучшать карточку. is_complete: Можно ли остановить дальнейшее обогащение карточки. """

    score: float
    identity_score: float
    presentation_score: float

    missing_fields: tuple[
        str,
        ...
    ]

    weak_fields: tuple[
        str,
        ...
    ]

    critical_missing_fields: tuple[
        str,
        ...
    ]

    next_priority_fields: tuple[
        str,
        ...
    ]

    is_complete: bool


def clean_text( value: Any, ) -> str:
    """ Убирает лишние пробелы. """

    if value is None:
        return ""

    return " ".join(
        str(value)
        .strip()
        .split()
    )


def normalized( value: Any, ) -> str:
    """ Простая безопасная нормализация для внутренних проверок. """

    return (
        clean_text(
            value
        )
        .lower()
        .replace(
            "ё",
            "е",
        )
    )


def normalize_barcode( value: Any, ) -> str | None:
    """ Нормализует EAN/GTIN. Внутренние артикулы магазинов не должны случайно считаться штрихкодами. """

    if value is None:
        return None

    digits = "".join(
        char
        for char in str(value)
        if char.isdigit()
    )

    if not BARCODE_PATTERN.fullmatch(
        digits
    ):
        return None

    return digits


def normalize_package_value( value: Any, ) -> Decimal | None:
    """ Проверяет значение упаковки. """

    if value is None:
        return None

    try:
        decimal_value = Decimal(
            str(value)
        )
    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):
        return None

    if decimal_value <= 0:
        return None

    return decimal_value


def normalize_package_unit( value: Any, ) -> str | None:
    """ Нормализует единицу упаковки. """

    unit = normalized(
        value
    )

    if not unit:
        return None

    aliases = {
        "гр": "г",
        "грамм": "г",
        "грамма": "г",
        "граммов": "г",
        "g": "г",
        "gram": "г",
        "grams": "г",

        "кг": "кг",
        "kg": "кг",
        "килограмм": "кг",
        "килограмма": "кг",
        "килограммов": "кг",

        "мл": "мл",
        "ml": "мл",
        "миллилитр": "мл",
        "миллилитра": "мл",
        "миллилитров": "мл",

        "л": "л",
        "l": "л",
        "литр": "л",
        "литра": "л",
        "литров": "л",
    }

    return aliases.get(
        unit,
        unit,
    )


def is_real_brand( value: Any, ) -> bool:
    """ Проверяет, что бренд не служебный. """

    brand_name = normalized(
        value
    )

    if not brand_name:
        return False

    return (
        brand_name
        not in {
            normalized(
                item
            )
            for item
            in UNKNOWN_BRAND_NAMES
        }
    )


def is_specific_category( value: Any, ) -> bool:
    """ Проверяет, что категория не слишком общая. """

    category_name = normalized(
        value
    )

    if not category_name:
        return False

    return (
        category_name
        not in {
            normalized(
                item
            )
            for item
            in GENERIC_CATEGORY_NAMES
        }
    )


def is_specific_product_name( value: Any, ) -> bool:
    """ Проверяет информативность названия. """

    product_name = normalized(
        value
    )

    if not product_name:
        return False

    if (
        product_name
        in {
            normalized(
                item
            )
            for item
            in GENERIC_PRODUCT_NAMES
        }
    ):
        return False

    tokens = [
        token
        for token in re.findall(
            r"[a-zа-я0-9]+",
            product_name,
            flags=re.IGNORECASE,
        )
        if token
    ]

    return (
        len(tokens) >= 2
        or len(product_name) >= 12
    )


def has_valid_package( *, value: Any, unit: Any, ) -> bool:
    """ Полная упаковка = есть и размер, и единица измерения. """

    return (
        normalize_package_value(
            value
        )
        is not None
        and bool(
            normalize_package_unit(
                unit
            )
        )
    )


def image_quality_score(
    value: Any,
) -> float:
    """
    Предварительно оценивает изображение
    без сетевого запроса.

    Важно:
    наличие URL ещё не означает,
    что изображение реально доступно.

    Поэтому внешний HTTP/HTTPS URL
    получает только предварительный балл.
    Окончательную проверку доступности
    выполняет отдельный Image Validator.
    """

    image = clean_text(
        value
    )

    if not image:
        return 0.0

    normalized_image = normalized(
        image
    )

    if any(
        marker in normalized_image
        for marker
        in PLACEHOLDER_IMAGE_MARKERS
    ):
        return 10.0

    if image.startswith(
        (
            "https://",
            "http://",
        )
    ):
        # URL существует только как значение
        # в базе. Мы ещё не знаем:
        #
        # - отвечает ли сервер;
        # - действительно ли это изображение;
        # - не возвращается ли 403/404;
        # - принимает ли URL Telegram;
        # - не является ли картинка заглушкой.
        #
        # Поэтому такой URL пока нельзя
        # считать подтверждённым изображением.
        return 40.0

    # Внутренний идентификатор изображения
    # (например, уже сохранённый Telegram file_id)
    # считаем значительно надёжнее внешнего URL.
    return 90.0


def description_quality_score( value: Any, ) -> float:
    """ Оценивает полезность описания. """

    description = clean_text(
        value
    )

    if not description:
        return 0.0

    length = len(
        description
    )

    if length < 20:
        return 25.0

    if length < 60:
        return 55.0

    if length < 140:
        return 80.0

    return 100.0


def evaluate_product_completeness( *, product: Any, brand: Any = None, category: Any = None, ) -> ProductCompletenessResult:
    """ Главная точка оценки полноты карточки. Важно: функция ничего не изменяет в БД. Она только оценивает текущее состояние. Максимальный score = 100. Вес полей: name 15 brand 12 category 10 package 15 image 15 description 12 barcode 12 subtype 4 keywords 5 --- 100 """

    missing_fields: list[
        str
    ] = []

    weak_fields: list[
        str
    ] = []

    critical_missing_fields: list[
        str
    ] = []

    score = 0.0

    product_name = clean_text(
        getattr(
            product,
            "name",
            None,
        )
    )

    brand_name = clean_text(
        getattr(
            brand,
            "name",
            None,
        )
    )

    category_name = clean_text(
        getattr(
            category,
            "name",
            None,
        )
    )

    barcode = normalize_barcode(
        getattr(
            product,
            "barcode",
            None,
        )
    )

    package_value = getattr(
        product,
        "package_value",
        None,
    )

    package_unit = getattr(
        product,
        "package_unit",
        None,
    )

    image_url = getattr(
        product,
        "image_url",
        None,
    )

    description = getattr(
        product,
        "description",
        None,
    )

    subtype = clean_text(
        getattr(
            product,
            "subtype",
            None,
        )
    )

    keywords = clean_text(
        getattr(
            product,
            "keywords",
            None,
        )
    )

    #
    # NAME — 15
    #

    if not product_name:
        missing_fields.append(
            "name"
        )
        critical_missing_fields.append(
            "name"
        )

    elif is_specific_product_name(
        product_name
    ):
        score += 15.0

    else:
        score += 6.0
        weak_fields.append(
            "name"
        )

    #
    # BRAND — 12
    #

    if not brand_name:
        missing_fields.append(
            "brand"
        )
        critical_missing_fields.append(
            "brand"
        )

    elif is_real_brand(
        brand_name
    ):
        score += 12.0

    else:
        score += 2.0
        weak_fields.append(
            "brand"
        )
        critical_missing_fields.append(
            "brand"
        )

    #
    # CATEGORY — 10
    #

    if not category_name:
        missing_fields.append(
            "category"
        )
        critical_missing_fields.append(
            "category"
        )

    elif is_specific_category(
        category_name
    ):
        score += 10.0

    else:
        score += 3.0
        weak_fields.append(
            "category"
        )
        critical_missing_fields.append(
            "category"
        )

    #
    # PACKAGE — 15
    #

    has_package_value = (
        normalize_package_value(
            package_value
        )
        is not None
    )

    has_package_unit = bool(
        normalize_package_unit(
            package_unit
        )
    )

    if has_valid_package(
        value=package_value,
        unit=package_unit,
    ):
        score += 15.0

    elif (
        has_package_value
        or has_package_unit
    ):
        score += 5.0
        weak_fields.append(
            "package"
        )
        critical_missing_fields.append(
            "package"
        )

    else:
        missing_fields.append(
            "package"
        )
        critical_missing_fields.append(
            "package"
        )

    #
    # IMAGE — 15
    #

    image_score = image_quality_score(
        image_url
    )

    if image_score <= 0:
        missing_fields.append(
            "image_url"
        )
        critical_missing_fields.append(
            "image_url"
        )

    elif image_score < 50:
        score += 3.0
        weak_fields.append(
            "image_url"
        )
        critical_missing_fields.append(
            "image_url"
        )

    else:
        score += (
            15.0
            * image_score
            / 100.0
        )

    #
    # DESCRIPTION — 12
    #

    description_score = (
        description_quality_score(
            description
        )
    )

    if description_score <= 0:
        missing_fields.append(
            "description"
        )

    elif description_score < 60:
        score += (
            12.0
            * description_score
            / 100.0
        )
        weak_fields.append(
            "description"
        )

    else:
        score += (
            12.0
            * description_score
            / 100.0
        )

    #
    # BARCODE — 12
    #
    # Важный идентификатор, но отсутствие
    # barcode не запрещает показать карточку.
    #

    raw_barcode = clean_text(
        getattr(
            product,
            "barcode",
            None,
        )
    )

    if barcode:
        score += 12.0

    elif raw_barcode:
        score += 2.0
        weak_fields.append(
            "barcode"
        )

    else:
        missing_fields.append(
            "barcode"
        )

    #
    # SUBTYPE — 4
    #

    if subtype:
        score += 4.0
    else:
        missing_fields.append(
            "subtype"
        )

    #
    # KEYWORDS — 5
    #

    if keywords:
        score += 5.0
    else:
        missing_fields.append(
            "keywords"
        )

    #
    # IDENTITY SCORE
    #
    # Насколько уверенно мы понимаем,
    # что это конкретный SKU.
    #

    identity_score = 0.0

    if barcode:
        identity_score += 45.0

    if is_real_brand(
        brand_name
    ):
        identity_score += 20.0

    if is_specific_product_name(
        product_name
    ):
        identity_score += 20.0

    if has_valid_package(
        value=package_value,
        unit=package_unit,
    ):
        identity_score += 15.0

    identity_score = min(
        identity_score,
        100.0,
    )

    #
    # PRESENTATION SCORE
    #
    # Насколько карточка уже выглядит
    # законченной для пользователя.
    #

    presentation_points = 0.0

    if product_name:
        presentation_points += 20.0

    if is_real_brand(
        brand_name
    ):
        presentation_points += 15.0

    if is_specific_category(
        category_name
    ):
        presentation_points += 15.0

    if has_valid_package(
        value=package_value,
        unit=package_unit,
    ):
        presentation_points += 15.0

    presentation_points += (
        20.0
        * image_score
        / 100.0
    )

    presentation_points += (
        15.0
        * description_score
        / 100.0
    )

    presentation_score = min(
        presentation_points,
        100.0,
    )

    #
    # ПРИОРИТЕТ ОБОГАЩЕНИЯ
    #
    # Не просто список пропусков:
    # здесь именно порядок обращения
    # к следующим источникам.
    #

    priority_order = (
        "brand",
        "name",
        "package",
        "category",
        "image_url",
        "barcode",
        "description",
        "subtype",
        "keywords",
    )

    problem_fields = set(
        missing_fields
    ) | set(
        weak_fields
    )

    next_priority_fields = tuple(
        field
        for field
        in priority_order
        if field in problem_fields
    )

    #
    # COMPLETE
    #
    # Карточка считается полноценной если:
    #
    # 1. нет критических дыр;
    # 2. общий score >= 85;
    # 3. identity >= 55;
    # 4. presentation >= 80.
    #
    # Barcode полезен, но не является
    # абсолютным обязательным условием.
    #

    unique_critical_missing = tuple(
        dict.fromkeys(
            critical_missing_fields
        )
    )

    is_complete = (
        not unique_critical_missing
        and score >= 85.0
        and identity_score >= 55.0
        and presentation_score >= 80.0
    )

    return ProductCompletenessResult(
        score=round(
            min(
                score,
                100.0,
            ),
            1,
        ),
        identity_score=round(
            identity_score,
            1,
        ),
        presentation_score=round(
            presentation_score,
            1,
        ),
        missing_fields=tuple(
            dict.fromkeys(
                missing_fields
            )
        ),
        weak_fields=tuple(
            dict.fromkeys(
                weak_fields
            )
        ),
        critical_missing_fields=(
            unique_critical_missing
        ),
        next_priority_fields=(
            next_priority_fields
        ),
        is_complete=is_complete,
    )
