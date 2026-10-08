"""Cart and checkout, including the edge cases: stock, price changes, duplicates, sessions."""

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Cart,
    CartItem,
    CartStatus,
    Delivery,
    DeliveryMethod,
    Inventory,
    Order,
    OrderItem,
    Payment,
    ProductStatus,
)
from app.services.settings import save_site_settings
from tests.web.conftest import HX, PINCODE, World, csrf, login, post


def add(client: TestClient, variant_id: int, qty: int = 1, **extra: str):  # type: ignore[no-untyped-def]
    return post(client, "/cart/add", {"variant_id": str(variant_id), "qty": str(qty), **extra})


def stock(db: Session, variant_id: int) -> int:
    db.expire_all()
    inv = db.get(Inventory, variant_id)
    assert inv is not None
    return inv.quantity


ADDRESS = {
    "name": "Asha Devi",
    "phone": "9000000001",
    "method": "delivery",
    "line1": "House 4, Station Road",
    "landmark": "Near Shiv Mandir",
    "city": "Gomia",
    "pincode": PINCODE,
    "payment_method": "cod",
}


def review(client: TestClient, data: dict | None = None) -> dict:  # type: ignore[type-arg]
    """Submit details; return the hidden fields of the review form."""
    form = {**ADDRESS, **(data or {})}
    if "slot" not in form:
        q = post(client, "/checkout/quote", form, headers=HX)
        m = re.search(r'name="slot" value="([^"]+)"', q.text)
        if m:
            form["slot"] = m.group(1)
    r = post(client, "/checkout/review", form)
    assert r.status_code == 200, r.text[:2000]
    fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', r.text))
    fields.pop("csrf_token", None)
    return fields


def place(client: TestClient, fields: dict):  # type: ignore[no-untyped-def,type-arg]
    return post(client, "/checkout/place", fields)


# ---- cart --------------------------------------------------------------------------------


def test_guest_add_update_remove(client: TestClient, world: World) -> None:
    r = add(client, world.haldi_100.id, 2)
    assert r.status_code == 303 and r.headers["location"] == "/cart"
    html = client.get("/cart").text
    assert "Haldi Powder" in html and "₹44" in html
    item_id = re.search(r'action="/cart/items/(\d+)"', html).group(1)  # type: ignore[union-attr]
    r = post(client, f"/cart/items/{item_id}", {"qty": "3"}, headers=HX)
    assert r.status_code == 200 and "₹66" in r.text and 'hx-swap-oob="true"' in r.text
    r = post(client, f"/cart/items/{item_id}/remove", headers=HX)
    assert "Your cart is empty" in r.text


def test_htmx_add_returns_toast_and_badge(client: TestClient, world: World) -> None:
    r = post(client, "/cart/add", {"variant_id": str(world.diya_1.id), "qty": "3"}, headers=HX)
    assert r.status_code == 200
    assert "Added to cart" in r.headers["HX-Trigger"]
    assert 'id="cart-count"' in r.text and ">3<" in r.text


@pytest.mark.parametrize("qty", ["0", "-2", "abc", "100", ""])
def test_invalid_quantities_rejected(
    client: TestClient, world: World, db: Session, qty: str
) -> None:
    r = post(client, "/cart/add", {"variant_id": str(world.diya_1.id), "qty": qty}, headers=HX)
    if qty == "":
        assert "Added to cart" in r.headers["HX-Trigger"]  # empty means 1
        return
    assert '"kind": "error"' in r.headers["HX-Trigger"]
    assert db.scalar(select(func.count()).select_from(CartItem)) == 0


def test_cannot_add_more_than_stock(client: TestClient, world: World) -> None:
    r = post(client, "/cart/add", {"variant_id": str(world.haldi_250.id), "qty": "4"}, headers=HX)
    assert "Only 3 left" in r.headers["HX-Trigger"]
    add(client, world.haldi_250.id, 3)
    r = post(client, "/cart/add", {"variant_id": str(world.haldi_250.id), "qty": "1"}, headers=HX)
    assert "Only 3 left" in r.headers["HX-Trigger"]


