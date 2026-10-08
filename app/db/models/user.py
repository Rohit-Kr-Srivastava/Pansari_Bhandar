"""Accounts: users, customer and seller profiles, delivery addresses."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntFK, IdMixin, TimestampMixin, enum_check, enum_column

if TYPE_CHECKING:
    from app.db.models.store import Store


class UserRole(StrEnum):
    CUSTOMER = "customer"
    SELLER = "seller"
    ADMIN = "admin"


class SellerStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class Language(StrEnum):
    ENGLISH = "en"
    HINDI = "hi"


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str | None] = mapped_column(String(254))
    phone: Mapped[str | None] = mapped_column(String(15), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "role"), default=UserRole.CUSTOMER, server_default="customer"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer_profile: Mapped[CustomerProfile | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    seller_profile: Mapped[SellerProfile | None] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        foreign_keys="SellerProfile.user_id",
    )
    addresses: Mapped[list[Address]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_check("role", UserRole),
        CheckConstraint("email IS NOT NULL OR phone IS NOT NULL", name="contact_required"),
        CheckConstraint("phone IS NULL OR length(phone) BETWEEN 10 AND 15", name="phone_length"),
        # Case-insensitive unique email.
        Index("uq_users_email_lower", text("lower(email)"), unique=True),
    )


class CustomerProfile(IdMixin, TimestampMixin, Base):
    __tablename__ = "customer_profiles"

    user_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    preferred_language: Mapped[Language] = mapped_column(
        enum_column(Language, "language"), default=Language.ENGLISH, server_default="en"
    )
    marketing_opt_in: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )

    user: Mapped[User] = relationship(back_populates="customer_profile")

    __table_args__ = (enum_check("preferred_language", Language),)


class SellerProfile(IdMixin, TimestampMixin, Base):
    __tablename__ = "seller_profiles"

    user_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    business_name: Mapped[str] = mapped_column(String(160))
    contact_name: Mapped[str | None] = mapped_column(String(120))
    contact_phone: Mapped[str | None] = mapped_column(String(15))
    status: Mapped[SellerStatus] = mapped_column(
        enum_column(SellerStatus, "seller_status"),
        default=SellerStatus.PENDING,
        server_default="pending",
    )
    reviewed_by_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="seller_profile", foreign_keys=[user_id])
    store: Mapped[Store | None] = relationship(
        back_populates="seller_profile", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_check("status", SellerStatus),
        Index("ix_seller_profiles_status", "status"),
    )


class Address(IdMixin, TimestampMixin, Base):
    __tablename__ = "addresses"

    user_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("users.id", ondelete="CASCADE"))
    label: Mapped[str | None] = mapped_column(String(40))
    recipient_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(15))
    line1: Mapped[str] = mapped_column(String(200))
    line2: Mapped[str | None] = mapped_column(String(200))
    landmark: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80), default="Gomia", server_default="Gomia")
    state: Mapped[str] = mapped_column(String(80), default="Jharkhand", server_default="Jharkhand")
    pincode: Mapped[str] = mapped_column(String(6))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    user: Mapped[User] = relationship(back_populates="addresses")

    __table_args__ = (
        CheckConstraint("length(pincode) = 6", name="pincode_length"),
        Index("ix_addresses_user_id", "user_id"),
        # At most one default address per user.
        Index(
            "uq_addresses_one_default_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )
