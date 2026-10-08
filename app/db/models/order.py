"""Orders, order lines, payments and per-store delivery records."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    BigIntFK,
    IdMixin,
    JSONType,
    TimestampMixin,
    enum_check,
    enum_column,
)


class OrderStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY = "ready"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class PaymentMethod(StrEnum):
    COD = "cod"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class DeliveryMethod(StrEnum):
    DELIVERY = "delivery"
    PICKUP = "pickup"


class DeliveryStatus(StrEnum):
    """Fulfilment status of one store's part of an order. Same values as OrderStatus."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    PREPARING = "preparing"
    READY = "ready"
    OUT_FOR_DELIVERY = "out_for_delivery"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class Order(IdMixin, TimestampMixin, Base):
    """The customer's order. One order can hold items from several stores (see Delivery)."""

    __tablename__ = "orders"

    order_no: Mapped[str] = mapped_column(String(24), unique=True)
    user_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[OrderStatus] = mapped_column(
        enum_column(OrderStatus, "order_status"),
        default=OrderStatus.PENDING,
        server_default="pending",
    )
    # Idempotency key from the checkout form: a resubmitted checkout finds the existing order.
    checkout_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Copy of the address at the time of ordering, so later edits do not change old orders.
    delivery_address: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    subtotal_paise: Mapped[int]
    delivery_fee_paise: Mapped[int] = mapped_column(default=0, server_default="0")
    discount_paise: Mapped[int] = mapped_column(default=0, server_default="0")
    total_paise: Mapped[int]
    customer_note: Mapped[str | None] = mapped_column(Text)
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[OrderItem]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    deliveries: Mapped[list[Delivery]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_check("status", OrderStatus),
        CheckConstraint(
            "subtotal_paise >= 0 AND delivery_fee_paise >= 0 AND discount_paise >= 0",
            name="amounts_non_negative",
        ),
        CheckConstraint(
            "total_paise = subtotal_paise + delivery_fee_paise - discount_paise",
            name="total_matches_parts",
        ),
        Index("ix_orders_user_id_placed_at", "user_id", "placed_at"),
        Index("ix_orders_status_placed_at", "status", "placed_at"),
    )


class OrderItem(IdMixin, TimestampMixin, Base):
    """A priced line. Names and prices are copied so history survives later catalogue edits."""

    __tablename__ = "order_items"

    order_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("orders.id", ondelete="CASCADE"))
    store_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("stores.id", ondelete="RESTRICT"))
    product_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("products.id", ondelete="SET NULL")
    )
    variant_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("product_variants.id", ondelete="SET NULL")
    )
    product_name: Mapped[str] = mapped_column(String(160))
    variant_label: Mapped[str] = mapped_column(String(60))
    sku: Mapped[str | None] = mapped_column(String(64))
    unit_price_paise: Mapped[int]
    quantity: Mapped[int]
    line_total_paise: Mapped[int]

    order: Mapped[Order] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_paise >= 0", name="price_non_negative"),
        CheckConstraint(
            "line_total_paise = unit_price_paise * quantity", name="line_total_matches"
        ),
        Index("ix_order_items_order_id", "order_id"),
        Index("ix_order_items_store_id", "store_id"),
    )


class Payment(IdMixin, TimestampMixin, Base):
    """Cash on Delivery now. provider and provider_ref are reserved for online payments later."""

    __tablename__ = "payments"

    order_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("orders.id", ondelete="CASCADE"))
    method: Mapped[PaymentMethod] = mapped_column(
        enum_column(PaymentMethod, "payment_method"),
        default=PaymentMethod.COD,
        server_default="cod",
    )
    status: Mapped[PaymentStatus] = mapped_column(
        enum_column(PaymentStatus, "payment_status"),
        default=PaymentStatus.PENDING,
        server_default="pending",
    )
    amount_paise: Mapped[int]
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_ref: Mapped[str | None] = mapped_column(String(120))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    order: Mapped[Order] = relationship(back_populates="payments")

    __table_args__ = (
        enum_check("method", PaymentMethod),
        enum_check("status", PaymentStatus),
        CheckConstraint("amount_paise >= 0", name="amount_non_negative"),
        UniqueConstraint("provider", "provider_ref", name="provider_ref"),
        Index("ix_payments_order_id", "order_id"),
    )


class Delivery(IdMixin, TimestampMixin, Base):
    """How one store fulfils its part of an order: home delivery or store pickup."""

    __tablename__ = "delivery"

    order_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("orders.id", ondelete="CASCADE"))
    store_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("stores.id", ondelete="RESTRICT"))
    method: Mapped[DeliveryMethod] = mapped_column(
        enum_column(DeliveryMethod, "delivery_method"), default=DeliveryMethod.DELIVERY
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus, "delivery_status"),
        default=DeliveryStatus.PENDING,
        server_default="pending",
    )
    fee_paise: Mapped[int] = mapped_column(default=0, server_default="0")
    scheduled_date: Mapped[date | None] = mapped_column(Date)
    slot_label: Mapped[str | None] = mapped_column(String(60))
    is_same_day: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notes: Mapped[str | None] = mapped_column(Text)

    order: Mapped[Order] = relationship(back_populates="deliveries")

    __table_args__ = (
        enum_check("method", DeliveryMethod),
        enum_check("status", DeliveryStatus),
        UniqueConstraint("order_id", "store_id", name="order_store"),
        CheckConstraint("fee_paise >= 0", name="fee_non_negative"),
        CheckConstraint(
            "method = 'delivery' OR status <> 'out_for_delivery'",
            name="pickup_never_out_for_delivery",
        ),
        Index("ix_delivery_store_id_status", "store_id", "status"),
    )