def test_cannot_add_out_of_stock_hidden_or_missing(
    client: TestClient, world: World, db: Session
) -> None:
    world.haldi_250.inventory.quantity = 0  # type: ignore[union-attr]
    world.diya.status = ProductStatus.INACTIVE
    db.commit()
    for vid, msg in [
        (world.haldi_250.id, "out of stock"),
        (world.diya_1.id, "not available"),
        (999999, "not available"),
    ]:
        r = post(client, "/cart/add", {"variant_id": str(vid), "qty": "1"}, headers=HX)
        assert msg in r.headers["HX-Trigger"], vid


def test_shop_quantity_limit_from_settings(client: TestClient, world: World, db: Session) -> None:
    save_site_settings(db, {"max_quantity_per_item": 5})
    db.commit()
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    r = post(client, "/cart/add", {"variant_id": str(world.diya_1.id), "qty": "6"}, headers=HX)
    assert "up to 5" in r.headers["HX-Trigger"]


def test_cart_flags_items_that_changed(client: TestClient, world: World, db: Session) -> None:
    add(client, world.haldi_100.id, 5)
    add(client, world.diya_1.id, 1)
    world.haldi_100.inventory.quantity = 2  # type: ignore[union-attr]
    world.diya.status = ProductStatus.INACTIVE  # product hidden after it was added
    db.commit()
    html = client.get("/cart").text
    assert "Only 2 left" in html and "No longer available" in html
    assert "Fix cart to continue" in html


