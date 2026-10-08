"""Local delivery rules. Everything comes from the database (seller_delivery_zones,
delivery_slots, stores), configured from the seller and admin dashboards.

The pincode list is authoritative. stores.delivery_radius_km is shown to customers as
guidance only, because customers' coordinates are not collected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.formatting import rupees
from app.db.models import DeliveryMethod, DeliverySlot, SellerDeliveryZone, Store

IST = ZoneInfo("Asia/Kolkata")
PINCODE_RE = re.compile(r"^[1-9][0-9]{5}$")
SAME_DAY_SLOT_LEAD = timedelta(minutes=60)


def now_ist() -> datetime:
    return datetime.now(IST)


def valid_pincode(pincode: str | None) -> bool:
    return bool(pincode) and bool(PINCODE_RE.match(pincode or ""))


@dataclass
class SlotOption:
    value: str  # "2026-10-05|3" (date|slot id), posted back by the checkout form
    label: str  # for display, e.g. "Today, 4 pm - 7 pm"
    window: str = ""  # the slot itself, e.g. "4 pm - 7 pm" (stored on the order)


@dataclass
class StoreQuote:
    store_id: int
    store_name: str
    available: bool
    reason: str = ""
    fee: int = 0
    base_fee: int = 0
    free_above: int | None = None
    min_order: int = 0
    same_day: bool = False
    eta_date: date | None = None
    eta_text: str = ""
    area_name: str = ""
    slots: list[SlotOption] = field(default_factory=list)


@dataclass
class Quote:
    method: DeliveryMethod
    pincode: str
    stores: list[StoreQuote]

    @property
    def available(self) -> bool:
        return bool(self.stores) and all(s.available for s in self.stores)

    @property
    def fee(self) -> int:
        return sum(s.fee for s in self.stores if s.available)

    @property
    def problems(self) -> list[str]:
        many = len(self.stores) > 1
        return [
            f"{s.store_name}: {s.reason}" if many else s.reason
            for s in self.stores
            if not s.available
        ]

    @property
    def eta_text(self) -> str:
        texts = {s.eta_text for s in self.stores if s.available}
        return " / ".join(sorted(texts))

    @property
    def slots(self) -> list[SlotOption]:
        # Slots are offered when the order comes from a single store (the usual case).
        return self.stores[0].slots if len(self.stores) == 1 and self.available else []


def _day_text(d: date, today: date) -> str:
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%a, %d %b").replace(" 0", " ")


def _slots_for(slots: list[DeliverySlot], day: date, earliest: datetime | None) -> list[SlotOption]:
    """Active slots on `day`; with `earliest`, only those starting at or after that moment."""
    return [
        SlotOption(f"{day.isoformat()}|{s.id}", s.label, s.label)
        for s in slots
        if s.is_active
        and (earliest is None or datetime.combine(day, s.start_time, tzinfo=IST) >= earliest)
    ]


def zone_for(db: Session, store_id: int, pincode: str) -> SellerDeliveryZone | None:
    return db.scalar(
        select(SellerDeliveryZone).where(
            SellerDeliveryZone.store_id == store_id,
            SellerDeliveryZone.pincode == pincode,
            SellerDeliveryZone.is_active.is_(True),
        )
    )


def quote_store(
    db: Session,
    store: Store,
    pincode: str,
    subtotal: int | None,
    method: DeliveryMethod = DeliveryMethod.DELIVERY,
    now: datetime | None = None,
) -> StoreQuote:
    """subtotal=None means "no cart yet": skip the minimum-order check and quote the base fee."""
    now = now or now_ist()
    today = now.date()
    q = StoreQuote(store.id, store.name, available=False)

    if method is DeliveryMethod.PICKUP:
        if not store.pickup_available:
            q.reason = "Store pickup is not available for this shop."
            return q
        q.available = True
        q.eta_date = today
        q.eta_text = "Pickup from the shop. We will message you when it is ready."
        return q

    if not valid_pincode(pincode):
        q.reason = "Enter a valid 6-digit pincode."
        return q
    zone = zone_for(db, store.id, pincode)
    if zone is None:
        q.reason = f"We do not deliver to {pincode} yet."
        if store.pickup_available:
            q.reason += " You can choose store pickup."
        return q

    q.area_name = zone.area_name or ""
    q.base_fee = zone.delivery_fee_paise
    q.free_above = zone.free_delivery_above_paise
    q.min_order = zone.min_order_paise
    if subtotal is not None and subtotal < zone.min_order_paise:
        q.reason = f"Minimum order for delivery to {pincode} is {rupees(zone.min_order_paise)}."
        return q

    free = (
        subtotal is not None
        and zone.free_delivery_above_paise is not None
        and subtotal >= zone.free_delivery_above_paise
    )
    q.fee = 0 if free else zone.delivery_fee_paise
    q.available = True

    slots = list(store.delivery_slots)
    cutoff_ok = zone.same_day_cutoff is None or now.time() < zone.same_day_cutoff
    if zone.same_day_available and cutoff_ok:
        same_day_slots = _slots_for(slots, today, now + SAME_DAY_SLOT_LEAD)
        if same_day_slots or not [s for s in slots if s.is_active]:
            q.same_day = True
            q.eta_date = today
            q.eta_text = "Today"
            q.slots = same_day_slots
            # Same-day customers can still pick tomorrow's slots.
            q.slots += _slots_for(slots, today + timedelta(days=1), None)
            _label_days(q.slots, today)
            return q

    days = max(zone.estimated_days, 1)
    q.eta_date = today + timedelta(days=days)
    q.eta_text = _day_text(q.eta_date, today)
    q.slots = _slots_for(slots, q.eta_date, None)
    _label_days(q.slots, today)
    return q


def _label_days(options: list[SlotOption], today: date) -> None:
    for o in options:
        day = date.fromisoformat(o.value.split("|", 1)[0])
        o.label = f"{_day_text(day, today)}, {o.label}"


def quote(
    db: Session,
    store_subtotals: dict[int, int],
    pincode: str,
    method: DeliveryMethod = DeliveryMethod.DELIVERY,
    now: datetime | None = None,
) -> Quote:
    stores = db.scalars(select(Store).where(Store.id.in_(store_subtotals))).all()
    by_id = {s.id: s for s in stores}
    quotes = [
        quote_store(db, by_id[sid], pincode, subtotal, method, now)
        for sid, subtotal in store_subtotals.items()
        if sid in by_id
    ]
    return Quote(method, pincode, quotes)


def parse_slot(value: str | None, q: Quote) -> SlotOption | None:
    """Accept only a slot the server itself offered for this quote."""
    if not value:
        return None
    return next((s for s in q.slots if s.value == value), None)


def public_check(db: Session, pincode: str, store_ids: list[int] | None = None) -> list[StoreQuote]:
    """Pincode check for the product page and the delivery page (no cart amount yet)."""
    stmt = select(Store).where(Store.is_active.is_(True))
    if store_ids:
        stmt = stmt.where(Store.id.in_(store_ids))
    return [quote_store(db, s, pincode, subtotal=None) for s in db.scalars(stmt)]


def serviceable_pincodes(db: Session) -> list[SellerDeliveryZone]:
    return list(
        db.scalars(
            select(SellerDeliveryZone)
            .join(Store, Store.id == SellerDeliveryZone.store_id)
            .where(SellerDeliveryZone.is_active.is_(True), Store.is_active.is_(True))
            .order_by(SellerDeliveryZone.pincode)
            .options(selectinload(SellerDeliveryZone.store))
        )
    )
