"""Pure logic: delivery quotes with a fixed clock, WhatsApp messages, money formatting."""

from datetime import datetime, time
from urllib.parse import unquote

import pytest
from sqlalchemy.orm import Session

from app.core.formatting import parse_rupees, rupees, weight_label
from app.core.security import safe_next
from app.db.models import DeliveryMethod, DeliverySlot, SellerDeliveryZone
from app.services import delivery, whatsapp
from app.services.cart import CartSummary, Line
from app.services.settings import SiteSettings
from tests.web.conftest import PINCODE, World

IST = delivery.IST


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 10, 5, h, m, tzinfo=IST)  # a Monday


def zone(db: Session, world: World) -> SellerDeliveryZone:
    z = delivery.zone_for(db, world.store.id, PINCODE)
    assert z is not None
    return z


def test_quote_fee_minimum_and_free_threshold(db: Session, world: World) -> None:
    q = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9))
    assert q.available and q.fee == 2000 and q.area_name == "Gomia Bazar"
    q = delivery.quote_store(db, world.store, PINCODE, 50000, now=at(9))
    assert q.available and q.fee == 0  # free at the threshold
    q = delivery.quote_store(db, world.store, PINCODE, 4999, now=at(9))
    assert not q.available and "Minimum order" in q.reason


def test_quote_unknown_and_invalid_pincode(db: Session, world: World) -> None:
    q = delivery.quote_store(db, world.store, "560001", 10000, now=at(9))
    assert not q.available and "store pickup" in q.reason
    assert not delivery.quote_store(db, world.store, "0123", 10000).available


def test_inactive_zone_is_not_served(db: Session, world: World) -> None:
    zone(db, world).is_active = False
    db.flush()
    assert not delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9)).available


def test_next_day_by_default_with_slots(db: Session, world: World) -> None:
    q = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9))
    assert q.eta_text == "Tomorrow" and not q.same_day
    assert [s.label for s in q.slots] == ["Tomorrow, 10 am - 1 pm"]
    assert q.slots[0].value.startswith("2026-10-06|")
    assert q.slots[0].window == "10 am - 1 pm"


def test_estimated_days(db: Session, world: World) -> None:
    zone(db, world).estimated_days = 3
    db.flush()
    q = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9))
    assert q.eta_date and q.eta_date.isoformat() == "2026-10-08" and "Thu" in q.eta_text


def test_same_day_before_cutoff_and_after(db: Session, world: World) -> None:
    z = zone(db, world)
    z.same_day_available = True
    z.same_day_cutoff = time(14, 0)
    db.add(
        DeliverySlot(
            store_id=world.store.id, label="4 pm - 7 pm", start_time=time(16), end_time=time(19)
        )
    )
    db.flush()
    db.refresh(world.store)
    morning = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9))
    assert morning.same_day and morning.eta_text == "Today"
    labels = [s.label for s in morning.slots]
    assert labels[:2] == ["Today, 10 am - 1 pm", "Today, 4 pm - 7 pm"]
    late_morning = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(9, 30))
    assert "Today, 10 am - 1 pm" not in [s.label for s in late_morning.slots]  # needs 1 h lead
    evening = delivery.quote_store(db, world.store, PINCODE, 10000, now=at(15))
    assert not evening.same_day and evening.eta_text == "Tomorrow"


def test_pickup_quote(db: Session, world: World) -> None:
    q = delivery.quote_store(db, world.store, "", 100, DeliveryMethod.PICKUP)
    assert q.available and q.fee == 0
    world.store.pickup_available = False
    assert not delivery.quote_store(db, world.store, "", 100, DeliveryMethod.PICKUP).available


def test_multi_store_quote_needs_every_store(db: Session, world: World) -> None:
    q = delivery.quote(db, {world.store.id: 10000, world.other_store.id: 10000}, PINCODE, now=at(9))
    assert not q.available  # the other shop has no zone for this pincode
    assert any("Other-Shop" in p for p in q.problems)


def test_slot_must_be_one_the_server_offered(db: Session, world: World) -> None:
    q = delivery.quote(db, {world.store.id: 10000}, PINCODE, now=at(9))
    assert delivery.parse_slot(q.slots[0].value, q) is not None
    assert delivery.parse_slot("2026-10-06|999", q) is None
    assert delivery.parse_slot("2020-01-01|1", q) is None


# ---- WhatsApp ----------------------------------------------------------------------------


def test_whatsapp_product_and_cart_messages() -> None:
    s = SiteSettings(whatsapp_number="+91 90000 00009")
    link = whatsapp.product_link(s, "Turmeric Powder", "500g", 9000, "https://x.test/p/haldi")
    assert link and link.startswith("https://wa.me/919000000009?text=")
    text = unquote(link.split("text=")[1])
    assert text.startswith("Namaste Pansari Bhandar,\nI would like to enquire about:")
    assert "Turmeric Powder 500g" in text and "Price: ₹90" in text

    summary = CartSummary(
        lines=[
            Line(1, 1, 1, 1, "Turmeric Powder", "t", "500g", "S1", None, 1, 9000, 9000, 5),
            Line(2, 2, 2, 1, "Jeera", "j", "250g", "S2", None, 2, 5000, 5000, 5),
        ]
    )
    text = whatsapp.cart_message(s, summary)
    assert "I would like to order:" in text
    assert "* Turmeric Powder 500g" in text and "* Jeera 250g x 2" in text
    assert "Total: ₹190" in text and "Please confirm availability and delivery." in text


def test_whatsapp_hidden_without_number() -> None:
    assert whatsapp.contact_link(SiteSettings()) is None
    assert whatsapp.link("12345", "hi") is None
    assert whatsapp.link("9000000009", "hi") == "https://wa.me/919000000009?text=hi"


# ---- formatting & helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "paise,text",
    [(0, "₹0"), (1250, "₹12.50"), (150000, "₹1,500"), (12345600, "₹1,23,456"), (-500, "-₹5")],
)
def test_rupees(paise: int, text: str) -> None:
    assert rupees(paise) == text


def test_parse_rupees() -> None:
    assert parse_rupees("120") == 12000
    assert parse_rupees("₹1,200.50") == 120050
    assert parse_rupees("") is None
    for bad in ["-1", "abc", "1e99"]:
        with pytest.raises(ValueError):
            parse_rupees(bad)


def test_weight_label() -> None:
    assert [weight_label(g) for g in (50, 250, 1000, 1500)] == ["50 g", "250 g", "1 kg", "1.5 kg"]


@pytest.mark.parametrize(
    "url,expected",
    [
        ("/checkout", "/checkout"),
        ("/p/haldi?v=2", "/p/haldi?v=2"),
        ("https://evil.test/", "/"),
        ("//evil.test/", "/"),
        ("/\\evil.test", "/"),
        ("javascript:alert(1)", "/"),
        ("", "/"),
    ],
)
def test_safe_next(url: str, expected: str) -> None:
    assert safe_next(url) == expected
