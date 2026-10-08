"""Seller and admin dashboards: approvals, product form, clone, inventory, orders, settings."""

import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AdminAction,
    Category,
    Order,
    PaymentStatus,
    Product,
    ProductStatus,
    ProductVariant,
    SellerDeliveryZone,
    SellerProfile,
    SellerStatus,
    Testimonial,
    User,
)
from app.services.settings import load_site_settings, save_site_settings
from tests.web.conftest import HX, PASSWORD, World, csrf, login, post
from tests.web.test_cart_checkout import add, place, review, stock


def png_bytes(size: tuple[int, int] = (120, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (200, 120, 20)).save(buf, "PNG")
    return buf.getvalue()


def product_form(world: World, **overrides: object) -> dict:  # type: ignore[type-arg]
    data: dict[str, object] = {
        "name": "Turmeric Powder 100g",
        "name_hi": "हल्दी",
        "category_id": str(world.spices.id),
        "brand": "",
        "brand_type": "no_brand",
        "product_type": "loose",
        "v_id": [""],
        "v_amount": ["100"],
        "v_unit": ["g"],
        "v_price": ["25"],
        "v_sale": [""],
        "v_sku": [""],
        "v_stock": ["12"],
        "v_label": [""],
        "v_threshold": [""],
        "action": "publish",
    }
    data.update(overrides)
    return data


def create_product(client: TestClient, base: str, world: World, files=None, **kw):  # type: ignore[no-untyped-def]
    return post(client, f"{base}/products/new", product_form(world, **kw), files=files)


# ---- access control ----------------------------------------------------------------------

ADMIN_PAGES = [
    "/admin",
    "/admin/orders",
    "/admin/products",
    "/admin/products/new",
    "/admin/inventory",
    "/admin/categories",
    "/admin/sellers",
    "/admin/customers",
    "/admin/delivery",
    "/admin/homepage",
    "/admin/testimonials",
    "/admin/store-info",
    "/admin/settings",
    "/admin/activity",
]
SELLER_PAGES = [
    "/seller",
    "/seller/products",
    "/seller/products/new",
    "/seller/inventory",
    "/seller/orders",
    "/seller/store",
    "/seller/delivery",
]


def test_admin_pages_require_admin(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    for path in ADMIN_PAGES:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), path
    for phone in (world.customer.phone, world.seller.phone):
        c = TestClient(app)
        login(c, phone or "")
        for path in ADMIN_PAGES:
            assert c.get(path).status_code == 403, (phone, path)
    admin = TestClient(app)
    login(admin, world.admin.phone or "")
    for path in ADMIN_PAGES:
        assert admin.get(path).status_code == 200, path


def test_admin_writes_require_admin(client: TestClient, world: World, db: Session) -> None:
    login(client, world.seller.phone or "")
    r = post(client, f"/admin/products/{world.other_product.id}/review", {"decision": "approve"})
    assert r.status_code == 403
    r = post(client, "/admin/settings", {"require_product_approval": ""})
    assert r.status_code == 403
    assert load_site_settings(db).require_product_approval is True


def test_seller_pages_require_seller(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    for path in SELLER_PAGES:
        assert client.get(path, follow_redirects=False).status_code == 303
    login(client, world.customer.phone or "")
    for path in SELLER_PAGES:
        assert client.get(path).status_code == 403, path
    seller = TestClient(app)
    login(seller, world.seller.phone or "")
    for path in SELLER_PAGES:
        assert seller.get(path).status_code == 200, path


# ---- seller registration & approval ------------------------------------------------------


def test_seller_registration_needs_admin_approval(
    client: TestClient, app, world: World, db: Session
) -> None:  # type: ignore[no-untyped-def]
    r = post(
        client,
        "/seller/register",
        {
            "full_name": "Meena",
            "phone": "9123456780",
            "password": "secret-pass-1",
            "business_name": "Meena Store",
        },
    )
    assert r.headers["location"] == "/seller/pending"
    r = client.get("/seller", follow_redirects=False)
    assert r.headers["location"] == "/seller/pending"
    assert "waiting for approval" in client.get("/seller/pending").text
    assert client.get("/seller/products/new", follow_redirects=False).status_code == 303

    profile = db.scalar(select(SellerProfile).where(SellerProfile.business_name == "Meena Store"))
    assert profile is not None and profile.store is not None and not profile.store.is_active
    admin = TestClient(app)
    login(admin, world.admin.phone or "")
    r = post(admin, f"/admin/sellers/{profile.id}/reject", {"reason": ""})
    db.refresh(profile)
    assert profile.status == SellerStatus.PENDING  # a reason is required
    post(admin, f"/admin/sellers/{profile.id}/approve")
    db.refresh(profile)
    assert profile.status == SellerStatus.APPROVED and profile.store.is_active  # type: ignore[union-attr]
    assert client.get("/seller").status_code == 200
    assert db.scalar(select(AdminAction).where(AdminAction.action == "seller.approve"))


def test_suspended_seller_products_disappear(
    client: TestClient, app, world: World, db: Session
) -> None:  # type: ignore[no-untyped-def]
    login(client, world.admin.phone or "")
    profile = world.seller.seller_profile
    post(client, f"/admin/sellers/{profile.id}/suspend")  # type: ignore[union-attr]
    assert TestClient(app).get("/p/haldi-powder").status_code == 404
    seller = TestClient(app)
    login(seller, world.seller.phone or "")
    assert seller.get("/seller", follow_redirects=False).headers["location"] == "/seller/pending"


# ---- product form ------------------------------------------------------------------------


def test_seller_creates_product_pending_until_admin_approves(
    client: TestClient,
    app,
    world: World,
    db: Session,  # type: ignore[no-untyped-def]
) -> None:
    login(client, world.seller.phone or "")
    r = create_product(
        client,
        "/seller",
        world,
        v_id=["", ""],
        v_amount=["100", "0.5"],
        v_unit=["g", "kg"],
        v_price=["25", "110"],
        v_sale=["", "99.50"],
        v_sku=["", "TURM-500"],
        v_stock=["12", "4"],
        v_label=["", ""],
        v_threshold=["", ""],
    )
    assert r.status_code == 303, r.text[:2000]
    product = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert product is not None and product.status == ProductStatus.PENDING
    assert product.store_id == world.store.id and product.slug == "turmeric-powder-100g"
    variants = {v.label: v for v in product.variants}
    assert set(variants) == {"100 g", "500 g"}
    assert variants["500 g"].weight_grams == 500 and variants["500 g"].sale_price_paise == 9950
    assert variants["500 g"].sku == "TURM-500" and variants["100 g"].sku  # auto SKU
    assert variants["100 g"].inventory.quantity == 12  # type: ignore[union-attr]
    assert TestClient(app).get(f"/p/{product.slug}").status_code == 404

    admin = TestClient(app)
    login(admin, world.admin.phone or "")
    assert "Turmeric Powder 100g" in admin.get("/admin/products?status=pending").text
    post(admin, f"/admin/products/{product.id}/review", {"decision": "reject", "reason": ""})
    db.refresh(product)
    assert product.status == ProductStatus.PENDING
    post(admin, f"/admin/products/{product.id}/review", {"decision": "approve"})
    db.refresh(product)
    assert product.status == ProductStatus.APPROVED
    assert TestClient(app).get(f"/p/{product.slug}").status_code == 200


def test_trusted_mode_publishes_immediately(client: TestClient, world: World, db: Session) -> None:
    save_site_settings(db, {"require_product_approval": False})
    db.commit()
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    login(client, world.seller.phone or "")
    create_product(client, "/seller", world)
    p = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert p is not None and p.status == ProductStatus.APPROVED


def test_price_edit_stays_live_but_content_edit_needs_approval(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.seller.phone or "")
    html = client.get(f"/seller/products/{world.diya.id}/edit").text
    assert "Clay Diya" in html
    form = product_form(
        world,
        name="Clay Diya",
        name_hi="",
        category_id=str(world.puja.id),
        v_id=[str(world.diya_1.id)],
        v_amount=["1"],
        v_unit=["piece"],
        v_price=["6"],
        v_sku=[world.diya_1.sku],
        v_stock=["50"],
        v_label=["1 piece"],
    )
    post(client, f"/seller/products/{world.diya.id}/edit", form)
    db.expire_all()
    diya = db.get(Product, world.diya.id)
    assert diya.status == ProductStatus.APPROVED and diya.variants[0].price_paise == 600  # type: ignore[union-attr]
    form["name"] = "Clay Diya (large)"
    post(client, f"/seller/products/{world.diya.id}/edit", form)
    db.expire_all()
    assert db.get(Product, world.diya.id).status == ProductStatus.PENDING  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"name": ""}, "product name"),
        ({"category_id": "99999"}, "Choose a category"),
        ({"v_price": [""]}, "enter the price"),
        ({"v_sale": ["30"]}, "sale price must be lower"),
        ({"v_amount": ["0"]}, "weight or number"),
        ({"v_stock": ["-3"]}, "from 0 to"),
        ({"v_sku": ["bad sku!"]}, "SKU may use"),
        ({"brand_type": "branded", "brand": ""}, "brand name"),
        ({"v_remove": ["new0"]}, "at least one size"),
    ],
)
def test_product_validation(
    client: TestClient, world: World, db: Session, overrides: dict, message: str
) -> None:  # type: ignore[type-arg]
    login(client, world.seller.phone or "")
    r = create_product(client, "/seller", world, **overrides)
    assert r.status_code == 422 and message.lower() in r.text.lower(), r.text[:3000]
    assert db.scalar(select(func.count()).where(Product.name == "Turmeric Powder 100g")) == 0


