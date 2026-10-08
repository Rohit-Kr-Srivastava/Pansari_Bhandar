"""Numbers for the seller and admin dashboards. store_id=None means the whole marketplace."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Select, exists, func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductStatus,
    ProductVariant,
    SellerProfile,
    SellerStatus,
)
from app.services.delivery import now_ist
from app.services.orders import OPEN_STATUSES

NOT_SOLD = [OrderStatus.CANCELLED, OrderStatus.RETURNED]


def _start_of_today() -> datetime:
    now = now_ist()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def orders_query(store_id: int | None) -> Select[tuple[Order]]:
    stmt = select(Order)
    if store_id is not None:
        stmt = stmt.where(
            exists().where(OrderItem.order_id == Order.id, OrderItem.store_id == store_id)
        )
    return stmt


@dataclass
class LowStockRow:
    variant: ProductVariant
    product: Product
    quantity: int
    threshold: int


@dataclass
class PopularRow:
    name: str
    product_id: int | None
    quantity: int
    revenue: int


def low_stock(db: Session, store_id: int | None, limit: int = 10) -> list[LowStockRow]:
    stmt = (
        select(ProductVariant, Inventory.quantity, Inventory.low_stock_threshold)
        .join(Inventory, Inventory.variant_id == ProductVariant.id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(
            ProductVariant.is_active.is_(True),
            Product.status.in_([ProductStatus.APPROVED, ProductStatus.PENDING]),
            Inventory.quantity - Inventory.reserved <= Inventory.low_stock_threshold,
        )
        .order_by(Inventory.quantity, Product.name)
        .limit(limit)
        .options(selectinload(ProductVariant.product))
    )
    if store_id is not None:
        stmt = stmt.where(Product.store_id == store_id)
    return [LowStockRow(v, v.product, q, t) for v, q, t in db.execute(stmt)]


def low_stock_count(db: Session, store_id: int | None) -> int:
    stmt = (
        select(func.count())
        .select_from(ProductVariant)
        .join(Inventory, Inventory.variant_id == ProductVariant.id)
        .join(Product, Product.id == ProductVariant.product_id)
        .where(
            ProductVariant.is_active.is_(True),
            Product.status.in_([ProductStatus.APPROVED, ProductStatus.PENDING]),
            Inventory.quantity - Inventory.reserved <= Inventory.low_stock_threshold,
        )
    )
    if store_id is not None:
        stmt = stmt.where(Product.store_id == store_id)
    return db.scalar(stmt) or 0


def popular(db: Session, store_id: int | None, days: int = 30, limit: int = 5) -> list[PopularRow]:
    since = now_ist() - timedelta(days=days)
    stmt = (
        select(
            OrderItem.product_name,
            func.max(OrderItem.product_id),
            func.sum(OrderItem.quantity),
            func.sum(OrderItem.line_total_paise),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.placed_at >= since, Order.status.notin_(NOT_SOLD))
        .group_by(OrderItem.product_name)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(limit)
    )
    if store_id is not None:
        stmt = stmt.where(OrderItem.store_id == store_id)
    return [PopularRow(n, pid, int(q or 0), int(r or 0)) for n, pid, q, r in db.execute(stmt)]


def summary(db: Session, store_id: int | None) -> dict[str, int]:
    today = _start_of_today()
    base = orders_query(store_id).subquery()
    today_orders = (
        db.scalar(select(func.count()).select_from(base).where(base.c.placed_at >= today)) or 0
    )
    pending = (
        db.scalar(
            select(func.count()).select_from(base).where(base.c.status == OrderStatus.PENDING)
        )
        or 0
    )
    open_orders = (
        db.scalar(select(func.count()).select_from(base).where(base.c.status.in_(OPEN_STATUSES)))
        or 0
    )
    if store_id is None:
        sales = db.scalar(
            select(func.coalesce(func.sum(Order.total_paise), 0)).where(
                Order.placed_at >= today, Order.status.notin_(NOT_SOLD)
            )
        )
    else:  # a seller's sales are their own lines, not the whole order total
        sales = db.scalar(
            select(func.coalesce(func.sum(OrderItem.line_total_paise), 0))
            .join(Order, Order.id == OrderItem.order_id)
            .where(
                OrderItem.store_id == store_id,
                Order.placed_at >= today,
                Order.status.notin_(NOT_SOLD),
            )
        )
    products = select(func.count()).select_from(Product)
    if store_id is not None:
        products = products.where(Product.store_id == store_id)
    data = {
        "today_orders": today_orders,
        "today_sales": int(sales or 0),
        "pending_orders": pending,
        "open_orders": open_orders,
        "low_stock": low_stock_count(db, store_id),
        "products_live": db.scalar(products.where(Product.status == ProductStatus.APPROVED)) or 0,
        "products_pending": db.scalar(products.where(Product.status == ProductStatus.PENDING)) or 0,
    }
    if store_id is None:
        data["sellers_pending"] = (
            db.scalar(select(func.count()).where(SellerProfile.status == SellerStatus.PENDING)) or 0
        )
    return data
