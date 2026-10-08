"""Security regression tests (see docs/security.md for the audit these come from)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import hash_password, verify_password
from app.db.models import Product, User
from tests.web.conftest import HX, PASSWORD, World, csrf, login, post

# ---- CSRF --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,data",
    [
        ("/cart/add", {"variant_id": "1", "qty": "1"}),
        ("/login", {"login": "9000000001", "password": PASSWORD}),
        ("/register", {"full_name": "X", "phone": "9111111111", "password": "abcdefgh1"}),
        ("/logout", {}),
        ("/checkout/place", {}),
    ],
)
def test_post_without_csrf_token_is_rejected(
    client: TestClient, world: World, path: str, data: dict
) -> None:  # type: ignore[type-arg]
    client.get("/")
    r = client.post(path, data=data, follow_redirects=False)
    assert r.status_code == 403 and "Session expired" in r.text


def test_wrong_csrf_token_and_htmx_header(client: TestClient, world: World) -> None:
    client.get("/")
    r = client.post(
        "/cart/add",
        data={"variant_id": str(world.haldi_100.id), "csrf_token": "forged"},
        follow_redirects=False,
    )
    assert r.status_code == 403
    token = csrf(client)
    r = client.post(
        "/cart/add",
        data={"variant_id": str(world.haldi_100.id)},
        headers={**HX, "X-CSRF-Token": token},
    )
    assert r.status_code == 200 and "Added to cart" in r.headers["HX-Trigger"]
    r = client.post(
        "/cart/add",
        data={"variant_id": str(world.haldi_100.id)},
        headers={**HX, "X-CSRF-Token": "forged"},
    )
    assert r.status_code == 403 and "HX-Trigger" in r.headers


def test_multipart_form_needs_csrf(client: TestClient, world: World) -> None:
    login(client, world.seller.phone or "")
    r = client.post(
        "/seller/products/new",
        data={"name": "X"},
        files=[("images", ("a.png", b"x", "image/png"))],
        follow_redirects=False,
    )
    assert r.status_code == 403


# ---- authentication & sessions -----------------------------------------------------------


def test_login_errors_do_not_reveal_accounts(client: TestClient, world: World) -> None:
    unknown = post(client, "/login", {"login": "9999999999", "password": "whatever1"})
    wrong = post(client, "/login", {"login": world.customer.phone, "password": "whatever1"})
    assert unknown.status_code == wrong.status_code == 400
    msg = "Mobile number / email or password is not correct."
    assert msg in unknown.text and msg in wrong.text


def test_login_is_rate_limited(client: TestClient, world: World) -> None:
    for _ in range(6):
        post(client, "/login", {"login": world.customer.phone, "password": "bad-pass-1"})
    r = post(client, "/login", {"login": world.customer.phone, "password": PASSWORD})
    assert r.status_code == 429


def test_session_is_regenerated_on_login(client: TestClient, world: World) -> None:
    before = csrf(client)
    login(client, world.customer.phone or "")
    assert csrf(client) != before


def test_logout_ends_session(client: TestClient, world: World) -> None:
    login(client, world.customer.phone or "")
    assert client.get("/account").status_code == 200
    post(client, "/logout")
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_password_change_signs_out_other_devices(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    phone = world.customer.phone or ""
    other_device = TestClient(app)
    login(other_device, phone)
    login(client, phone)
    post(
        client,
        "/account/password",
        {"current_password": PASSWORD, "new_password": "brand-new-pass-9"},
    )
    assert client.get("/account").status_code == 200  # this device stays signed in
    assert other_device.get("/account", follow_redirects=False).status_code == 303


def test_tampered_session_cookie_is_ignored(client: TestClient, world: World) -> None:
    login(client, world.customer.phone or "")
    cookie = client.cookies.get("pb_session")
    assert cookie
    client.cookies.set("pb_session", cookie[:-4] + "AAAA")
    assert client.get("/account", follow_redirects=False).status_code == 303


def test_weak_passwords_rejected(client: TestClient, world: World) -> None:
    for pw in ["short1", "12345678901"]:
        r = post(client, "/register", {"full_name": "Raju", "phone": "9111111111", "password": pw})
        assert r.status_code == 400


def test_passwords_are_hashed(db: Session, world: World) -> None:
    user = db.get(User, world.customer.id)
    assert user is not None and user.password_hash.startswith("$argon2")
    assert PASSWORD not in user.password_hash
    assert verify_password(hash_password("x-y-z-1234"), "x-y-z-1234")
    assert not verify_password("!", "anything")  # unusable demo hashes never match


def test_duplicate_phone_registration(client: TestClient, world: World) -> None:
    r = post(
        client,
        "/register",
        {"full_name": "Dup", "phone": world.customer.phone, "password": "abcdefgh1"},
    )
    assert r.status_code == 400 and "already exists" in r.text


# ---- redirects, XSS, injection -----------------------------------------------------------


@pytest.mark.parametrize("target", ["https://evil.test/", "//evil.test", "/\\evil.test"])
def test_login_next_cannot_redirect_off_site(client: TestClient, world: World, target: str) -> None:
    r = post(
        client, "/login", {"login": world.customer.phone, "password": PASSWORD, "next": target}
    )
    assert r.headers["location"] == "/"


def test_user_content_is_escaped(client: TestClient, world: World, db: Session) -> None:
    payload = '<script>alert("x")</script><img src=x onerror=alert(1)>'
    product = db.get(Product, world.haldi.id)
    assert product is not None
    product.short_description = payload
    product.description = payload
    product.product_info = {"ingredients": payload}
    db.commit()
    html = client.get("/p/haldi-powder").text
    assert "<script>alert" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in html
    html = client.get("/search?q=" + payload).text
    assert "<script>alert" not in html


def test_json_ld_cannot_break_out_of_script(client: TestClient, world: World, db: Session) -> None:
    product = db.get(Product, world.haldi.id)
    assert product is not None
    product.short_description = "</script><script>alert(1)</script>"
    db.commit()
    html = client.get("/p/haldi-powder").text
    assert "</script><script>alert(1)" not in html


@pytest.mark.parametrize(
    "q", ["' OR 1=1 --", "%' UNION SELECT password_hash FROM users --", "\\", "_"]
)
def test_search_is_injection_safe(client: TestClient, world: World, q: str) -> None:
    r = client.get("/search", params={"q": q})
    assert r.status_code == 200 and "$argon2" not in r.text


def test_numeric_route_params_validated(client: TestClient, world: World) -> None:
    login(client, world.admin.phone or "")
    assert client.get("/admin/products/abc/edit").status_code == 400
    assert client.get("/admin/products/99999/edit").status_code == 404


# ---- headers, limits, errors -------------------------------------------------------------


def test_security_headers(client: TestClient, world: World) -> None:
    r = client.get("/")
    h = r.headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert "script-src 'self'" in h["content-security-policy"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-content-type-options"] == "nosniff"
    assert h["x-frame-options"] == "DENY"
    assert h["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "x-request-id" in h
    login(client, world.customer.phone or "")
    assert client.get("/account").headers["cache-control"] == "no-store"


def test_session_cookie_flags(client: TestClient, world: World) -> None:
    r = client.get("/login")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_large_bodies_rejected(client: TestClient, world: World) -> None:
    r = client.post(
        "/cart/add",
        content=b"x" * (60 << 20),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


def test_errors_do_not_leak_internals(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    @app.get("/boom-test")
    def boom() -> None:
        raise RuntimeError("secret internal detail")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/boom-test")
    assert r.status_code == 500
    assert "secret internal detail" not in r.text and "Traceback" not in r.text


def test_api_docs_disabled(client: TestClient) -> None:
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404


# ---- production configuration ------------------------------------------------------------


def test_production_settings_refuse_unsafe_values() -> None:
    base = {
        "environment": "production",
        "allowed_hosts": "shop.example.in",
        "secret_key": "x" * 40,
        "_env_file": None,
    }
    Settings(**base)  # type: ignore[arg-type]
    for bad in [
        {"debug": True},
        {"secret_key": "short"},
        {"allowed_hosts": "*"},
        {"secret_key": "dev-only-insecure-secret-key-change-me"},
    ]:
        with pytest.raises(ValueError):
            Settings(**{**base, **bad})  # type: ignore[arg-type]


def test_railway_database_url_normalised() -> None:
    s = Settings(database_url="postgresql://u:p@host:5432/db", _env_file=None)  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+psycopg://")
    s = Settings(database_url="postgres://u:p@host/db", _env_file=None)  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+psycopg://")


def test_disabled_admin_loses_access(client: TestClient, world: World, db: Session) -> None:
    login(client, world.admin.phone or "")
    assert client.get("/admin").status_code == 200
    admin = db.scalar(select(User).where(User.id == world.admin.id))
    assert admin is not None
    admin.is_active = False
    db.commit()
    assert client.get("/admin", follow_redirects=False).status_code == 303


# ---- regressions for the security audit (docs/security.md, findings 1-14) ----------------


def test_f1_login_limit_cannot_be_bypassed_with_phone_variants(
    client: TestClient, world: World
) -> None:
    phone = world.customer.phone or ""
    for prefix in ["a", "b", "c", "d", "e", "f"]:
        post(client, "/login", {"login": f"{prefix}{phone}", "password": "wrong-pass-1"})
    r = post(client, "/login", {"login": f"+91 {phone[:5]} {phone[5:]}", "password": PASSWORD})
    assert r.status_code == 429


def test_f2_client_ip_ignores_spoofed_forwarded_for(app, world: World) -> None:  # type: ignore[no-untyped-def]
    from starlette.requests import Request

    from app.web.deps import client_ip

    def req(xff: str) -> Request:
        return Request(
            {
                "type": "http",
                "app": app,
                "headers": [(b"x-forwarded-for", xff.encode())],
                "client": ("10.0.0.9", 1234),
            }
        )

    app.state.settings.trusted_proxy_hops = 0
    assert client_ip(req("1.2.3.4")) == "10.0.0.9"  # no proxy configured: header ignored
    app.state.settings.trusted_proxy_hops = 1
    assert client_ip(req("1.2.3.4, 203.0.113.7")) == "203.0.113.7"  # left entry is spoofed


def test_f2_spoofed_forwarded_for_does_not_reset_login_limit(
    client: TestClient,
    app,  # type: ignore[no-untyped-def]
    world: World,
) -> None:
    app.state.settings.trusted_proxy_hops = 1
    for i in range(21):
        token = csrf(client)
        client.post(
            "/login",
            data={"login": f"98765{i:05d}", "password": "x-wrong-1", "csrf_token": token},
            headers={"X-Forwarded-For": f"10.9.{i}.1, 198.51.100.4"},
        )
    token = csrf(client)
    r = client.post(
        "/login",
        data={"login": "9876599999", "password": "x-wrong-1", "csrf_token": token},
        headers={"X-Forwarded-For": "10.10.10.10, 198.51.100.4"},
    )
    assert r.status_code == 429


def test_f3_status_change_on_stale_order_is_refused(world: World, db: Session) -> None:
    """Two requests that both loaded the order as 'pending': only the first may cancel."""
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Inventory, Order, OrderItem
    from app.services import orders as order_service

    order = Order(
        order_no="PB-RACE", user_id=world.customer.id, subtotal_paise=2200, total_paise=2200
    )
    db.add(order)
    db.flush()
    db.add(
        OrderItem(
            order_id=order.id,
            store_id=world.store.id,
            product_id=world.haldi.id,
            variant_id=world.haldi_100.id,
            product_name="Haldi",
            variant_label="100 g",
            unit_price_paise=2200,
            quantity=1,
            line_total_paise=2200,
        )
    )
    db.commit()
    maker = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    with maker() as s1, maker() as s2:
        o1 = order_service.load_order(s1, "PB-RACE")
        o2 = order_service.load_order(s2, "PB-RACE")
        assert o1 and o2
        order_service.change_status(s1, o1, order_service.S.CANCELLED, None, "first")
        s1.commit()
        with pytest.raises(order_service.OrderError):
            order_service.change_status(s2, o2, order_service.S.CANCELLED, None, "second")
    db.expire_all()
    assert db.get(Inventory, world.haldi_100.id).quantity == 11  # type: ignore[union-attr]  # once


def test_f4_clone_from_cannot_copy_another_sellers_images(
    client: TestClient, world: World, db: Session, settings
) -> None:  # type: ignore[no-untyped-def]
    from app.db.models import ProductImage
    from tests.web.test_dashboards import create_product, png_bytes

    other = TestClient(client.app)
    login(other, world.other_seller.phone or "")
    create_product(
        other,
        "/seller",
        world,
        name="Secret Draft",
        files=[("images", ("p.png", png_bytes(), "image/png"))],
        action="draft",
    )
    secret = db.scalar(select(Product).where(Product.name == "Secret Draft"))
    assert secret is not None
    login(client, world.seller.phone or "")
    for value in [f"000000{secret.id}", f"{secret.id:012d}", str(secret.id)]:
        r = create_product(client, "/seller", world, clone_from=value)
        assert r.status_code in (303, 404)
    mine = db.scalars(
        select(Product).where(
            Product.store_id == world.store.id, Product.name == "Turmeric Powder 100g"
        )
    ).all()
    ids = [p.id for p in mine]
    if ids:
        assert db.scalar(select(ProductImage).where(ProductImage.product_id.in_(ids))) is None


@pytest.mark.parametrize(
    "path", ["/shop?page=²", "/p/haldi-powder?v=²", "/search?q=x&page=¹", "/c/spices-masala?page=٣"]
)
def test_f5_unicode_digits_do_not_crash(client: TestClient, world: World, path: str) -> None:
    assert client.get(path).status_code == 200


def test_f5_admin_unicode_digits(client: TestClient, world: World) -> None:
    login(client, world.admin.phone or "")
    assert client.get("/admin/delivery?store=²").status_code == 200
    r = post(client, f"/admin/inventory/{world.haldi_100.id}", {"adjust": "+-5"}, headers=HX)
    assert "whole number" in r.headers["HX-Trigger"]


def test_f6_stock_adjust_is_relative_to_current_value(
    client: TestClient, world: World, db: Session
) -> None:
    from app.db.models import Inventory

    login(client, world.seller.phone or "")
    # The form was rendered when stock was 10; a checkout then took 4.
    db.get(Inventory, world.haldi_100.id).quantity = 6  # type: ignore[union-attr]
    db.commit()
    post(
        client,
        f"/seller/inventory/{world.haldi_100.id}",
        {"quantity": "10", "adjust": "+1"},
        headers=HX,
    )
    db.expire_all()
    assert db.get(Inventory, world.haldi_100.id).quantity == 7  # type: ignore[union-attr]


def test_f7_huge_image_dimensions_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import io

    from PIL import Image

    from app.services.images import ImageError, save_upload

    buf = io.BytesIO()
    Image.new("1", (9000, 9000)).save(buf, "PNG")  # tiny file, 81 megapixels
    with pytest.raises(ImageError):
        save_upload(buf.getvalue(), tmp_path, 10 << 20)


def test_f8_label_change_needs_reapproval(client: TestClient, world: World, db: Session) -> None:
    from tests.web.test_dashboards import product_form

    login(client, world.seller.phone or "")
    form = product_form(
        world,
        name="Clay Diya",
        name_hi="",
        category_id=str(world.puja.id),
        v_id=[str(world.diya_1.id)],
        v_amount=["1"],
        v_unit=["piece"],
        v_price=["5"],
        v_sku=[world.diya_1.sku],
        v_stock=["50"],
        v_label=["Big premium diya"],
    )
    post(client, f"/seller/products/{world.diya.id}/edit", form)
    db.expire_all()
    assert db.get(Product, world.diya.id).status.value == "pending"  # type: ignore[union-attr]


def test_f9_seller_sees_only_own_totals_on_multi_shop_order(
    client: TestClient, world: World, db: Session
) -> None:
    from app.db.models import Order, OrderItem

    order = Order(
        order_no="PB-MULTI", user_id=world.customer.id, subtotal_paise=6700, total_paise=6700
    )
    db.add(order)
    db.flush()
    for store, product, price in [
        (world.store, world.haldi, 2200),
        (world.other_store, world.other_product, 4500),
    ]:
        db.add(
            OrderItem(
                order_id=order.id,
                store_id=store.id,
                product_id=product.id,
                product_name=product.name,
                variant_label="100 g",
                unit_price_paise=price,
                quantity=1,
                line_total_paise=price,
            )
        )
    db.commit()
    login(client, world.seller.phone or "")
    html = client.get("/seller/orders/PB-MULTI").text
    assert "Your items" in html and "₹22" in html
    assert "₹67" not in html and "Other Jeera" not in html


def test_f10_small_body_limit_outside_upload_routes(client: TestClient, world: World) -> None:
    r = client.post(
        "/login",
        content=b"x" * (300 << 10),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 413


def test_f11_placeholder_secret_refused_in_production() -> None:
    with pytest.raises(ValueError):
        Settings(
            environment="production",
            allowed_hosts="x.in",  # type: ignore[call-arg]
            secret_key="change-me-to-a-long-random-value-in-every-environment",
            _env_file=None,
        )


def test_f12_staff_sessions_expire(client: TestClient, app, world: World) -> None:  # type: ignore[no-untyped-def]
    login(client, world.admin.phone or "")
    assert client.get("/admin").status_code == 200
    app.state.settings.staff_session_hours = 0
    import time

    time.sleep(1.1)
    assert client.get("/admin", follow_redirects=False).status_code == 303
    shopper = TestClient(app)
    login(shopper, world.customer.phone or "")
    assert shopper.get("/account").status_code == 200  # customers keep long sessions


def test_f14_pages_are_not_shared_cacheable(client: TestClient, world: World) -> None:
    assert client.get("/").headers["cache-control"] == "private, no-cache"
