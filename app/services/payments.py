"""Payment providers.

Checkout only talks to the PaymentProvider interface, so an Indian gateway (Razorpay, Cashfree,
PayU) can be added without touching checkout:

1. Add a PaymentMethod value (for example "razorpay") and a migration for its CHECK constraint.
2. Write a provider whose start() creates the gateway order and returns redirect_url (or the
   data the gateway's checkout script needs), and whose confirm() verifies the gateway signature.
3. Register it in PROVIDERS and add a webhook route that calls mark_paid() / mark_failed().
4. Orders paid online stay "pending" until the webhook confirms payment. If payment fails or
   times out, cancel the order with orders.change_status(..., CANCELLED), which restocks.

See docs/payments.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy.orm import Session

from app.db.models import Order, Payment, PaymentMethod, PaymentStatus


@dataclass
class PaymentStart:
    """What checkout does next. redirect_url=None means "show the order confirmation"."""

    redirect_url: str | None = None


class PaymentProvider(Protocol):
    method: PaymentMethod
    label: str
    description: str

    def is_available(self, amount_paise: int) -> bool: ...

    def start(self, db: Session, order: Order, payment: Payment) -> PaymentStart: ...

    def on_delivered(self, db: Session, payment: Payment) -> None: ...


class CashOnDelivery:
    method = PaymentMethod.COD
    label = "Cash on delivery"
    description = "Pay in cash or UPI when you receive your order (or at pickup)."

    def is_available(self, amount_paise: int) -> bool:
        return amount_paise > 0

    def start(self, db: Session, order: Order, payment: Payment) -> PaymentStart:
        return PaymentStart()  # nothing to collect now

    def on_delivered(self, db: Session, payment: Payment) -> None:
        mark_paid(payment)


PROVIDERS: dict[str, PaymentProvider] = {PaymentMethod.COD.value: CashOnDelivery()}


def available_providers(amount_paise: int) -> list[PaymentProvider]:
    return [p for p in PROVIDERS.values() if p.is_available(amount_paise)]


def get_provider(method: str | None) -> PaymentProvider | None:
    return PROVIDERS.get(method or "")


def mark_paid(payment: Payment, provider_ref: str | None = None) -> None:
    payment.status = PaymentStatus.PAID
    payment.paid_at = datetime.now(UTC)
    if provider_ref:
        payment.provider_ref = provider_ref


def mark_failed(payment: Payment) -> None:
    if payment.status == PaymentStatus.PENDING:
        payment.status = PaymentStatus.FAILED