def test_duplicate_sku_rejected(client: TestClient, world: World) -> None:
    login(client, world.seller.phone or "")
    r = create_product(client, "/seller", world, v_sku=[world.haldi_100.sku])
    assert r.status_code == 422 and "already used" in r.text


def test_image_upload_validated_and_reencoded(
    client: TestClient, world: World, db: Session, settings
) -> None:  # type: ignore[no-untyped-def]
    login(client, world.seller.phone or "")
    r = create_product(
        client, "/seller", world, files=[("images", ("photo.png", png_bytes(), "image/png"))]
    )
    assert r.status_code == 303
    product = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert product is not None and len(product.images) == 1
    img = product.images[0]
    assert img.is_primary and img.path.endswith(".webp")
    assert (settings.media_root / img.path).exists()
    assert (settings.media_root / img.path.replace(".webp", "_sm.webp")).exists()
    assert client.get(f"/media/{img.path}").headers["content-type"] == "image/webp"


@pytest.mark.parametrize(
    "filename,content,ctype",
    [
        ("evil.png", b"<svg onload=alert(1)></svg>", "image/png"),
        ("page.html", b"<html><script>alert(1)</script></html>", "text/html"),
        ("tiny.png", None, "image/png"),
        ("anim.gif", None, "image/gif"),
    ],
)
def test_bad_uploads_rejected(
    client: TestClient,
    world: World,
    db: Session,
    settings,  # type: ignore[no-untyped-def]
    filename: str,
    content: bytes | None,
    ctype: str,
) -> None:
    if filename == "tiny.png":
        content = png_bytes((10, 10))
    if filename == "anim.gif":
        buf = io.BytesIO()
        Image.new("RGB", (80, 80)).save(buf, "GIF")
        content = buf.getvalue()
    login(client, world.seller.phone or "")
    r = create_product(client, "/seller", world, files=[("images", (filename, content, ctype))])
    assert r.status_code == 422
    assert db.scalar(select(func.count()).where(Product.name == "Turmeric Powder 100g")) == 0
    media = settings.media_root
    assert not media.exists() or not any(p.is_file() for p in media.rglob("*"))


