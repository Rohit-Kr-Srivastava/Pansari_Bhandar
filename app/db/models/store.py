"""Seller stores and their serviceable delivery zones."""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, BigIntFK, IdMixin, TimestampMixin

if TYPE_CHECKING:
    from app.db.models.catalog import Product
    from app.db.models.user import SellerProfile


class Store(IdMixin, TimestampMixin, Base):
    __tablename__ = "stores"

    seller_profile_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("seller_profiles.id", ondelete="CASCADE"), unique=True
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    name_hi: Mapped[str | None] = mapped_column(String(160))
    description: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(String(15))
    whatsapp: Mapped[str | None] = mapped_column(String(15))
    address_line: Mapped[str | None] = mapped_column(String(240))
    area: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str] = mapped_column(String(80), default="Gomia", server_default="Gomia")
    state: Mapped[str] = mapped_column(String(80), default="Jharkhand", server_default="Jharkhand")
    pincode: Mapped[str | None] = mapped_column(String(6))
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    delivery_radius_km: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    logo_path: Mapped[str | None] = mapped_column(String(300))
    pickup_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    pickup_instructions: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    seller_profile: Mapped[SellerProfile] = relationship(back_populates="store")
    products: Mapped[list[Product]] = relationship(back_populates="store")
    delivery_zones: Mapped[list[SellerDeliveryZone]] = relationship(
        back_populates="store", cascade="all, delete-orphan"
    )
    delivery_slots: Mapped[list[DeliverySlot]] = relationship(
        back_populates="store", cascade="all, delete-orphan", order_by="DeliverySlot.sort_order"
    )

    __table_args__ = (
        CheckConstraint("pincode IS NULL OR length(pincode) = 6", name="pincode_length"),
        CheckConstraint(
            "delivery_radius_km IS NULL OR delivery_radius_km >= 0", name="radius_non_negative"
        ),
    )


class SellerDeliveryZone(IdMixin, TimestampMixin, Base):
    """A pincode a store delivers to, with its own fee, minimum and free-delivery threshold."""

    __tablename__ = "seller_delivery_zones"

    store_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("stores.id", ondelete="CASCADE"))
    pincode: Mapped[str] = mapped_column(String(6))
    area_name: Mapped[str | None] = mapped_column(String(120))
    delivery_fee_paise: Mapped[int] = mapped_column(default=0, server_default="0")
    min_order_paise: Mapped[int] = mapped_column(default=0, server_default="0")
    free_delivery_above_paise: Mapped[int | None]
    same_day_available: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    same_day_cutoff: Mapped[time | None] = mapped_column(Time)
    # Days until delivery when same-day is not possible (1 = next day).
    estimated_days: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    store: Mapped[Store] = relationship(back_populates="delivery_zones")

    __table_args__ = (
        UniqueConstraint("store_id", "pincode", name="store_pincode"),
        CheckConstraint("length(pincode) = 6", name="pincode_length"),
        CheckConstraint("delivery_fee_paise >= 0", name="fee_non_negative"),
        CheckConstraint("min_order_paise >= 0", name="min_order_non_negative"),
        CheckConstraint(
            "free_delivery_above_paise IS NULL OR free_delivery_above_paise >= 0",
            name="free_above_non_negative",
        ),
        CheckConstraint("estimated_days BETWEEN 0 AND 30", name="estimated_days_range"),
        Index("ix_seller_delivery_zones_pincode", "pincode", "is_active"),
    )


class DeliverySlot(IdMixin, TimestampMixin, Base):
    """A delivery time window a store offers, for example "10 am - 1 pm"."""

    __tablename__ = "delivery_slots"

    store_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("stores.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(String(60))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    store: Mapped[Store] = relationship(back_populates="delivery_slots")

    __table_args__ = (
        CheckConstraint("end_time > start_time", name="end_after_start"),
        Index("ix_delivery_slots_store_id_sort_order", "store_id", "sort_order"),
    )
