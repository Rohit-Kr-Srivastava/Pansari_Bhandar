"""Shopping cart and wishlist."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntFK, IdMixin, TimestampMixin, enum_check, enum_column


class CartStatus(StrEnum):
    ACTIVE = "active"
    CONVERTED = "converted"
    ABANDONED = "abandoned"


class Cart(IdMixin, TimestampMixin, Base):
    """A logged-in customer's cart (user_id) or a guest cart (session_token)."""

    __tablename__ = "carts"

    user_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="CASCADE")
    )
    session_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    status: Mapped[CartStatus] = mapped_column(
        enum_column(CartStatus, "cart_status"),
        default=CartStatus.ACTIVE,
        server_default="active",
    )

    items: Mapped[list[CartItem]] = relationship(
        back_populates="cart", cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_check("status", CartStatus),
        CheckConstraint("user_id IS NOT NULL OR session_token IS NOT NULL", name="owner_required"),
        Index(
            "uq_carts_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active' AND user_id IS NOT NULL"),
            sqlite_where=text("status = 'active' AND user_id IS NOT NULL"),
        ),
    )


class CartItem(IdMixin, TimestampMixin, Base):
    """Price is not stored here: the server prices the cart from the variant at checkout."""

    __tablename__ = "cart_items"

    cart_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("carts.id", ondelete="CASCADE"))
    variant_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("product_variants.id", ondelete="CASCADE")
    )
    quantity: Mapped[int]

    cart: Mapped[Cart] = relationship(back_populates="items")

    __table_args__ = (
        UniqueConstraint("cart_id", "variant_id", name="cart_variant"),
        CheckConstraint("quantity > 0 AND quantity <= 99", name="quantity_range"),
    )


class Wishlist(IdMixin, TimestampMixin, Base):
    __tablename__ = "wishlists"

    user_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )

    items: Mapped[list[WishlistItem]] = relationship(
        back_populates="wishlist", cascade="all, delete-orphan"
    )


class WishlistItem(IdMixin, TimestampMixin, Base):
    __tablename__ = "wishlist_items"

    wishlist_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("wishlists.id", ondelete="CASCADE")
    )
    product_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("products.id", ondelete="CASCADE"))

    wishlist: Mapped[Wishlist] = relationship(back_populates="items")

    __table_args__ = (UniqueConstraint("wishlist_id", "product_id", name="wishlist_product"),)