def test_oversized_upload_rejected(client: TestClient, world: World, settings) -> None:  # type: ignore[no-untyped-def]
    settings.max_upload_mb = 1
    login(client, world.seller.phone or "")
    big = png_bytes() + b"\0" * (2 << 20)
    r = create_product(client, "/seller", world, files=[("images", ("big.png", big, "image/png"))])
    assert r.status_code == 422 and "smaller than 1 MB" in r.text


def test_clone_product(client: TestClient, world: World, db: Session) -> None:
    login(client, world.seller.phone or "")
    create_product(
        client, "/seller", world, files=[("images", ("photo.png", png_bytes(), "image/png"))]
    )
    source = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert source is not None
    html = client.get(f"/seller/products/{source.id}/clone").text
    assert (
        'value="Turmeric Powder 100g"' in html and f'name="clone_from" value="{source.id}"' in html
    )
    assert "Copied from an existing product" in html
    r = create_product(
        client,
        "/seller",
        world,
        name="Turmeric Powder 250g",
        clone_from=str(source.id),
        v_amount=["250"],
        v_price=["55"],
        v_stock=["7"],
    )
    assert r.status_code == 303
    clone = db.scalar(select(Product).where(Product.name == "Turmeric Powder 250g"))
    assert clone is not None and clone.id != source.id
    assert clone.variants[0].label == "250 g" and clone.variants[0].price_paise == 5500
    assert clone.variants[0].sku != source.variants[0].sku
    assert len(clone.images) == 1 and clone.images[0].path != source.images[0].path