def test_cannot_change_someone_elses_cart_item(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    add(client, world.haldi_100.id, 1)
    item_id = re.search(r"/cart/items/(\d+)", client.get("/cart").text).group(1)  # type: ignore[union-attr]
    other = TestClient(app)
    add(other, world.diya_1.id, 1)
    r = post(other, f"/cart/items/{item_id}", {"qty": "50"}, headers=HX)
    assert "no longer in your cart" in r.headers["HX-Trigger"]
    post(other, f"/cart/items/{item_id}/remove", headers=HX)
    assert "Haldi Powder" in client.get("/cart").text


def test_guest_cart_merges_on_login(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 1)
    post(client, "/logout")
    add(client, world.haldi_100.id, 2)
    add(client, world.diya_1.id, 1)
    login(client, world.customer.phone or "")
    cart = db.scalar(
        select(Cart).where(Cart.user_id == world.customer.id, Cart.status == CartStatus.ACTIVE)
    )
    assert cart is not None
    qty = {i.variant_id: i.quantity for i in cart.items}
    assert qty == {world.haldi_100.id: 3, world.diya_1.id: 1}


# ---- checkout ----------------------------------------------------------------------------


def test_checkout_requires_login(client: TestClient, world: World) -> None:
    add(client, world.haldi_100.id, 1)
    r = client.get("/checkout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/checkout"


def test_empty_cart_cannot_check_out(client: TestClient, world: World) -> None:
    login(client, world.customer.phone or "")
    r = client.get("/checkout", follow_redirects=False)
    assert r.headers["location"] == "/cart"


def test_full_checkout_creates_order(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 2)
    add(client, world.haldi_250.id, 1)
    fields = review(client)
    assert fields["fingerprint"] and fields["checkout_token"]
    r = place(client, fields)
    assert r.status_code == 303
    order_no = r.headers["location"].split("/")[2]
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order is not None
    assert order.subtotal_paise == 2 * 2200 + 5000  # sale price used
    assert order.delivery_fee_paise == 2000
    assert order.total_paise == order.subtotal_paise + 2000
    assert order.status.value == "pending"
    assert order.delivery_address["landmark"] == "Near Shiv Mandir"
    assert stock(db, world.haldi_100.id) == 8 and stock(db, world.haldi_250.id) == 2
    delivery = db.scalar(select(Delivery).where(Delivery.order_id == order.id))
    assert delivery.method == DeliveryMethod.DELIVERY and delivery.slot_label == "10 am - 1 pm"  # type: ignore[union-attr]
    payment = db.scalar(select(Payment).where(Payment.order_id == order.id))
    assert payment.method.value == "cod" and payment.amount_paise == order.total_paise  # type: ignore[union-attr]
    page = client.get(r.headers["location"])
    assert "Thank you" in page.text and order_no in page.text
    assert client.get("/cart").text.count("Your cart is empty") == 1


def test_free_delivery_threshold_and_discount_offer(
    client: TestClient, world: World, db: Session
) -> None:
    save_site_settings(
        db,
        {
            "offer_enabled": True,
            "offer_percent": 10,
            "offer_min_subtotal_paise": 10000,
            "offer_max_discount_paise": 1500,
        },
    )
    db.commit()
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    login(client, world.customer.phone or "")
    add(client, world.diya_1.id, 20)  # ₹100: just reaches the offer minimum
    add(client, world.haldi_100.id, 10)  # +₹220 -> 10% would be ₹32, capped at ₹15
    order_no = place(client, review(client)).headers["location"].split("/")[2]
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.subtotal_paise == 32000 and order.discount_paise == 1500  # type: ignore[union-attr]
    assert order.total_paise == 32000 + 2000 - 1500  # type: ignore[union-attr]


def test_store_pickup(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 1)
    fields = review(client, {"method": "pickup", "line1": "", "pincode": ""})
    order_no = place(client, fields).headers["location"].split("/")[2]
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.delivery_fee_paise == 0  # type: ignore[union-attr]
    assert order.deliveries[0].method == DeliveryMethod.PICKUP  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "change,message",
    [
        ({"pincode": "110001"}, "do not deliver to 110001"),
        ({"pincode": "12345"}, "valid 6-digit pincode"),
        ({"phone": "12345"}, "10-digit mobile"),
        ({"line1": ""}, "house / street"),
        ({"payment_method": "card"}, "payment method"),
    ],
)
def test_invalid_checkout_details(
    client: TestClient,
    world: World,
    change: dict,  # type: ignore[type-arg]
    message: str,
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    r = post(client, "/checkout/review", {**ADDRESS, **change})
    assert r.status_code == 422 and message in r.text


def test_minimum_order_for_delivery(client: TestClient, world: World) -> None:
    login(client, world.customer.phone or "")
    add(client, world.diya_1.id, 1)  # ₹5, zone minimum is ₹50
    r = post(client, "/checkout/review", ADDRESS)
    assert r.status_code == 422 and "Minimum order" in r.text


def test_duplicate_submission_creates_one_order(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    first = place(client, fields)
    second = place(client, fields)
    assert first.headers["location"] == second.headers["location"]
    assert db.scalar(select(func.count()).select_from(Order)) == 1
    assert stock(db, world.haldi_100.id) == 7  # taken once


def test_price_change_between_review_and_place(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    world.haldi_100.price_paise = 2500
    db.commit()
    r = place(client, fields)
    assert r.status_code == 409 and "Prices or delivery charges changed" in r.text
    assert "₹75" in r.text  # new total shown for confirmation
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    new_fields = dict(re.findall(r'<input type="hidden" name="(\w+)" value="([^"]*)"', r.text))
    new_fields.pop("csrf_token")
    assert place(client, new_fields).status_code == 303


def test_stock_taken_by_someone_else_before_place(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_250.id, 3)
    fields = review(client)
    world.haldi_250.inventory.quantity = 1  # type: ignore[union-attr]
    db.commit()
    r = place(client, fields)
    assert r.status_code == 303 and r.headers["location"] == "/cart"
    assert db.scalar(select(func.count()).select_from(Order)) == 0
    assert stock(db, world.haldi_250.id) == 1
    assert "Only 1 left" in client.get("/cart").text


def test_atomic_stock_update_prevents_overselling(world: World, db: Session) -> None:
    """The conditional UPDATE used by checkout never takes more than is available."""
    from sqlalchemy import update

    def take(q: int) -> int:
        return db.execute(
            update(Inventory)
            .where(Inventory.variant_id == world.haldi_250.id, Inventory.quantity >= q)
            .values(quantity=Inventory.quantity - q)
        ).rowcount  # type: ignore[attr-defined,no-any-return]

    assert take(2) == 1 and take(2) == 0 and take(1) == 1
    db.commit()
    assert stock(db, world.haldi_250.id) == 0


def test_expired_or_missing_checkout_token(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    fields["checkout_token"] = "made-up-token"
    r = place(client, fields)
    assert r.status_code == 303 and r.headers["location"] == "/checkout"
    assert db.scalar(select(func.count()).select_from(Order)) == 0


def test_expired_session_on_place(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    fresh = TestClient(app)  # no session cookie: like an expired session
    r = fresh.post(
        "/checkout/place", data={**fields, "csrf_token": csrf(fresh)}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_checkout_token_cannot_reveal_another_customers_order(
    client: TestClient,
    app,
    world: World,
    db: Session,  # type: ignore[no-untyped-def]
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    place(client, fields)
    from app.db.models import UserRole
    from tests.web.conftest import _user

    _user(db, "9000000099", UserRole.CUSTOMER, "Other")
    db.commit()
    other = TestClient(app)
    login(other, "9000000099")
    add(other, world.haldi_100.id, 3)
    review(other)
    r = place(other, fields)  # replaying the first customer's token
    assert r.status_code == 303 and "/orders/" not in r.headers["location"]


def test_client_cannot_set_prices_or_fees(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    fields = review(client)
    fields.update({"price": "1", "unit_price_paise": "1", "total": "1", "delivery_fee": "0"})
    order_no = place(client, fields).headers["location"].split("/")[2]
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.total_paise == 3 * 2200 + 2000  # type: ignore[union-attr]
    line = db.scalar(select(OrderItem).where(OrderItem.order_id == order.id))  # type: ignore[union-attr]
    assert line.unit_price_paise == 2200  # type: ignore[union-attr]


def test_customer_cancels_pending_order_and_stock_returns(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 4)
    order_no = place(client, review(client)).headers["location"].split("/")[2]
    assert stock(db, world.haldi_100.id) == 6
    post(client, f"/account/orders/{order_no}/cancel", {"reason": "ordered by mistake"})
    db.expire_all()
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.status.value == "cancelled"  # type: ignore[union-attr]
    assert stock(db, world.haldi_100.id) == 10
    assert "Cancelled" in client.get(f"/account/orders/{order_no}").text


def test_order_history_is_private(client: TestClient, app, world: World, db: Session) -> None:  # type: ignore[no-untyped-def]
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 3)
    order_no = place(client, review(client)).headers["location"].split("/")[2]
    assert order_no in client.get("/account").text
    from app.db.models import UserRole
    from tests.web.conftest import _user

    _user(db, "9000000098", UserRole.CUSTOMER, "Snooper")
    db.commit()
    other = TestClient(app)
    login(other, "9000000098")
    assert other.get(f"/account/orders/{order_no}").status_code == 404
    assert other.get(f"/orders/{order_no}/confirmed").status_code == 404
    assert post(other, f"/account/orders/{order_no}/cancel").status_code == 404


def test_deleted_product_keeps_order_history(client: TestClient, world: World, db: Session) -> None:
    login(client, world.customer.phone or "")
    add(client, world.diya_1.id, 20)
    order_no = place(client, review(client)).headers["location"].split("/")[2]
    db.delete(db.get(type(world.diya), world.diya.id))
    db.commit()
    html = client.get(f"/account/orders/{order_no}").text
    assert "Clay Diya" in html and "₹100" in html
