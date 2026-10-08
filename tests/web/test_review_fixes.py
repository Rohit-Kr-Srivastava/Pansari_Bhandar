"""Regression tests for the final code review (docs: each test names the problem it guards)."""

import re
from datetime import UTC, datetime, time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    DeliverySlot,
    Inventory,
    Order,
    OrderItem,
    Product,
    SellerDeliveryZone,
)
from app.services import accounts, delivery
from app.web.templating import ist
from tests.web.conftest import HX, PINCODE, World, login, post
from tests.web.test_cart_checkout import add, place, review, stock
from tests.web.test_dashboards import create_product, png_bytes, product_form


def test_healthz_ignores_host_restrictions_but_pages_do_not(app, world: World) -> None:  # type: ignore[no-untyped-def]
    """Railway probes /healthz with its own Host header; every other page still checks Host."""
    from app.main import create_app

    strict = create_app(
        app.state.settings.model_copy(update={"allowed_hosts": "shop.example.in"}), app.state.engine
    )
    c = TestClient(strict, base_url="http://healthcheck.railway.app")
    assert c.get("/healthz").status_code == 200
    assert c.head("/healthz").status_code == 200
    assert c.get("/").status_code == 400
    assert TestClient(strict, base_url="http://shop.example.in").get("/").status_code == 200


def test_timestamps_display_in_indian_time() -> None:
    utc_evening = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)  # 01:30 on the 6th in India
    assert ist(utc_evening, "%d %b %Y, %I:%M %p") == "6 Oct 2026, 1:30 AM"
    assert ist(datetime(2026, 10, 5, 20, 0)) == ist(utc_evening)  # SQLite gives naive UTC
    assert ist(None) == ""


def test_dashboard_shows_order_time_in_ist(client: TestClient, world: World, db: Session) -> None:
    order = Order(
        order_no="PB-IST",
        user_id=world.customer.id,
        subtotal_paise=100,
        total_paise=100,
        placed_at=datetime(2026, 10, 5, 20, 0, tzinfo=UTC),
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            store_id=world.store.id,
            product_name="X",
            variant_label="1",
            unit_price_paise=100,
            quantity=1,
            line_total_paise=100,
        )
    )
    db.commit()
    login(client, world.admin.phone or "")
    html = client.get("/admin/orders?status=all").text
    assert "6 Oct, 1:30 AM" in html


def test_empty_or_junk_cart_quantity_does_not_delete_the_item(
    client: TestClient, world: World
) -> None:
    add(client, world.haldi_100.id, 2)
    item_id = re.search(r"/cart/items/(\d+)", client.get("/cart").text).group(1)  # type: ignore[union-attr]
    for junk in ["", "abc", "-1", "99999999"]:
        r = post(client, f"/cart/items/{item_id}", {"qty": junk}, headers=HX)
        assert '"kind": "error"' in r.headers["HX-Trigger"], junk
        assert "Haldi Powder" in r.text  # the cart is shown unchanged
    r = post(client, f"/cart/items/{item_id}", {"qty": "0"}, headers=HX)
    assert "Your cart is empty" in r.text  # only an explicit 0 removes the line


def test_delivery_pincode_and_slot_can_be_switched_off(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.seller.phone or "")
    zone = db.scalar(select(SellerDeliveryZone).where(SellerDeliveryZone.pincode == PINCODE))
    slot = db.scalar(select(DeliverySlot).where(DeliverySlot.store_id == world.store.id))
    assert zone and slot
    post(client, f"/seller/delivery/zones/{zone.id}", {"pincode": PINCODE, "fee": "20"})
    post(client, f"/seller/delivery/slots/{slot.id}", {"start_time": "10:00", "end_time": "13:00"})
    db.expire_all()
    assert zone.is_active is False and slot.is_active is False
    assert "do not deliver" in client.get(f"/delivery/check?pincode={PINCODE}", headers=HX).text


def test_stale_product_form_cannot_overwrite_sales(
    client: TestClient, world: World, db: Session
) -> None:
    """The owner opens the form (stock 10); a customer buys 4; the owner saves a price edit."""
    login(client, world.seller.phone or "")
    v = world.haldi_100
    form = product_form(
        world,
        name="Haldi Powder",
        name_hi="हल्दी पाउडर",
        category_id=str(world.spices.id),
        v_id=[str(v.id), str(world.haldi_250.id)],
        v_amount=["100", "250"],
        v_unit=["g", "g"],
        v_price=["25", "55"],
        v_sale=["", "50"],
        v_sku=[v.sku, world.haldi_250.sku],
        v_stock=["10", "3"],
        v_stock_was=["10", "3"],
        v_label=["100 g", "250 g"],
        v_threshold=["", ""],
    )
    inv = db.get(Inventory, v.id)
    assert inv
    inv.quantity = 6  # a customer's order took 4 while the form was open
    db.commit()
    post(client, f"/seller/products/{world.haldi.id}/edit", form)
    assert stock(db, v.id) == 6  # untouched: the owner did not change the stock field
    form["v_stock"] = ["12", "3"]  # now the owner adds 2 to what the form showed (10 -> 12)
    post(client, f"/seller/products/{world.haldi.id}/edit", form)
    assert stock(db, v.id) == 8  # 6 (real stock) + 2 (their change)


