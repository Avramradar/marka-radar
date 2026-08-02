from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.product import Product
    from app.database.models.product_relation import ProductRelation
    from app.database.models.search_history import SearchHistory


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(128),
        unique=True,
        nullable=False,
        index=True,
    )

    normalized_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        index=True,
    )

    parent_id: Mapped[int | None] = mapped_column(
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
    )

    parent: Mapped["Category | None"] = relationship(
        remote_side="Category.id",
        back_populates="children",
    )

    children: Mapped[list["Category"]] = relationship(
        back_populates="parent",
        passive_deletes=True,
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="category",
        passive_deletes=True,
    )

    source_relations: Mapped[list["ProductRelation"]] = relationship(
        foreign_keys="ProductRelation.source_category_id",
        back_populates="source_category",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    target_relations: Mapped[list["ProductRelation"]] = relationship(
        foreign_keys="ProductRelation.target_category_id",
        back_populates="target_category",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    search_selections: Mapped[list["SearchHistory"]] = relationship(
        back_populates="selected_category",
        passive_deletes=True,
    )
