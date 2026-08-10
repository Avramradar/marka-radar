from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.product import Product


class ProductSource(Base):
    """ Постоянная привязка внешней карточки к каноническому Product MarkaRadar. Ключевой принцип: один внешний объект (provider + source_id) всегда должен указывать на один и тот же Product. Пример: provider = "metro" source_id = "700g-pelmeni-..." product_id = 25659 После создания такой связи при следующем запросе нам больше не нужно заново угадывать товар по fuzzy-match. """

    __tablename__ = "product_sources"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "source_id",
            name=(
                "uq_product_sources_"
                "provider_source_id"
            ),
        ),
        Index(
            "ix_product_sources_product_provider",
            "product_id",
            "provider",
        ),
        Index(
            "ix_product_sources_provider_source",
            "provider",
            "source_id",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    product_id: Mapped[int] = mapped_column(
        ForeignKey(
            "products.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    provider: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
    )

    source_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    source_url: Mapped[str | None] = mapped_column(
        String(2048),
        nullable=True,
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

    product: Mapped["Product"] = relationship(
        back_populates="sources",
    )
