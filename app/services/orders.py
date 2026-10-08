"""Order lifecycle: allowed status changes, restocking, payments and the audit trail."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    AdminAction,
    DeliveryMethod,
    DeliveryStatus,
    Inventory,
    Order,
    OrderStatus,
    PaymentStatus,
    User,
    UserRole,
)
from app.services.delivery import IST
from app.services.payments import get_provider

S = OrderStatus

TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    S.PENDING: {S.CONFIRMED, S.CANCELLED},
    S.CONFIRMED: {S.PREPARING, S.READY, S.CANCELLED},
    S.PREPARING: {S.READY, S.CANCELLED},
    S.READY: {S.OUT_FOR_DELIVERY, S.DELIVERED, S.CANCELLED},
    S.OUT_FOR_DELIVERY: {S.DELIVERED, S.CANCELLED},
    S.DELIVERED: {S.RETURNED},
    S.CANCELLED: set(),
    S.RETURNED: set(),
}

LABELS = {
    S.PENDING: "Pending",
    S.CONFIRMED: "Confirmed",
    S.PREPARING: "Preparing",
    S.READY: "Ready",
    S.OUT_FOR_DELIVERY: "Out for delivery",
    S.DELIVERED: "Delivered",
    S.CANCELLED: "Cancelled",
    S.RETURNED: "Returned",
}

CUSTOMER_LABELS = {
    **LABELS,
    S.PENDING: "Order received",
    S.READY: "Packed and ready",
}

OPEN_STATUSES = [S.PENDING, S.CONFIRMED, S.PREPARING, S.READY, S.OUT_FOR_DELIVERY]
RESTOCK_STATUSES = {S.CANCELLED, S.RETURNED}
_ORDER_NO_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class OrderError(ValueError):
    pass


def new_order_no() -> str:
    day = datetime.now(IST).strftime("%y%m%d")  # the shop's date, not UTC's
    suffix = "".join(secrets.choice(_ORDER_NO_ALPHABET) for _ in range(5))
    return f"PB{day}-{suffix}"


def load_order(db: Session, order_no: str) -> Order | None:
    return db.scalar(
        select(Order)
        .where(Order.order_no == order_no)
        .options(
            selectinload(Order.items),
            selectinload(Order.deliveries),
            selectinload(Order.payments),
        )
    )


def is_pickup(order: Order) -> bool:
    return bool(order.deliveries) and all(
        d.method == DeliveryMethod.PICKUP for d in order.deliveries
    )


def allowed_next(order: Order) -> list[OrderStatus]:
    nxt = TRANSITIONS[order.status]
    if is_pickup(order):
        nxt = nxt - {S.OUT_FOR_DELIVERY}
    else:
        # A delivery order goes out for delivery before it is delivered.
        if order.status == S.READY:
            nxt = nxt - {S.DELIVERED}
    return [s for s in S if s in nxt]


def store_ids(order: Order) -> set[int]:
    return {i.store_id for i in order.items}


def can_manage(user: User, order: Order) -> bool:
    if user.role == UserRole.ADMIN:
        return True
    if user.role == UserRole.SELLER and user.seller_profile and user.seller_profile.store:
        # Sellers manage orders that contain only their own store's items.
        return store_ids(order) == {user.seller_profile.store.id}
    return False


def _restock(db: Session, order: Order) -> None:
    for item in order.items:
        if item.variant_id is None:
            continue
        db.execute(
            update(Inventory)
            .where(Inventory.variant_id == item.variant_id)
            .values(quantity=Inventory.quantity + item.quantity)
        )


def change_status(
    db: Session,
    order: Order,
    new: OrderStatus,
    actor: User | None,
    reason: str | None = None,
    ip: str | None = None,
) -> None:
    old = order.status
    if new not in allowed_next(order):
        raise OrderError(f"Cannot change an order from {LABELS[old]} to {LABELS[new]}.")
    if new == S.CANCELLED and not (reason or "").strip():
        reason = "Cancelled"
    # Compare-and-set: of two simultaneous changes (double-clicked cancel, customer cancel vs
    # seller confirm) only one matches the old status, so stock is restocked exactly once.
    claimed = db.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == old)
        .values(status=new)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        db.rollback()
        raise OrderError("This order was just updated by someone else. Please reload.")
    order.status = new
    now = datetime.now(UTC)
    for d in order.deliveries:
        d.status = DeliveryStatus(new.value)
        if new == S.DELIVERED:
            d.delivered_at = now
    if new in RESTOCK_STATUSES:
        _restock(db, order)
    if new == S.CANCELLED:
        order.cancelled_at = now
        order.cancel_reason = (reason or "")[:500]
        for p in order.payments:
            if p.status == PaymentStatus.PENDING:
                p.status = PaymentStatus.FAILED
    if new == S.DELIVERED:
        for p in order.payments:
            provider = get_provider(p.method.value)
            if provider and p.status == PaymentStatus.PENDING:
                provider.on_delivered(db, p)
    if new == S.RETURNED:
        for p in order.payments:
            if p.status == PaymentStatus.PAID:
                p.status = PaymentStatus.REFUNDED
    db.add(
        AdminAction(
            actor_id=actor.id if actor else None,
            action=f"order.{new.value}",
            entity_type="order",
            entity_id=order.id,
            reason=reason,
            before={"status": old.value},
            after={"status": new.value},
            ip_address=ip,
        )
    )
    db.flush()


def customer_can_cancel(order: Order) -> bool:
    return order.status == S.PENDING
