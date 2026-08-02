from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.category import Category


class ProductRelation(Base):
    __tablename__ = "product_relations"

    __table_args__ = (
        UniqueConstraint(
            "source_category_id",
            "target_category_id",
            "target_subtype",
            name="uq_product_relation",
        ),
        CheckConstraint(
            "compatibility_score >= 0 "
            "AND compatibility_score <= 1",
            name="compatibility_score_0_1",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    source_category_id: Mapped[int] = mapped_column(
        ForeignKey(
            "categories.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    target_category_id: Mapped[int] = mapped_column(
        ForeignKey(
            "categories.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    target_subtype: Mapped[str] = mapped_column(
        String(128),
        default="",
        nullable=False,
        index=True,
    )

    compatibility_score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        nullable=False,
    )

    explanation: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    source_category: Mapped["Category"] = relationship(
        foreign_keys=[source_category_id],
        back_populates="source_relations",
    )

    target_category: Mapped["Category"] = relationship(
        foreign_keys=[target_category_id],
        back_populates="target_relations",
    )
