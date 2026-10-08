"""Checkout: validate the form, review the order, and create it exactly once.

Safety rules:
- Prices, fees and discounts are always recomputed on the server.
- The review page carries a price fingerprint. If anything changed before "Place order",
  the customer is shown the new totals instead of an order being placed.
- Each checkout form has a one-time token stored on the order (unique), so double clicks,
  refreshes and retries return the same order instead of creating a second one.
- Stock is taken with a conditional UPDATE (quantity >= requested), so two customers cannot
  buy the last packet twice.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import (
    Address,
    Cart,
    CartStatus,
    Delivery,
    DeliveryMethod,
    Inventory,
    Order,
    OrderItem,
    Payment,
    User,
)
from app.services import delivery as delivery_service
from app.services.cart import CartSummary, summarise
from app.services.orders import new_order_no
from app.services.payments import PaymentStart, get_provider
from app.services.settings import SiteSettings

PHONE_RE = re.compile(r"^[6-9][0-9]{9}$")


class CheckoutError(ValueError):
    pass


class PriceChanged(CheckoutError):
    pass


def normalise_phone(raw: str | None) -> str:
    digits = "".join(ch for ch in (raw or "") if ch in "0123456789")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


@dataclass
class CheckoutForm:
    name: str = ""
    phone: str = ""
    method: str = DeliveryMethod.DELIVERY.value
    line1: str = ""
    line2: str = ""
    landmark: str = ""
    city: str = "Gomia"
    pincode: str = ""
    slot: str = ""
    payment_method: str = "cod"
    note: str = ""
    save_address: bool = True

    @classmethod
    def from_mapping(cls, data: dict[str, str]) -> CheckoutForm:
        def g(key: str, limit: int) -> str:
            return " ".join(str(data.get(key, "") or "").split())[:limit]

        return cls(
            name=g("name", 120),
            phone=normalise_phone(data.get("phone")),
            method=g("method", 20) or DeliveryMethod.DELIVERY.value,
            line1=g("line1", 200),
            line2=g("line2", 200),
            landmark=g("landmark", 120),
            city=g("city", 80) or "Gomia",
            pincode=g("pincode", 6),
            slot=g("slot", 40),
            payment_method=g("payment_method", 20) or "cod",
            note=str(data.get("note", "") or "").strip()[:500],
            save_address=str(data.get("save_address", "")) in {"1", "on", "true"},
        )

    @property
    def delivery_method(self) -> DeliveryMethod:
        try:
            return DeliveryMethod(self.method)
        except ValueError:
            return DeliveryMethod.DELIVERY

    def errors(self) -> dict[str, str]:
        e: dict[str, str] = {}
        if len(self.name) < 2:
            e["name"] = "Enter your name."
        if not PHONE_RE.match(self.phone):
            e["phone"] = "Enter a 10-digit mobile number."
        if self.method not in {m.value for m in DeliveryMethod}:
            e["method"] = "Choose home delivery or store pickup."
        if self.delivery_method is DeliveryMethod.DELIVERY:
            if len(self.line1) < 3:
                e["line1"] = "Enter your house / street."
            if not delivery_service.valid_pincode(self.pincode):
                e["pincode"] = "Enter a valid 6-digit pincode."
        if get_provider(self.payment_method) is None:
            e["payment_method"] = "Choose a payment method."
        return e

    def address_snapshot(self) -> dict[str, str]:
        if self.delivery_method is DeliveryMethod.PICKUP:
            return {"name": self.name, "phone": self.phone, "method": "pickup"}
        return {
            "name": self.name,
            "phone": self.phone,
            "line1": self.line1,
            "line2": self.line2,
            "landmark": self.landmark,
            "city": self.city,
            "pincode": self.pincode,
            "method": "delivery",
        }


@dataclass
class Review:
    form: CheckoutForm
    summary: CartSummary
    quote: delivery_service.Quote
    slot: delivery_service.SlotOption | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def delivery_fee(self) -> int:
        return self.quote.fee if self.quote.available else 0

    @property
    def total(self) -> int:
        return self.summary.subtotal + self.delivery_fee - self.summary.discount

    @property
    def ok(self) -> bool:
        return not self.errors

    def fingerprint(self) -> str:
        return f"{self.summary.fingerprint()}-f{self.delivery_fee}"


def prefill(user: User, db: Session) -> CheckoutForm:
    form = CheckoutForm(name=user.full_name, phone=normalise_phone(user.phone))
    addr = db.scalar(
        select(Address)
        .where(Address.user_id == user.id)
        .order_by(Address.is_default.desc(), Address.updated_at.desc())
        .limit(1)
    )
    if addr:
        form.name = addr.recipient_name or form.name
        form.phone = normalise_phone(addr.phone) or form.phone
        form.line1, form.line2 = addr.line1, addr.line2 or ""
        form.landmark, form.city, form.pincode = addr.landmark or "", addr.city, addr.pincode
    return form


def review(
    db: Session,
    cart: Cart | None,
    form: CheckoutForm,
    settings: SiteSettings,
    summary: CartSummary | None = None,
) -> Review:
    summary = summary or summarise(db, cart, settings)
    errors = form.errors()
    quote = delivery_service.quote(
        db, summary.store_subtotals(), form.pincode, form.delivery_method
    )
    slot = delivery_service.parse_slot(form.slot, quote)
    if summary.is_empty:
        errors["cart"] = "Your cart is empty."
    elif summary.has_problems:
        errors["cart"] = "Some items in your cart need attention."
    if "pincode" not in errors and "method" not in errors and not summary.is_empty:
        if not quote.available:
            errors["delivery"] = " ".join(quote.problems) or "Delivery is not available."
        elif quote.slots and form.slot and slot is None:
            errors["slot"] = "That delivery slot is no longer available. Please choose again."
        elif quote.slots and not form.slot:
            errors["slot"] = "Choose a delivery slot."
    return Review(form, summary, quote, slot, errors)


def _existing(db: Session, token: str, user: User) -> Order | None:
    order = db.scalar(select(Order).where(Order.checkout_token == token))
    if order is not None and order.user_id != user.id:
        raise CheckoutError("This checkout link is not valid. Please try again.")
    return order


def place_order(
    db: Session,
    user: User,
    cart: Cart | None,
    form: CheckoutForm,
    settings: SiteSettings,
    token: str,
    expected_fingerprint: str,
) -> tuple[Order, PaymentStart, bool]:
    """Returns (order, payment_start, created). created=False for a repeated submission."""
    if not token or len(token) > 64:
        raise CheckoutError("Your checkout session expired. Please review your order again.")
    existing = _existing(db, token, user)
    if existing is not None:
        return existing, PaymentStart(), False

    r = review(db, cart, form, settings)
    if not r.ok or cart is None:
        raise CheckoutError(next(iter(r.errors.values()), "Please check your details."))
    if r.fingerprint() != expected_fingerprint:
        raise PriceChanged("Prices or delivery charges changed. Please check the new total.")
    provider = get_provider(form.payment_method)
    if provider is None or not provider.is_available(r.total):
        raise CheckoutError("That payment method is not available.")

    order = Order(
        order_no=new_order_no(),
        user_id=user.id,
        checkout_token=token,
        delivery_address=form.address_snapshot(),
        subtotal_paise=r.summary.subtotal,
        delivery_fee_paise=r.delivery_fee,
        discount_paise=r.summary.discount,
        total_paise=r.total,
        customer_note=form.note or None,
    )
    db.add(order)
    try:
        db.flush()
    except IntegrityError:  # the same token won a race in another request
        db.rollback()
        again = _existing(db, token, user)
        if again is None:
            raise
        return again, PaymentStart(), False

    for ln in r.summary.ok_lines:
        taken = db.execute(
            update(Inventory)
            .where(
                Inventory.variant_id == ln.variant_id,
                Inventory.quantity - Inventory.reserved >= ln.quantity,
            )
            .values(quantity=Inventory.quantity - ln.quantity)
        )
        if taken.rowcount != 1:  # type: ignore[attr-defined]
            db.rollback()
            raise CheckoutError(
                f"Sorry, {ln.product_name} ({ln.variant_label}) just went out of stock. "
                "Please review your cart."
            )
        db.add(
            OrderItem(
                order_id=order.id,
                store_id=ln.store_id,
                product_id=ln.product_id,
                variant_id=ln.variant_id,
                product_name=ln.product_name,
                variant_label=ln.variant_label,
                sku=ln.sku,
                unit_price_paise=ln.unit_price,
                quantity=ln.quantity,
                line_total_paise=ln.line_total,
            )
        )

    slot_date: date | None = None
    slot_label: str | None = None
    if r.slot:
        slot_date = date.fromisoformat(r.slot.value.split("|", 1)[0])
        slot_label = r.slot.window
    for sq in r.quote.stores:
        db.add(
            Delivery(
                order_id=order.id,
                store_id=sq.store_id,
                method=form.delivery_method,
                fee_paise=sq.fee,
                scheduled_date=slot_date or sq.eta_date,
                slot_label=slot_label,
                is_same_day=sq.same_day,
            )
        )
    payment = Payment(order_id=order.id, method=provider.method, amount_paise=r.total)
    db.add(payment)

    cart.status = CartStatus.CONVERTED
    cart.session_token = None
    if form.save_address and form.delivery_method is DeliveryMethod.DELIVERY:
        _save_address(db, user, form)
    db.flush()
    start = provider.start(db, order, payment)
    db.commit()
    return order, start, True


def _save_address(db: Session, user: User, form: CheckoutForm) -> None:
    addresses = db.scalars(select(Address).where(Address.user_id == user.id)).all()
    match = next(
        (a for a in addresses if a.line1 == form.line1 and a.pincode == form.pincode), None
    )
    for a in addresses:
        if a.is_default and a is not match:
            a.is_default = False
    db.flush()
    if match is None:
        match = Address(
            user_id=user.id,
            line1=form.line1,
            pincode=form.pincode,
            recipient_name=form.name,
            phone=form.phone,
        )
        db.add(match)
    match.recipient_name, match.phone = form.name, form.phone
    match.line2, match.landmark, match.city = form.line2 or None, form.landmark or None, form.city
    match.is_default = True