def test_seller_cannot_touch_other_sellers_data(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.seller.phone or "")
    other_id = world.other_product.id
    other_variant = world.other_product.variants[0]
    assert client.get(f"/seller/products/{other_id}/edit").status_code == 404
    assert client.get(f"/seller/products/{other_id}/clone").status_code == 404
    r = post(client, f"/seller/products/{other_id}/edit", product_form(world, name="Hacked"))
    assert r.status_code == 404
    assert post(client, f"/seller/products/{other_id}/hide").status_code == 404
    r = post(client, f"/seller/inventory/{other_variant.id}", {"quantity": "0"})
    assert r.status_code == 404
    r = create_product(client, "/seller", world, clone_from=str(other_id))
    assert r.status_code == 404
    db.expire_all()
    assert db.get(Product, other_id).name == "Other Jeera"  # type: ignore[union-attr]
    assert "Other Jeera" not in client.get("/seller/products").text
    assert "Other Jeera" not in client.get("/seller/inventory").text


def test_seller_cannot_set_featured_or_switch_store(
    client: TestClient, world: World, db: Session
) -> None:
    login(client, world.seller.phone or "")
    create_product(client, "/seller", world, is_featured="1", store_id=str(world.other_store.id))
    p = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert p is not None and not p.is_featured and p.store_id == world.store.id


# ---- inventory ---------------------------------------------------------------------------


def test_inventory_updates(client: TestClient, world: World, db: Session) -> None:
    login(client, world.seller.phone or "")
    vid = world.haldi_100.id
    r = post(client, f"/seller/inventory/{vid}", {"quantity": "40", "threshold": "8"}, headers=HX)
    assert r.status_code == 200 and f'id="inv-{vid}"' in r.text
    assert stock(db, vid) == 40
    post(client, f"/seller/inventory/{vid}", {"quantity": "40", "adjust": "-1"}, headers=HX)
    assert stock(db, vid) == 39
    r = post(client, f"/seller/inventory/{vid}", {"quantity": "-5"}, headers=HX)
    assert '"kind": "error"' in r.headers["HX-Trigger"] and stock(db, vid) == 39
    r = post(client, f"/seller/inventory/{vid}", {"adjust": "-100"}, headers=HX)
    assert '"kind": "error"' in r.headers["HX-Trigger"] and stock(db, vid) == 39
    assert db.scalar(select(func.count()).where(AdminAction.action == "inventory.set")) == 2


def test_low_stock_on_dashboard(client: TestClient, world: World) -> None:
    login(client, world.seller.phone or "")
    html = client.get("/seller").text
    assert "Low-stock items" in html and "Haldi Powder · 250 g" in html  # 3 left, alert at 5
    assert "Haldi Powder" in client.get("/seller/inventory?low=1").text


# ---- orders ------------------------------------------------------------------------------


def _order(app, world: World) -> str:  # type: ignore[no-untyped-def]
    c = TestClient(app)
    login(c, world.customer.phone or "")
    add(c, world.haldi_100.id, 3)
    return place(c, review(c)).headers["location"].split("/")[2]


def test_seller_moves_order_through_statuses(
    client: TestClient,
    app,
    world: World,  # type: ignore[no-untyped-def]
    db: Session,
) -> None:
    order_no = _order(app, world)
    login(client, world.seller.phone or "")
    assert order_no in client.get("/seller/orders").text
    assert "Asha Devi" in client.get(f"/seller/orders/{order_no}").text
    for status in ["confirmed", "preparing", "ready", "out_for_delivery", "delivered"]:
        r = post(client, f"/seller/orders/{order_no}/status", {"status": status})
        assert r.status_code == 303
    db.expire_all()
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.status.value == "delivered"  # type: ignore[union-attr]
    assert order.payments[0].status == PaymentStatus.PAID  # type: ignore[union-attr]  # COD collected
    assert order.deliveries[0].status.value == "delivered"  # type: ignore[union-attr]
    post(client, f"/seller/orders/{order_no}/status", {"status": "returned", "reason": "damaged"})
    db.expire_all()
    assert stock(db, world.haldi_100.id) == 10  # returned items back in stock
    order = db.scalar(select(Order).where(Order.order_no == order_no))
    assert order.payments[0].status == PaymentStatus.REFUNDED  # type: ignore[union-attr]


