"""WhatsApp click-to-chat links with ready-written messages (wa.me, no API account needed).

The shop's number is a site setting (admin > Settings), never hard-coded.
"""

from __future__ import annotations

from urllib.parse import quote

from app.core.formatting import rupees
from app.db.models import Order
from app.services.cart import CartSummary
from app.services.settings import SiteSettings

GREETING = "Namaste {name},"


def link(number: str, message: str) -> str | None:
    digits = "".join(ch for ch in number if ch in "0123456789")
    if len(digits) == 10:
        digits = "91" + digits
    if len(digits) < 11:
        return None
    return f"https://wa.me/{digits}?text={quote(message, safe='')}"


def _greet(s: SiteSettings) -> str:
    return GREETING.format(name=s.business_name)


def product_message(
    s: SiteSettings, product_name: str, variant_label: str | None, price: int | None, url: str
) -> str:
    title = f"{product_name} {variant_label}".strip() if variant_label else product_name
    lines = [_greet(s), "I would like to enquire about:", title]
    if price is not None:
        lines.append(f"Price: {rupees(price)}")
    lines += ["", url]
    return "\n".join(lines)


def cart_message(s: SiteSettings, summary: CartSummary) -> str:
    lines = [_greet(s), "I would like to order:"]
    for ln in summary.ok_lines:
        qty = f" x {ln.quantity}" if ln.quantity > 1 else ""
        lines.append(f"* {ln.product_name} {ln.variant_label}{qty}")
    lines += ["", f"Total: {rupees(summary.subtotal - summary.discount)}"]
    lines.append("Please confirm availability and delivery.")
    return "\n".join(lines)


def order_message(s: SiteSettings, order: Order) -> str:
    return "\n".join(
        [
            _greet(s),
            f"I need help with my order {order.order_no}.",
            f"Total: {rupees(order.total_paise)}",
        ]
    )


def contact_message(s: SiteSettings) -> str:
    return "\n".join([_greet(s), "I have a question."])


def product_link(s: SiteSettings, *args: object) -> str | None:
    return link(s.whatsapp_number, product_message(s, *args))  # type: ignore[arg-type]


def cart_link(s: SiteSettings, summary: CartSummary) -> str | None:
    if summary.is_empty:
        return None
    return link(s.whatsapp_number, cart_message(s, summary))


def order_link(s: SiteSettings, order: Order) -> str | None:
    return link(s.whatsapp_number, order_message(s, order))


def contact_link(s: SiteSettings) -> str | None:
    return link(s.whatsapp_number, contact_message(s))


def customer_link(phone: str | None, message: str) -> str | None:
    """For the seller: message a customer about their order."""
    return link(phone or "", message)
