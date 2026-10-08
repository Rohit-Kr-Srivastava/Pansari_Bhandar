"""Read side of the catalogue: what customers can see, search, filter and sort."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import ColumnElement, Select, Subquery, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.formatting import percent_off
from app.db.models import (
    Category,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
    SellerProfile,
    SellerStatus,
    Store,
)

PER_PAGE = 24
MAX_QUERY_LEN = 80


class Sort(StrEnum):
    RELEVANCE = "relevance"
    NEWEST = "newest"
    PRICE_LOW = "price_asc"
    PRICE_HIGH = "price_desc"
    NAME = "name"


SORT_LABELS = {
    Sort.RELEVANCE: "Best match",
    Sort.NEWEST: "Newest",
    Sort.PRICE_LOW: "Price: low to high",
    Sort.PRICE_HIGH: "Price: high to low",
    Sort.NAME: "Name A-Z",
}


# ---- view models -------------------------------------------------------------------------


def effective_price(v: ProductVariant) -> int:
    return v.sale_price_paise if v.sale_price_paise is not None else v.price_paise


def available_stock(v: ProductVariant) -> int:
    inv = v.inventory
    return max(inv.quantity - inv.reserved, 0) if inv else 0


@dataclass
class VariantView:
    id: int
    label: str
    price: int
    sale_price: int | None
    effective: int
    stock: int
    percent_off: int

    @property
    def in_stock(self) -> bool:
        return self.stock > 0


@dataclass
class ProductView:
    product: Product
    variants: list[VariantView]
    default: VariantView | None
    image: ProductImage | None
    images: list[ProductImage] = field(default_factory=list)

    @property
    def in_stock(self) -> bool:
        return any(v.in_stock for v in self.variants)

    @property
    def min_price(self) -> int | None:
        return min((v.effective for v in self.variants), default=None)

    @property
    def has_choices(self) -> bool:
        return len(self.variants) > 1


def to_view(product: Product, selected_variant_id: int | None = None) -> ProductView:
    variants = [
        VariantView(
            id=v.id,
            label=v.label,
            price=v.price_paise,
            sale_price=v.sale_price_paise,
            effective=effective_price(v),
            stock=available_stock(v),
            percent_off=percent_off(v.price_paise, v.sale_price_paise),
        )
        for v in product.variants
        if v.is_active
    ]
    default = next((v for v in variants if v.id == selected_variant_id), None)
    if default is None:
        default = next((v for v in variants if v.in_stock), variants[0] if variants else None)
    images = list(product.images)
    primary = next((i for i in images if i.is_primary), images[0] if images else None)
    if primary is not None and images and images[0] is not primary:
        images = [primary] + [i for i in images if i is not primary]
    return ProductView(product, variants, default, primary, images)


# ---- visibility --------------------------------------------------------------------------


def visible_products() -> Select[tuple[Product]]:
    """Approved products of active, approved stores in active categories."""
    return (
        select(Product)
        .join(Store, Store.id == Product.store_id)
        .join(SellerProfile, SellerProfile.id == Store.seller_profile_id)
        .join(Category, Category.id == Product.category_id)
        .where(
            Product.status == ProductStatus.APPROVED,
            Store.is_active.is_(True),
            SellerProfile.status == SellerStatus.APPROVED,
            Category.is_active.is_(True),
            exists().where(
                ProductVariant.product_id == Product.id, ProductVariant.is_active.is_(True)
            ),
        )
    )


def _card_options() -> list:  # type: ignore[type-arg]
    return [
        selectinload(Product.variants).selectinload(ProductVariant.inventory),
        selectinload(Product.images),
    ]


def _min_price_subquery() -> Subquery:
    eff = func.coalesce(ProductVariant.sale_price_paise, ProductVariant.price_paise)
    return (
        select(ProductVariant.product_id, func.min(eff).label("min_price"))
        .where(ProductVariant.is_active.is_(True))
        .group_by(ProductVariant.product_id)
        .subquery()
    )


def _in_stock_clause() -> ColumnElement[bool]:
    return exists().where(
        ProductVariant.product_id == Product.id,
        ProductVariant.is_active.is_(True),
        Inventory.variant_id == ProductVariant.id,
        Inventory.quantity - Inventory.reserved > 0,
    )


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def clean_query(q: str | None) -> str:
    return " ".join((q or "").split())[:MAX_QUERY_LEN]


# ---- listing -----------------------------------------------------------------------------


@dataclass
class ListingFilters:
    q: str = ""
    category_ids: list[int] | None = None
    min_price: int | None = None  # paise
    max_price: int | None = None
    in_stock: bool = False
    sort: Sort = Sort.RELEVANCE
    page: int = 1


@dataclass
class Page:
    items: list[ProductView]
    total: int
    page: int
    per_page: int

    @property
    def pages(self) -> int:
        return max(math.ceil(self.total / self.per_page), 1)

    @property
    def has_next(self) -> bool:
        return self.page < self.pages

    @property
    def has_prev(self) -> bool:
        return self.page > 1


def list_products(db: Session, f: ListingFilters, per_page: int = PER_PAGE) -> Page:
    prices = _min_price_subquery()
    stmt = visible_products().join(prices, prices.c.product_id == Product.id)

    if f.category_ids is not None:
        stmt = stmt.where(Product.category_id.in_(f.category_ids))
    if f.min_price is not None:
        stmt = stmt.where(prices.c.min_price >= f.min_price)
    if f.max_price is not None:
        stmt = stmt.where(prices.c.min_price <= f.max_price)
    if f.in_stock:
        stmt = stmt.where(_in_stock_clause())

    q = clean_query(f.q)
    relevance = None
    if q:
        for word in q.split()[:6]:
            like = f"%{_escape_like(word)}%"
            stmt = stmt.where(
                or_(
                    Product.name.ilike(like, escape="\\"),
                    Product.name_hi.ilike(like, escape="\\"),
                    Product.search_keywords.ilike(like, escape="\\"),
                    Product.brand.ilike(like, escape="\\"),
                    Category.name.ilike(like, escape="\\"),
                )
            )
        starts = f"{_escape_like(q)}%"
        contains = f"%{_escape_like(q)}%"
        relevance = case(
            (Product.name.ilike(starts, escape="\\"), 0),
            (Product.name.ilike(contains, escape="\\"), 1),
            else_=2,
        )

    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0

    sort = f.sort
    if sort is Sort.RELEVANCE and relevance is None:
        order: list[Any] = [Product.is_featured.desc(), Product.name.asc()]
    elif sort is Sort.RELEVANCE:
        order = [relevance, Product.name.asc()]
    elif sort is Sort.NEWEST:
        order = [Product.created_at.desc(), Product.id.desc()]
    elif sort is Sort.PRICE_LOW:
        order = [prices.c.min_price.asc(), Product.name.asc()]
    elif sort is Sort.PRICE_HIGH:
        order = [prices.c.min_price.desc(), Product.name.asc()]
    else:
        order = [Product.name.asc()]

    pages = max(math.ceil(total / per_page), 1)
    page = min(max(f.page, 1), pages)
    rows = db.scalars(
        stmt.order_by(*order, Product.id)
        .offset((page - 1) * per_page)
        .limit(per_page)
        .options(*_card_options())
    ).all()
    return Page([to_view(p) for p in rows], total, page, per_page)


# ---- categories --------------------------------------------------------------------------


def category_by_slug(db: Session, slug: str) -> Category | None:
    return db.scalar(
        select(Category)
        .where(Category.slug == slug, Category.is_active.is_(True))
        .options(selectinload(Category.children), selectinload(Category.parent))
    )


def category_ids_with_children(cat: Category) -> list[int]:
    return [cat.id] + [c.id for c in cat.children if c.is_active]


# ---- product detail ----------------------------------------------------------------------


def product_by_slug(db: Session, slug: str) -> Product | None:
    return db.scalar(
        visible_products()
        .where(Product.slug == slug)
        .options(
            *_card_options(),
            selectinload(Product.category).selectinload(Category.parent),
            selectinload(Product.store),
        )
    )


def related_products(db: Session, product: Product, limit: int = 8) -> list[ProductView]:
    cat = product.category
    ids = [cat.id]
    if cat.parent_id:
        ids.append(cat.parent_id)
    rows = db.scalars(
        visible_products()
        .where(Product.category_id.in_(ids), Product.id != product.id)
        .order_by(Product.is_featured.desc(), Product.name)
        .limit(limit)
        .options(*_card_options())
    ).all()
    return [to_view(p) for p in rows]


# ---- homepage ----------------------------------------------------------------------------
#
# The homepage shows many product rows. Each row first asks only for product ids (cheap), then
# one shared hydrate() loads every product, variant, stock row and image in a few queries,
# instead of repeating those loads once per row.


def popular_ids(db: Session, limit: int = 8, days: int = 60) -> list[int]:
    """Best sellers by quantity over the last N days; topped up with featured products."""
    since = datetime.now(UTC) - timedelta(days=days)
    sold = (
        select(OrderItem.product_id, func.sum(OrderItem.quantity).label("qty"))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.placed_at >= since,
            Order.status.notin_([OrderStatus.CANCELLED, OrderStatus.RETURNED]),
            OrderItem.product_id.is_not(None),
        )
        .group_by(OrderItem.product_id)
        .subquery()
    )
    ids = list(
        db.scalars(
            visible_products()
            .with_only_columns(Product.id)
            .join(sold, sold.c.product_id == Product.id)
            .order_by(sold.c.qty.desc(), Product.name)
            .limit(limit)
        )
    )
    if len(ids) < limit:
        ids += db.scalars(
            visible_products()
            .with_only_columns(Product.id)
            .where(Product.id.notin_(ids or [-1]))
            .order_by(Product.is_featured.desc(), Product.created_at.desc())
            .limit(limit - len(ids))
        ).all()
    return ids


def rail_ids(
    db: Session, category_slugs: list[str], seasonal_only: bool = False, limit: int = 8
) -> list[int]:
    if not category_slugs and not seasonal_only:
        return []
    conditions = []
    if category_slugs:
        parent = Category.__table__.alias("parent_cat")
        conditions.append(
            or_(
                Category.slug.in_(category_slugs),
                exists().where(
                    and_(parent.c.id == Category.parent_id, parent.c.slug.in_(category_slugs))
                ),
            )
        )
    if seasonal_only:
        conditions.append(Product.is_seasonal.is_(True))
    return list(
        db.scalars(
            visible_products()
            .with_only_columns(Product.id)
            .where(*conditions)
            .order_by(Product.is_featured.desc(), Product.is_seasonal.desc(), Product.name)
            .limit(limit)
        )
    )


def hydrate(db: Session, ids: Iterable[int]) -> dict[int, ProductView]:
    """Load card data for many products at once (4 queries however many products)."""
    wanted = sorted(set(ids))
    if not wanted:
        return {}
    rows = db.scalars(select(Product).where(Product.id.in_(wanted)).options(*_card_options()))
    return {p.id: to_view(p) for p in rows}
