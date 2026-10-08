"""Product reviews (customer-written, moderated)."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntFK, IdMixin, TimestampMixin, enum_check, enum_column


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    HIDDEN = "hidden"


class Review(IdMixin, TimestampMixin, Base):
    __tablename__ = "reviews"

    product_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("products.id", ondelete="CASCADE"))
    user_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("users.id", ondelete="CASCADE"))
    # Set when the reviewer actually bought the product (verified purchase).
    order_item_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("order_items.id", ondelete="SET NULL")
    )
    rating: Mapped[int]
    title: Mapped[str | None] = mapped_column(String(120))
    body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReviewStatus] = mapped_column(
        enum_column(ReviewStatus, "review_status"),
        default=ReviewStatus.PENDING,
        server_default="pending",
    )

    __table_args__ = (
        enum_check("status", ReviewStatus),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating_range"),
        UniqueConstraint("product_id", "user_id", name="product_user"),
        Index("ix_reviews_product_id_status", "product_id", "status"),
    )
