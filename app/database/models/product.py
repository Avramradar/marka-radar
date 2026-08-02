from datetime import datetime

from sqlalchemy import Boolean
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.database.base import Base


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        index=True,
    )

    normalized_name: Mapped[str] = mapped_column(
        String(255),
        index=True,
    )

    brand_id: Mapped[int] = mapped_column(
        ForeignKey("brands.id", ondelete="RESTRICT"),
        index=True,
    )

    category_id: Mapped[int] = mapped_column(
        ForeignKey("categories.id", ondelete="RESTRICT"),
        index=True,
    )

    barcode: Mapped[str | None] = mapped_column(
        String(32),
        unique=True,
        nullable=True,
        index=True,
    )

    package_value: Mapped[float | None] = mapped_column(
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

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
