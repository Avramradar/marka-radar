from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.brand import Brand
    from app.database.models.category import Category
    from app.database.models.price import PriceObservation
    from app.database.models.product_alias import ProductAlias
    from app.database.models.rating import Rating
    from app.database.models.review import Review
    from app.database.models.search_history import SearchHistory


class Product(Base):
    __tablename__ = "products"

    __table_args__ = (
        Index(
            "ix_products_brand_category",
            "brand_id",
            "category_id",
        ),
        Index(
            "ix_products_active_normalized_name",
            "is_active",
            "normalized_name",
        ),
        Index(
            "ix_products_search_text",
            "search_text",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    normalized_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    brand_id: Mapped[int] = mapped_column(
        ForeignKey(
            "brands.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    category_id: Mapped[int] = mapped_column(
        ForeignKey(
            "categories.id",
            ondelete="RESTRICT",
        ),
        nullable=False,
        index=True,
    )

    barcode: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
        nullable=True,
        index=True,
    )

    package_value: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3),
        nullable=True,
    )

    package_unit: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    subtype: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        index=True,
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    image_url: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    keywords: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    search_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        index=True,
    )

    brand: Mapped["Brand"] = relationship(
        back_populates="products",
    )

    category: Mapped["Category"] = relationship(
        back_populates="products",
    )

    ratings: Mapped[list["Rating"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    reviews: Mapped[list["Review"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    prices: Mapped[list["PriceObservation"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    aliases: Mapped[list["ProductAlias"]] = relationship(
        back_populates="product",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    search_selections: Mapped[list["SearchHistory"]] = relationship(
        back_populates="selected_product",
        passive_deletes=True,
    )
