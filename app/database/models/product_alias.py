from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.product import Product


class ProductAlias(Base):
    __tablename__ = "product_aliases"

    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "alias",
            name="uq_product_alias",
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

    alias: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    normalized_alias: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    product: Mapped["Product"] = relationship(
        back_populates="aliases",
    )
