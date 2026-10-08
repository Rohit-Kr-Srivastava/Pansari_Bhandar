"""Cart: guest carts (session token) and customer carts (user id), always priced on the server."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    Cart,
    CartItem,
    CartStatus,
    Product,
    ProductStatus,
    ProductVariant,
    SellerStatus,
    Store,
)
from app.services.catalog import available_stock, effective_price
from app.services.settings import SiteSettings

HARD_MAX_QTY = 99  # database limit; the shop's own limit is SiteSettings.max_quantity_per_item


class CartError(ValueError):
    """A message safe to show to the customer."""


# ---- loading -----------------------------------------------------------------------------


def get_cart(db: Session, user_id: int | None, guest_token: str | None) -> Cart | None:
    if user_id:
        return db.scalar(
            select(Cart).where(Cart.user_id == user_id, Cart.status == CartStatus.ACTIVE)
        )
    if guest_token:
        return db.scalar(
            select(Cart).where(
                Cart.session_token == guest_token,
                Cart.status == CartStatus.ACTIVE,
                Cart.user_id.is_(None),
            )
        )
    return None


def get_or_create_cart(db: Session, user_id: int | None, guest_token: str | None) -> Cart:
    cart = get_cart(db, user_id, guest_token)
    if cart:
        return cart
    cart = Cart(user_id=user_id, session_token=None if user_id else guest_token)
    db.add(cart)
    try:
        db.flush()
    except IntegrityError:  # a parallel request created it first
        db.rollback()
        existing = get_cart(db, user_id, guest_token)
        if existing is None:
            raise
        return existing
    return cart


def load_variant(db: Session, variant_id: int) -> ProductVariant | None:
    return db.scalar(
        select(ProductVariant)
        .where(ProductVariant.id == variant_id)
        .options(
            selectinload(ProductVariant.inventory),
            selectinload(ProductVariant.product)
            .selectinload(Product.store)
            .selectinload(Store.seller_profile),
            selectinload(ProductVariant.product).selectinload(Product.category),
            selectinload(ProductVariant.product).selectinload(Product.images),
        )
    )


def purchasable(v: ProductVariant | None) -> bool:
    if v is None or not v.is_active:
        return False
    p = v.product
    return (
        p.status == ProductStatus.APPROVED
        and p.store.is_active
        and p.store.seller_profile.status == SellerStatus.APPROVED
        and p.category.is_active
    )


# ---- changes -----------------------------------------------------------------------------


def _max_qty(settings: SiteSettings) -> int:
    return min(settings.max_quantity_per_item, HARD_MAX_QTY)


def _check_qty(qty: int, settings: SiteSettings) -> None:
    if qty < 1:
        raise CartError("Quantity must be at least 1.")
    if qty > _max_qty(settings):
        raise CartError(f"You can order up to {_max_qty(settings)} of an item.")


def add_item(
    db: Session, cart: Cart, variant_id: int, qty: int, settings: SiteSettings
) -> CartItem:
    _check_qty(qty, settings)
    variant = load_variant(db, variant_id)
    if not purchasable(variant):
        raise CartError("Sorry, this item is not available.")
    assert variant is not None
    stock = available_stock(variant)
    if stock <= 0:
        raise CartError(f"{variant.product.name} ({variant.label}) is out of stock.")
    item = db.scalar(
        select(CartItem).where(CartItem.cart_id == cart.id, CartItem.variant_id == variant_id)
    )
    new_qty = (item.quantity if item else 0) + qty
    if new_qty > _max_qty(settings):
        raise CartError(f"You can order up to {_max_qty(settings)} of an item.")
    if new_qty > stock:
        raise CartError(f"Only {stock} left of {variant.product.name} ({variant.label}).")
    if item:
        item.quantity = new_qty
    else:
        item = CartItem(cart_id=cart.id, variant_id=variant_id, quantity=new_qty)
        db.add(item)
    db.flush()
    return item


def _owned_item(db: Session, cart: Cart, item_id: int) -> CartItem:
    item = db.get(CartItem, item_id)
    if item is None or item.cart_id != cart.id:
        raise CartError("That item is no longer in your cart.")
    return item


def set_quantity(
    db: Session, cart: Cart, item_id: int, qty: int, settings: SiteSettings
) -> CartItem | None:
    item = _owned_item(db, cart, item_id)
    if qty <= 0:
        db.delete(item)
        db.flush()
        return None
    _check_qty(qty, settings)
    variant = load_variant(db, item.variant_id)
    if purchasable(variant):
        assert variant is not None
        stock = available_stock(variant)
        if qty > stock:
            raise CartError(f"Only {stock} left of {variant.product.name} ({variant.label}).")
    item.quantity = qty
    db.flush()
    return item


def remove_item(db: Session, cart: Cart, item_id: int) -> None:
    db.delete(_owned_item(db, cart, item_id))
    db.flush()


def merge_guest_cart(
    db: Session, guest_token: str | None, user_id: int, settings: SiteSettings
) -> None:
    """After login: move the guest cart's items into the customer's cart."""
    if not guest_token:
        return
    guest = get_cart(db, None, guest_token)
    if guest is None:
        return
    user_cart = get_cart(db, user_id, None)
    if user_cart is None:
        guest.user_id = user_id
        guest.session_token = None
        db.flush()
        return
    existing = {i.variant_id: i for i in user_cart.items}
    for gi in list(guest.items):
        if gi.variant_id in existing:
            existing[gi.variant_id].quantity = min(
                existing[gi.variant_id].quantity + gi.quantity, _max_qty(settings)
            )
        else:
            db.add(CartItem(cart_id=user_cart.id, variant_id=gi.variant_id, quantity=gi.quantity))
    guest.status = CartStatus.ABANDONED  # its token is dropped from the session by the caller
    for gi in list(guest.items):
        db.delete(gi)
    db.flush()


# ---- pricing -----------------------------------------------------------------------------


@dataclass
class Line:
    item_id: int
    variant_id: int
    product_id: int
    store_id: int
    product_name: str
    product_slug: str
    variant_label: str
    sku: str
    image_path: str | None
    quantity: int
    unit_price: int
    mrp: int
    stock: int
    problem: str = ""

    @property
    def line_total(self) -> int:
        return self.unit_price * self.quantity

    @property
    def ok(self) -> bool:
        return not self.problem


@dataclass
class CartSummary:
    lines: list[Line] = field(default_factory=list)
    discount: int = 0
    discount_label: str = ""

    @property
    def ok_lines(self) -> list[Line]:
        return [ln for ln in self.lines if ln.ok]

    @property
    def subtotal(self) -> int:
        return sum(ln.line_total for ln in self.ok_lines)

    @property
    def mrp_total(self) -> int:
        return sum(ln.mrp * ln.quantity for ln in self.ok_lines)

    @property
    def savings(self) -> int:
        return self.mrp_total - self.subtotal

    @property
    def count(self) -> int:
        return sum(ln.quantity for ln in self.lines)

    @property
    def has_problems(self) -> bool:
        return any(not ln.ok for ln in self.lines)

    @property
    def is_empty(self) -> bool:
        return not self.lines

    def store_subtotals(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for ln in self.ok_lines:
            out[ln.store_id] = out.get(ln.store_id, 0) + ln.line_total
        return out

    def fingerprint(self) -> str:
        """Changes whenever a price, quantity or discount changes (detects price changes)."""
        raw = "|".join(
            f"{ln.variant_id}:{ln.quantity}:{ln.unit_price}"
            for ln in sorted(self.ok_lines, key=lambda x: x.variant_id)
        )
        raw += f"|d{self.discount}"
        return hashlib.sha256(raw.encode()).hexdigest()[:20]


def compute_discount(subtotal: int, settings: SiteSettings) -> tuple[int, str]:
    if not settings.has_offer or subtotal <= 0 or subtotal < settings.offer_min_subtotal_paise:
        return 0, ""
    amount = subtotal * settings.offer_percent // 100
    if settings.offer_max_discount_paise:
        amount = min(amount, settings.offer_max_discount_paise)
    return amount, f"{settings.offer_percent}% off"


def summarise(db: Session, cart: Cart | None, settings: SiteSettings) -> CartSummary:
    summary = CartSummary()
    if cart is None:
        return summary
    items = db.scalars(
        select(CartItem).where(CartItem.cart_id == cart.id).order_by(CartItem.id)
    ).all()
    if not items:
        return summary
    variants = {
        v.id: v
        for v in db.scalars(
            select(ProductVariant)
            .where(ProductVariant.id.in_([i.variant_id for i in items]))
            .options(
                selectinload(ProductVariant.inventory),
                selectinload(ProductVariant.product)
                .selectinload(Product.store)
                .selectinload(Store.seller_profile),
                selectinload(ProductVariant.product).selectinload(Product.images),
                selectinload(ProductVariant.product).selectinload(Product.category),
            )
        )
    }
    for item in items:
        v = variants.get(item.variant_id)
        if v is None:
            continue
        p = v.product
        images = list(p.images)
        primary = next((i for i in images if i.is_primary), images[0] if images else None)
        stock = available_stock(v)
        line = Line(
            item_id=item.id,
            variant_id=v.id,
            product_id=p.id,
            store_id=p.store_id,
            product_name=p.name,
            product_slug=p.slug,
            variant_label=v.label,
            sku=v.sku,
            image_path=primary.path if primary else None,
            quantity=item.quantity,
            unit_price=effective_price(v),
            mrp=v.price_paise,
            stock=stock,
        )
        if not purchasable(v):
            line.problem = "No longer available. Please remove it."
        elif stock <= 0:
            line.problem = "Out of stock. Please remove it."
        elif item.quantity > stock:
            line.problem = f"Only {stock} left. Please reduce the quantity."
        summary.lines.append(line)
    summary.discount, summary.discount_label = compute_discount(summary.subtotal, settings)
    return summary


def cart_count(db: Session, user_id: int | None, guest_token: str | None) -> int:
    from sqlalchemy import func

    cart = get_cart(db, user_id, guest_token)
    if cart is None:
        return 0
    return (
        db.scalar(
            select(func.coalesce(func.sum(CartItem.quantity), 0)).where(CartItem.cart_id == cart.id)
        )
        or 0
    )