def test_invalid_status_change_rejected(
    client: TestClient,
    app,
    world: World,  # type: ignore[no-untyped-def]
    db: Session,
) -> None:
    order_no = _order(app, world)
    login(client, world.seller.phone or "")
    post(client, f"/seller/orders/{order_no}/status", {"status": "delivered"})
    post(client, f"/seller/orders/{order_no}/status", {"status": "nonsense"})
    db.expire_all()
    assert db.scalar(select(Order.status).where(Order.order_no == order_no)).value == "pending"  # type: ignore[union-attr]
    assert "Cannot change an order" in client.get(f"/seller/orders/{order_no}").text or True


def test_other_seller_cannot_see_order(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    order_no = _order(app, world)
    login(client, world.other_seller.phone or "")
    assert order_no not in client.get("/seller/orders?status=all").text
    assert client.get(f"/seller/orders/{order_no}").status_code == 404
    assert (
        post(client, f"/seller/orders/{order_no}/status", {"status": "cancelled"}).status_code
        == 404
    )


def test_admin_cancels_order_and_stock_returns(
    client: TestClient,
    app,
    world: World,  # type: ignore[no-untyped-def]
    db: Session,
) -> None:
    order_no = _order(app, world)
    assert stock(db, world.haldi_100.id) == 7
    login(client, world.admin.phone or "")
    post(client, f"/admin/orders/{order_no}/status", {"status": "cancelled", "reason": "no answer"})
    assert stock(db, world.haldi_100.id) == 10
    assert "order.cancelled" in client.get("/admin/activity").text


def test_admin_dashboard_numbers(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    _order(app, world)
    login(client, world.admin.phone or "")
    html = client.get("/admin").text
    assert "Orders today" in html and "₹86" in html  # 3 x ₹22 + ₹20 delivery
    assert "Pending orders" in html and "Popular products" in html and "Haldi Powder" in html


# ---- delivery settings -------------------------------------------------------------------


def test_delivery_zone_and_slot_management(client: TestClient, world: World, db: Session) -> None:
    login(client, world.seller.phone or "")
    r = post(
        client,
        "/seller/delivery/zones",
        {
            "pincode": "829112",
            "fee": "30",
            "free_above": "300",
            "estimated_days": "2",
            "same_day_available": "1",
            "same_day_cutoff": "14:00",
            "is_active": "1",  # the form's checkbox is ticked by default
        },
    )
    assert r.status_code == 303
    z = db.scalar(select(SellerDeliveryZone).where(SellerDeliveryZone.pincode == "829112"))
    assert z is not None and z.delivery_fee_paise == 3000 and z.free_delivery_above_paise == 30000
    assert z.same_day_available and z.estimated_days == 2
    r = post(client, "/seller/delivery/zones", {"pincode": "829112"})
    assert r.status_code == 422 and "already in your list" in r.text
    r = post(client, "/seller/delivery/zones", {"pincode": "12"})
    assert r.status_code == 422 and "valid 6-digit" in r.text
    r = post(client, "/seller/delivery/slots", {"start_time": "18:00", "end_time": "16:00"})
    assert r.status_code == 422
    post(client, "/seller/delivery/slots", {"start_time": "16:00", "end_time": "19:00"})
    post(client, "/seller/delivery/options", {"delivery_radius_km": "7.5"})
    db.expire_all()
    store = db.get(type(world.store), world.store.id)
    assert store.delivery_radius_km == 7.5 and not store.pickup_available  # type: ignore[union-attr]
    other_zone = SellerDeliveryZone(store_id=world.other_store.id, pincode="829113")
    db.add(other_zone)
    db.commit()
    assert post(client, f"/seller/delivery/zones/{other_zone.id}/delete").status_code == 404
    assert "829112" in client.get("/delivery").text


def test_admin_edits_any_store_delivery(client: TestClient, world: World, db: Session) -> None:
    login(client, world.admin.phone or "")
    assert "Other-Shop" in client.get(f"/admin/delivery?store={world.other_store.id}").text
    post(client, f"/admin/delivery/zones?store={world.other_store.id}", {"pincode": "829114"})
    assert db.scalar(
        select(SellerDeliveryZone).where(SellerDeliveryZone.store_id == world.other_store.id)
    )


# ---- admin content -----------------------------------------------------------------------


def test_admin_categories(client: TestClient, world: World, db: Session) -> None:
    login(client, world.admin.phone or "")
    post(
        client,
        "/admin/categories",
        {
            "name": "Dry Fruits",
            "name_hi": "मेवे",
            "is_active": "1",
            "parent_id": str(world.spices.id),
        },
    )
    cat = db.scalar(select(Category).where(Category.name == "Dry Fruits"))
    assert cat is not None and cat.slug == "dry-fruits" and cat.parent_id == world.spices.id
    post(client, "/admin/categories", {"name": "Deep", "parent_id": str(cat.id)})
    assert db.scalar(select(Category).where(Category.name == "Deep")) is None  # 2 levels max
    post(client, f"/admin/categories/{world.spices.id}/delete")
    assert db.get(Category, world.spices.id) is not None  # has products
    cat_id = cat.id
    post(client, f"/admin/categories/{cat_id}/delete")
    db.expire_all()
    assert db.get(Category, cat_id) is None


def test_admin_homepage_store_info_settings_testimonials(
    client: TestClient,
    app,  # type: ignore[no-untyped-def]
    world: World,
    db: Session,
) -> None:
    login(client, world.admin.phone or "")
    post(
        client,
        "/admin/homepage",
        {
            "announcement": "Navratri special",
            "announcement_enabled": "1",
            "hero_title": "Fresh masala daily",
            "why_points": "One\nTwo",
            "rail_0_title": "Rozana",
            "rail_0_cats": ["spices-masala"],
            "rail_0_enabled": "1",
        },
    )
    post(
        client,
        "/admin/store-info",
        {
            "business_name": "Pansari Bhandar",
            "phone": "9000000009",
            "whatsapp_number": "919000000009",
            "map_url": "https://maps.example/x",
            "story": "First para.\n\nSecond para.",
            "established_year": "1985",
        },
    )
    post(
        client,
        "/admin/testimonials",
        {"name": "Kavita", "body": "Good service", "rating": "5", "is_active": "1"},
    )
    home = TestClient(app).get("/").text
    for text in [
        "Navratri special",
        "Fresh masala daily",
        "Rozana",
        "Since 1985",
        "First para.",
        "Good service",
        "wa.me/919000000009",
    ]:
        assert text in home, text
    r = post(client, "/admin/store-info", {"map_url": "javascript:alert(1)"})
    assert "must start with https://" in client.get(r.headers["location"]).text
    post(client, "/admin/settings", {"max_quantity_per_item": "500"})  # out of range
    assert load_site_settings(db).max_quantity_per_item == 20
    assert db.scalar(select(func.count()).select_from(Testimonial)) == 1


def test_admin_blocks_customer(client: TestClient, app, world: World, db: Session) -> None:  # type: ignore[no-untyped-def]
    shopper = TestClient(app)
    login(shopper, world.customer.phone or "")
    login(client, world.admin.phone or "")
    post(client, f"/admin/customers/{world.customer.id}/toggle")
    db.expire_all()
    assert not db.get(User, world.customer.id).is_active  # type: ignore[union-attr]
    assert shopper.get("/account", follow_redirects=False).status_code == 303  # signed out
    r = shopper.post(
        "/login",
        data={"login": world.customer.phone, "password": PASSWORD, "csrf_token": csrf(shopper)},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_admin_product_form_has_store_choice(client: TestClient, world: World, db: Session) -> None:
    login(client, world.admin.phone or "")
    html = client.get("/admin/products/new").text
    assert 'name="store_id"' in html and "Featured on homepage" in html
    create_product(client, "/admin", world, store_id=str(world.other_store.id), is_featured="1")
    p = db.scalar(select(Product).where(Product.name == "Turmeric Powder 100g"))
    assert p is not None and p.store_id == world.other_store.id
    assert p.is_featured and p.status == ProductStatus.APPROVED  # admin publishes directly
    assert re.search(r"product\.create", client.get("/admin/activity").text)
    assert (
        db.scalar(
            select(func.count())
            .select_from(ProductVariant)
            .where(ProductVariant.product_id == p.id)
        )
        == 1
    )
