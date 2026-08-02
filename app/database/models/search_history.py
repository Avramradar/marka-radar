from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
)
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.category import Category
    from app.database.models.product import Product
    from app.database.models.user import User


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "users.telegram_id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    query_text: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    normalized_query: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    selected_product_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "products.id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    selected_category_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "categories.id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
        index=True,
    )

    user: Mapped["User"] = relationship(
        back_populates="search_history",
    )

    selected_product: Mapped["Product | None"] = relationship(
        back_populates="search_selections",
    )

    selected_category: Mapped["Category | None"] = relationship(
        back_populates="search_selections",
    )
