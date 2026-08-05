from dataclasses import dataclass, field


@dataclass(slots=True)
class ParsedQuery:
    """
    Полностью разобранный пользовательский запрос.

    Пример:

        Домик молоко 3.2 930 мл

    превращается в

        product_terms = ["молоко"]

        brand_terms = ["домик в деревне"]

        attributes = ["3.2"]

        package_value = 930

        package_unit = "мл"
    """

    # Исходный текст
    original: str

    # После исправления опечаток
    corrected: str

    # По каким словам искать товар
    product_terms: list[str] = field(
        default_factory=list
    )

    # Найденные бренды
    brand_terms: list[str] = field(
        default_factory=list
    )

    # Категории (пока пусто)
    category_terms: list[str] = field(
        default_factory=list
    )

    # "3.2", "безлактозное", "ультра"...
    attributes: list[str] = field(
        default_factory=list
    )

    # Размер упаковки
    package_value: float | None = None

    package_unit: str | None = None

    # Если пользователь ввёл штрихкод
    barcode: str | None = None

    # Исправлялась ли опечатка
    is_typo: bool = False

    # Уверенность парсера
    confidence: float = 1.0

    def has_brand(self) -> bool:
        return bool(
            self.brand_terms
        )

    def has_product(self) -> bool:
        return bool(
            self.product_terms
        )

    def has_package(self) -> bool:
        return (
            self.package_value is not None
        )

    def has_attributes(self) -> bool:
        return bool(
            self.attributes
        )

    def is_barcode(self) -> bool:
        return (
            self.barcode is not None
        )

    def is_empty(self) -> bool:
        return not (
            self.product_terms
            or self.brand_terms
            or self.category_terms
            or self.attributes
            or self.barcode
        )
