"""Seller dashboard: products, inventory, orders, store profile and delivery settings."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.db.models import SellerStatus, Store, User, UserRole
from app.services import accounts, seo
from app.services.checkout import normalise_phone
from app.services.delivery import valid_pincode
from app.web import manage
from app.web.deps import (
    RateLimited,
    client_ip,
    current_user,
    get_db,
    login_session,
    require_seller,
    require_seller_account,
    seller_store,
    touch_login,
)
from app.web.manage import Scope
from app.web.templating import flash, render, site_settings

router = APIRouter(prefix="/seller")


def scope(user: User = Depends(require_seller)) -> Scope:
    assert user.seller_profile and user.seller_profile.store
    return Scope("seller", user, user.seller_profile.store.id)


# ---- registration & approval -------------------------------------------------------------


@router.get("/register")
def register_page(request: Request, db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user and user.role == UserRole.SELLER:
        return RedirectResponse("/seller", status_code=303)
    meta = seo.Meta(
        title=seo.title("Sell with us", site_settings(request, db)), robots="noindex,follow"
    )
    return render(request, db, "seller/register.html", {"v": {}, "errors": {}}, meta=meta)


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    business_name: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    if not request.app.state.ratelimiter.hit(f"register:{client_ip(request)}", 10, 3600):
        raise RateLimited("Too many accounts created from this network. Try again later.")
    reg = accounts.Registration(full_name, phone, email, password[:256], business_name)
    try:
        user = accounts.register_seller(db, reg)
    except accounts.AccountError as exc:
        db.rollback()
        v = {
            "full_name": full_name[:120],
            "phone": phone[:15],
            "email": email[:254],
            "business_name": business_name[:160],
        }
        return render(
            request, db, "seller/register.html", {"v": v, "errors": exc.errors}, status_code=400
        )
    touch_login(user)
    login_session(request, user)
    db.commit()
    return RedirectResponse("/seller/pending", status_code=303)


@router.get("/pending")
def pending(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_seller_account)
) -> Response:
    profile = user.seller_profile
    assert profile is not None
    if profile.status == SellerStatus.APPROVED:
        return RedirectResponse("/seller", status_code=303)
    return render(request, db, "seller/pending.html", {"profile": profile})


# ---- dashboard ---------------------------------------------------------------------------


@router.get("")
def dashboard(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.dashboard_page(request, db, s, "seller/dashboard.html")


@router.get("/products")
def products(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.products_list(request, db, s)


@router.get("/products/new")
def new_product(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.new_product_page(request, db, s)


@router.post("/products/new")
async def create_product(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_product(request, db, s)


@router.get("/products/{product_id}/edit")
def edit_product(
    product_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.edit_product_page(request, db, s, product_id)


@router.post("/products/{product_id}/edit")
async def update_product(
    product_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_product(request, db, s, product_id)


@router.get("/products/{product_id}/clone")
def clone_product(
    product_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.clone_product_page(request, db, s, product_id)


@router.post("/products/{product_id}/{action}")
def product_visibility(
    product_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
) -> Response:
    return manage.set_product_status(request, db, s, product_id, action)


@router.get("/inventory")
def inventory(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.inventory_list(request, db, s)


@router.post("/inventory/{variant_id}")
async def update_stock(
    variant_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.update_stock(request, db, s, variant_id)


@router.get("/orders")
def orders(request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)) -> Response:
    return manage.orders_list(request, db, s)


@router.get("/orders/{order_no}")
def order_detail(
    order_no: str, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.order_detail(request, db, s, order_no)


@router.post("/orders/{order_no}/status")
async def order_status(
    order_no: str, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.change_order_status(request, db, s, order_no)


# ---- store profile -----------------------------------------------------------------------


@router.get("/store")
def store_profile(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return render(request, db, "seller/store.html", s.ctx(store=store, errors={}))


@router.post("/store")
async def save_store(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    form = await request.form()
    errors = update_store_from_form(store, form)
    if errors:
        db.rollback()
        return render(
            request, db, "seller/store.html", s.ctx(store=store, errors=errors), status_code=422
        )
    db.commit()
    request.app.state.cache.clear()
    flash(request, "Store profile saved.")
    return RedirectResponse("/seller/store", status_code=303)


def update_store_from_form(store: Store, form: object) -> dict[str, str]:
    """Shared with the admin's store screen."""
    get = form.get  # type: ignore[attr-defined]

    def text(key: str, limit: int) -> str | None:
        return str(get(key, "") or "").strip()[:limit] or None

    errors: dict[str, str] = {}
    name = text("name", 160)
    if not name:
        errors["name"] = "Enter the store name."
    pincode = text("pincode", 6)
    if pincode and not valid_pincode(pincode):
        errors["pincode"] = "Enter a valid 6-digit pincode."
    phone = normalise_phone(text("phone", 20))
    whatsapp = normalise_phone(text("whatsapp", 20))
    for key, value in (("phone", phone), ("whatsapp", whatsapp)):
        if value and len(value) != 10:
            errors[key] = "Enter a 10-digit mobile number."
    coords: dict[str, Decimal | None] = {}
    for key, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
        raw = text(key, 20)
        try:
            coords[key] = Decimal(raw) if raw else None
            if coords[key] is not None and not lo <= coords[key] <= hi:  # type: ignore[operator]
                raise InvalidOperation
        except InvalidOperation:
            errors[key] = f"Enter a valid {key}."
    if errors:
        return errors
    store.name = name or store.name
    store.name_hi = text("name_hi", 160)
    store.description = text("description", 3000)
    store.phone, store.whatsapp = phone or None, whatsapp or None
    store.address_line, store.area = text("address_line", 240), text("area", 120)
    store.city = text("city", 80) or "Gomia"
    store.pincode = pincode
    store.latitude, store.longitude = coords["latitude"], coords["longitude"]
    return {}


# ---- delivery ----------------------------------------------------------------------------


@router.get("/delivery")
def delivery(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return manage.delivery_page(request, db, s, store)


@router.post("/delivery/options")
async def delivery_options(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return await manage.save_delivery_options(request, db, s, store)


@router.post("/delivery/zones")
async def add_zone(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return await manage.save_zone(request, db, s, store)


@router.post("/delivery/zones/{zone_id}")
async def edit_zone(
    zone_id: int,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return await manage.save_zone(request, db, s, store, zone_id)


@router.post("/delivery/zones/{zone_id}/delete")
def remove_zone(
    zone_id: int,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return manage.delete_zone(request, db, s, store, zone_id)


@router.post("/delivery/slots")
async def add_slot(
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return await manage.save_slot(request, db, s, store)


@router.post("/delivery/slots/{slot_id}")
async def edit_slot(
    slot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return await manage.save_slot(request, db, s, store, slot_id)


@router.post("/delivery/slots/{slot_id}/delete")
def remove_slot(
    slot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
    store: Store = Depends(seller_store),
) -> Response:
    return manage.delete_slot(request, db, s, store, slot_id)
