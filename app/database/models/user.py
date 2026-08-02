from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger
from sqlalchemy import DateTime
from sqlalchemy import String
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship

from app.database.base import Base


if TYPE_CHECKING:
    from app.database.models.rating import Rating
    from app.database.models.review import Review
    from app.database.models.search_history import SearchHistory


class User(Base):
    __tablename__ = "users"

    telegram_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    username: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    first_name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )

    last_name: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )

    language_code: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        nullable=False,
    )

    last_activity: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
        index=True,
    )

    ratings: Mapped[list["Rating"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    reviews: Mapped[list["Review"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    search_history: Mapped[list["SearchHistory"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