def test_removed_size_with_orders_can_be_added_again(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    place(client, review(client))
    seller = TestClient(client.app)
    login(seller, world.seller.phone or "")
    form = product_form(
        world,
        name="Haldi Powder",
        name_hi="",
        category_id=str(world.spices.id),
        v_id=["", str(world.haldi_250.id)],
        v_amount=["100", "250"],
        v_unit=["g", "g"],
        v_price=["30", "55"],
        v_sale=["", "50"],
        v_sku=["", world.haldi_250.sku],
        v_stock=["5", "3"],
        v_stock_was=["", "3"],
        v_label=["100 g", "250 g"],
        v_threshold=["", ""],
    )
    # the old "100 g" (ordered, so kept for history) is dropped and a new "100 g" is added
    r = post(seller, f"/seller/products/{world.haldi.id}/edit", form)
    assert r.status_code == 303, r.text[:500]
    db.expire_all()
    labels = sorted(v.label for v in db.get(Product, world.haldi.id).variants)  # type: ignore[union-attr]
    assert "100 g" in labels and any("(old " in x for x in labels)


def test_nan_size_is_a_validation_error(client: TestClient, world: World) -> None:
    login(client, world.seller.phone or "")
    r = create_product(client, "/seller", world, v_amount=["nan"])
    assert r.status_code == 422


def test_failed_save_leaves_no_orphan_photos(
    client: TestClient, world: World, db: Session, settings
) -> None:  # type: ignore[no-untyped-def]
    login(client, world.seller.phone or "")
    r = create_product(
        client,
        "/seller",
        world,
        v_sku=[world.haldi_100.sku],  # duplicate SKU
        files=[("images", ("p.png", png_bytes(), "image/png"))],
    )
    assert r.status_code == 422
    assert not [p for p in settings.media_root.rglob("*") if p.is_file()]


def test_no_javascript_pincode_check_shows_the_answer(client: TestClient, world: World) -> None:
    r = client.get(f"/delivery/check?pincode={PINCODE}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/delivery?pincode={PINCODE}"
    assert "Delivery available to 829111" in client.get(r.headers["location"]).text


def test_same_day_slots_do_not_wrap_after_midnight(db: Session, world: World) -> None:
    zone = db.scalar(select(SellerDeliveryZone).where(SellerDeliveryZone.pincode == PINCODE))
    assert zone
    zone.same_day_available = True
    zone.same_day_cutoff = time(23, 59)
    db.flush()
    db.refresh(world.store)
    late = datetime(2026, 10, 5, 23, 30, tzinfo=delivery.IST)
    q = delivery.quote_store(db, world.store, PINCODE, 10000, now=late)
    assert not q.same_day and q.eta_text == "Tomorrow"  # 10 am "today" has already passed


def test_crash_page_has_security_headers(app, world: World) -> None:  # type: ignore[no-untyped-def]
    @app.get("/crash-test")
    def crash() -> None:
        raise RuntimeError("boom")

    r = TestClient(app, raise_server_exceptions=False).get("/crash-test")
    assert r.status_code == 500
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_toast_link_comes_from_the_server(client: TestClient, world: World) -> None:
    ok = post(client, "/cart/add", {"variant_id": str(world.haldi_100.id)}, headers=HX)
    assert '"link": {"href": "/cart"' in ok.headers["HX-Trigger"]
    bad = post(client, "/cart/add", {"variant_id": "999999"}, headers=HX)
    assert '"link"' not in bad.headers["HX-Trigger"]


def test_hindi_text_is_marked_up_as_hindi(client: TestClient, world: World) -> None:
    assert 'class="hi" lang="hi"' in client.get("/p/haldi-powder").text


def test_simultaneous_registration_of_the_same_phone_is_not_a_500(
    db: Session, world: World, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(accounts, "_validate", lambda *a, **k: None)  # both passed the pre-check
    reg = accounts.Registration("Another Person", world.customer.phone or "", "", "good-pass-123")
    with pytest.raises(accounts.AccountError):
        accounts.register_customer(db, reg)
