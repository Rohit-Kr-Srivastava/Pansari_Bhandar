"""Admin dashboard. Every route requires an active admin account (require_admin)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.db.models import (
    AdminAction,
    Category,
    Product,
    ProductStatus,
    SellerProfile,
    SellerStatus,
    Store,
    Testimonial,
    User,
    UserRole,
)
from app.services import accounts
from app.services import images as image_service
from app.services.product_editor import slugify, unique_slug
from app.services.settings import HomeRail, load_site_settings, save_site_settings
from app.web import manage
from app.web.deps import NotFound, client_ip, get_db, require_admin
from app.web.manage import Scope
from app.web.seller import update_store_from_form
from app.web.templating import flash, render

router = APIRouter(prefix="/admin")


def scope(user: User = Depends(require_admin)) -> Scope:
    return Scope("admin", user, None)


def audit(
    db: Session,
    request: Request,
    admin: User,
    action: str,
    entity_type: str,
    entity_id: int | None,
    before: Any = None,
    after: Any = None,
    reason: str | None = None,
) -> None:
    db.add(
        AdminAction(
            actor_id=admin.id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            reason=reason,
            ip_address=client_ip(request),
        )
    )


def _text(form: Any, key: str, limit: int) -> str:
    return str(form.get(key, "") or "").strip()[:limit]


def _back(url: str) -> Response:
    return RedirectResponse(url, status_code=303)


# ---- dashboard ---------------------------------------------------------------------------


@router.get("")
def dashboard(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.dashboard_page(request, db, s, "admin/dashboard.html")


@router.get("/activity")
def activity(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    rows = db.execute(
        select(AdminAction, User.full_name)
        .outerjoin(User, User.id == AdminAction.actor_id)
        .order_by(AdminAction.created_at.desc())
        .limit(200)
    ).all()
    return render(request, db, "admin/activity.html", s.ctx(rows=rows))


# ---- sellers -----------------------------------------------------------------------------


@router.get("/sellers")
def sellers(request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)) -> Response:
    status = request.query_params.get("status", "")
    stmt = select(SellerProfile).options(
        selectinload(SellerProfile.user), selectinload(SellerProfile.store)
    )
    if status in {x.value for x in SellerStatus}:
        stmt = stmt.where(SellerProfile.status == status)
    profiles = db.scalars(stmt.order_by(SellerProfile.created_at.desc()).limit(200)).all()
    return render(
        request,
        db,
        "admin/sellers.html",
        s.ctx(profiles=profiles, status=status, statuses=list(SellerStatus)),
    )


@router.post("/sellers/{profile_id}/{action}")
async def seller_action(
    profile_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
) -> Response:
    form = await request.form()
    profile = db.scalar(
        select(SellerProfile)
        .where(SellerProfile.id == profile_id)
        .options(selectinload(SellerProfile.store))
    )
    if profile is None:
        raise NotFound()
    reason = _text(form, "reason", 500) or None
    before = profile.status.value
    if action in ("approve", "reinstate"):
        profile.status = SellerStatus.APPROVED
        if profile.store:
            profile.store.is_active = True
    elif action == "reject":
        if not reason:
            flash(request, "Give a reason so the seller knows what to fix.", "error")
            return _back("/admin/sellers")
        profile.status = SellerStatus.REJECTED
        profile.rejection_reason = reason
        if profile.store:
            profile.store.is_active = False
    elif action == "suspend":
        profile.status = SellerStatus.SUSPENDED
        if profile.store:
            profile.store.is_active = False
    else:
        raise NotFound()
    profile.reviewed_by_id = s.user.id
    profile.reviewed_at = datetime.now(UTC)
    audit(
        db,
        request,
        s.user,
        f"seller.{action}",
        "seller_profile",
        profile.id,
        {"status": before},
        {"status": profile.status.value},
        reason,
    )
    db.commit()
    request.app.state.cache.clear()
    flash(request, f"{profile.business_name}: {profile.status.value}.")
    return _back("/admin/sellers")


# ---- customers ---------------------------------------------------------------------------


@router.get("/customers")
def customers(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    q = request.query_params.get("q", "")
    users = accounts.search_users(db, UserRole.CUSTOMER, q, limit=100)
    return render(request, db, "admin/customers.html", s.ctx(users=users, q=q[:60]))


@router.post("/customers/{user_id}/toggle")
def toggle_customer(
    user_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    user = db.get(User, user_id)
    if user is None or user.role != UserRole.CUSTOMER:
        raise NotFound()
    user.is_active = not user.is_active
    audit(
        db,
        request,
        s.user,
        "customer.activate" if user.is_active else "customer.deactivate",
        "user",
        user.id,
    )
    db.commit()
    flash(request, f"{user.full_name} {'activated' if user.is_active else 'deactivated'}.")
    return _back("/admin/customers")


# ---- products ----------------------------------------------------------------------------


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


@router.post("/products/{product_id}/review")
async def review_product(
    product_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    form = await request.form()
    product = db.get(Product, product_id)
    if product is None:
        raise NotFound()
    decision = _text(form, "decision", 20)
    reason = _text(form, "reason", 500)
    before = product.status.value
    if decision == "approve":
        product.status = ProductStatus.APPROVED
        product.rejection_reason = None
    elif decision == "reject":
        if not reason:
            flash(request, "Give a reason so the seller knows what to fix.", "error")
            return _back(f"/admin/products?status={before}")
        product.status = ProductStatus.REJECTED
        product.rejection_reason = reason
    elif decision in ("feature", "unfeature"):
        product.is_featured = decision == "feature"
    else:
        raise NotFound()
    product.reviewed_by_id = s.user.id
    product.reviewed_at = datetime.now(UTC)
    audit(
        db,
        request,
        s.user,
        f"product.{decision}",
        "product",
        product.id,
        {"status": before},
        {"status": product.status.value, "featured": product.is_featured},
        reason or None,
    )
    db.commit()
    request.app.state.cache.clear()
    flash(request, f"{product.name}: saved.")
    return _back(
        str(form.get("back") or "/admin/products")
        if str(form.get("back", "")).startswith("/admin/")
        else "/admin/products"
    )


@router.post("/products/{product_id}/{action}")
def product_visibility(
    product_id: int,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    s: Scope = Depends(scope),
) -> Response:
    return manage.set_product_status(request, db, s, product_id, action)


# ---- categories --------------------------------------------------------------------------


@router.get("/categories")
def categories(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    cats = db.scalars(
        select(Category).order_by(
            Category.parent_id.is_not(None), Category.sort_order, Category.name
        )
    ).all()
    counts: dict[int, int] = {
        category_id: n
        for category_id, n in db.execute(
            select(Product.category_id, func.count()).group_by(Product.category_id)
        )
    }
    tops = [c for c in cats if c.parent_id is None]
    return render(
        request, db, "admin/categories.html", s.ctx(cats=cats, tops=tops, counts=counts, errors={})
    )


def _category_values(db: Session, form: Any, cat: Category | None) -> tuple[dict[str, Any], str]:
    name = _text(form, "name", 120)
    if len(name) < 2:
        return {}, "Enter the category name."
    parent_raw = _text(form, "parent_id", 12)
    parent = db.get(Category, int(parent_raw)) if parent_raw.isdecimal() else None
    if parent is not None and (parent.parent_id is not None or (cat and parent.id == cat.id)):
        return {}, "Subcategories can only sit under a top-level category."
    if cat is not None and parent is not None and cat.children:
        return {}, "A category with subcategories cannot become a subcategory."
    sort_raw = _text(form, "sort_order", 6)
    return {
        "name": name,
        "name_hi": _text(form, "name_hi", 120) or None,
        "description": _text(form, "description", 1000) or None,
        "parent_id": parent.id if parent else None,
        "sort_order": int(sort_raw) if sort_raw.isdecimal() else 0,
        "is_active": form.get("is_active") in {"1", "on"},
    }, ""


@router.post("/categories")
async def create_category(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    form = await request.form()
    values, error = _category_values(db, form, None)
    if error:
        flash(request, error, "error")
        return _back("/admin/categories")
    slug = unique_slug(db, Category, slugify(_text(form, "slug", 80) or values["name"]))
    cat = Category(slug=slug, **values)
    db.add(cat)
    db.flush()
    audit(db, request, s.user, "category.create", "category", cat.id, after={"name": cat.name})
    db.commit()
    request.app.state.cache.clear()
    flash(request, f"Category {cat.name} added.")
    return _back("/admin/categories")


@router.post("/categories/{cat_id}")
async def update_category(
    cat_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    form = await request.form()
    cat = db.scalar(
        select(Category).where(Category.id == cat_id).options(selectinload(Category.children))
    )
    if cat is None:
        raise NotFound()
    values, error = _category_values(db, form, cat)
    if error:
        flash(request, error, "error")
        return _back("/admin/categories")
    before = {"name": cat.name, "is_active": cat.is_active}
    for k, v in values.items():
        setattr(cat, k, v)
    audit(
        db,
        request,
        s.user,
        "category.update",
        "category",
        cat.id,
        before,
        {"name": cat.name, "is_active": cat.is_active},
    )
    db.commit()
    request.app.state.cache.clear()
    flash(request, f"Category {cat.name} saved.")
    return _back("/admin/categories")


@router.post("/categories/{cat_id}/delete")
def delete_category(
    cat_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    cat = db.scalar(
        select(Category).where(Category.id == cat_id).options(selectinload(Category.children))
    )
    if cat is None:
        raise NotFound()
    in_use = db.scalar(select(func.count()).where(Product.category_id == cat.id))
    if in_use or cat.children:
        flash(request, "This category has products or subcategories. Hide it instead.", "error")
        return _back("/admin/categories")
    audit(db, request, s.user, "category.delete", "category", cat.id, {"name": cat.name})
    db.delete(cat)
    db.commit()
    request.app.state.cache.clear()
    flash(request, "Category deleted.")
    return _back("/admin/categories")


# ---- orders & inventory ------------------------------------------------------------------


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


# ---- delivery (any store) ----------------------------------------------------------------


def _store(db: Session, request: Request) -> Store:
    raw = request.query_params.get("store", "")
    store = db.get(Store, int(raw)) if raw.isdecimal() else None
    store = store or db.scalar(select(Store).order_by(Store.is_active.desc(), Store.id).limit(1))
    if store is None:
        raise NotFound()
    return store


@router.get("/delivery")
def delivery(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.delivery_page(request, db, s, _store(db, request))


@router.post("/delivery/options")
async def delivery_options(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_delivery_options(request, db, s, _store(db, request))


@router.post("/delivery/zones")
async def add_zone(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_zone(request, db, s, _store(db, request))


@router.post("/delivery/zones/{zone_id}")
async def edit_zone(
    zone_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_zone(request, db, s, _store(db, request), zone_id)


@router.post("/delivery/zones/{zone_id}/delete")
def remove_zone(
    zone_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.delete_zone(request, db, s, _store(db, request), zone_id)


@router.post("/delivery/slots")
async def add_slot(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_slot(request, db, s, _store(db, request))


@router.post("/delivery/slots/{slot_id}")
async def edit_slot(
    slot_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return await manage.save_slot(request, db, s, _store(db, request), slot_id)


@router.post("/delivery/slots/{slot_id}/delete")
def remove_slot(
    slot_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return manage.delete_slot(request, db, s, _store(db, request), slot_id)


@router.get("/stores/{store_id}")
def store_edit(
    store_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    store = db.get(Store, store_id)
    if store is None:
        raise NotFound()
    return render(
        request,
        db,
        "seller/store.html",
        s.ctx(store=store, errors={}, action=f"/admin/stores/{store.id}"),
    )


@router.post("/stores/{store_id}")
async def store_save(
    store_id: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    store = db.get(Store, store_id)
    if store is None:
        raise NotFound()
    errors = update_store_from_form(store, await request.form())
    if errors:
        db.rollback()
        return render(
            request,
            db,
            "seller/store.html",
            s.ctx(store=store, errors=errors, action=f"/admin/stores/{store.id}"),
            status_code=422,
        )
    audit(db, request, s.user, "store.update", "store", store.id)
    db.commit()
    request.app.state.cache.clear()
    flash(request, "Store saved.")
    return _back(f"/admin/stores/{store.id}")


# ---- site content & settings -------------------------------------------------------------


def _save_settings(
    request: Request, db: Session, s: Scope, changes: dict[str, Any], back: str, action: str
) -> Response:
    try:
        save_site_settings(db, changes)
    except ValidationError as exc:
        db.rollback()
        first = exc.errors()[0]
        field = ".".join(str(x) for x in first.get("loc", []))
        flash(request, f"Please check {field or 'the form'}: {first.get('msg', '')}", "error")
        return _back(back)
    audit(db, request, s.user, action, "site_settings", None, after=list(changes))
    db.commit()
    request.app.state.cache.clear()
    flash(request, "Saved.")
    return _back(back)


def _lines(value: str, limit: int = 12) -> list[str]:
    return [ln.strip()[:200] for ln in value.splitlines() if ln.strip()][:limit]


@router.get("/homepage")
def homepage(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    cats = db.scalars(select(Category).order_by(Category.sort_order, Category.name)).all()
    return render(
        request, db, "admin/homepage.html", s.ctx(cfg=load_site_settings(db), categories=cats)
    )


@router.post("/homepage")
async def save_homepage(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    form = await request.form()
    current = load_site_settings(db)
    rails = []
    for i, rail in enumerate(current.rails):
        rails.append(
            HomeRail(
                key=rail.key,
                title=_text(form, f"rail_{i}_title", 60) or rail.title,
                title_hi=_text(form, f"rail_{i}_title_hi", 60),
                category_slugs=[str(x)[:80] for x in form.getlist(f"rail_{i}_cats")][:6],
                seasonal_only=form.get(f"rail_{i}_seasonal") in {"1", "on"},
                enabled=form.get(f"rail_{i}_enabled") in {"1", "on"},
            ).model_dump()
        )
    changes: dict[str, Any] = {
        "announcement": _text(form, "announcement", 160),
        "announcement_enabled": form.get("announcement_enabled") in {"1", "on"},
        "hero_title": _text(form, "hero_title", 120) or current.hero_title,
        "hero_subtitle": _text(form, "hero_subtitle", 240),
        "why_points": _lines(_text(form, "why_points", 2000), 6),
        "rails": rails,
    }
    upload = form.get("hero_image")
    if upload is not None and getattr(upload, "filename", None):
        settings = request.app.state.settings
        try:
            data = await upload.read((settings.max_upload_mb << 20) + 1)  # type: ignore[union-attr]
            changes["hero_image"] = image_service.save_upload(
                data, settings.media_root, settings.max_upload_mb << 20, folder="site"
            )
        except image_service.ImageError as exc:
            flash(request, str(exc), "error")
            return _back("/admin/homepage")
    if form.get("remove_hero_image") in {"1", "on"}:
        changes["hero_image"] = ""
    return _save_settings(request, db, s, changes, "/admin/homepage", "settings.homepage")


@router.get("/store-info")
def store_info(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return render(request, db, "admin/store_info.html", s.ctx(cfg=load_site_settings(db)))


@router.post("/store-info")
async def save_store_info(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    form = await request.form()
    map_url = _text(form, "map_url", 500)
    if map_url and not map_url.startswith("https://"):
        flash(request, "The map link must start with https://", "error")
        return _back("/admin/store-info")
    year = _text(form, "established_year", 4)
    changes = {
        "business_name": _text(form, "business_name", 120) or "Pansari Bhandar",
        "business_name_hi": _text(form, "business_name_hi", 120),
        "tagline": _text(form, "tagline", 160),
        "phone": _text(form, "phone", 20),
        "whatsapp_number": _text(form, "whatsapp_number", 20),
        "email": _text(form, "email", 254),
        "address_line": _text(form, "address_line", 240),
        "locality": _text(form, "locality", 80),
        "district": _text(form, "district", 80),
        "state": _text(form, "state", 80),
        "pincode": _text(form, "pincode", 6),
        "map_url": map_url,
        "latitude": _text(form, "latitude", 20),
        "longitude": _text(form, "longitude", 20),
        "opening_hours": _lines(_text(form, "opening_hours", 1000), 8),
        "established_year": year if year.isdecimal() else "",
        "story_title": _text(form, "story_title", 80) or "Our story",
        "story": str(form.get("story", "") or "").strip()[:5000],
    }
    return _save_settings(request, db, s, changes, "/admin/store-info", "settings.store_info")


@router.get("/settings")
def settings_page(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    return render(request, db, "admin/settings.html", s.ctx(cfg=load_site_settings(db)))


@router.post("/settings")
async def save_settings(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    from app.core.formatting import parse_rupees

    form = await request.form()

    def num(key: str, default: int) -> int:
        raw = _text(form, key, 9)
        return int(raw) if raw.isdecimal() else default

    try:
        min_sub = parse_rupees(_text(form, "offer_min_subtotal", 12)) or 0
        max_disc = parse_rupees(_text(form, "offer_max_discount", 12)) or 0
    except ValueError as exc:
        flash(request, str(exc), "error")
        return _back("/admin/settings")
    changes = {
        "require_product_approval": form.get("require_product_approval") in {"1", "on"},
        "default_low_stock_threshold": num("default_low_stock_threshold", 5),
        "max_quantity_per_item": num("max_quantity_per_item", 20),
        "offer_enabled": form.get("offer_enabled") in {"1", "on"},
        "offer_min_subtotal_paise": min_sub,
        "offer_percent": num("offer_percent", 0),
        "offer_max_discount_paise": max_disc,
        "meta_description": _text(form, "meta_description", 300),
    }
    return _save_settings(request, db, s, changes, "/admin/settings", "settings.update")


# ---- testimonials ------------------------------------------------------------------------


@router.get("/testimonials")
def testimonials(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    rows = db.scalars(select(Testimonial).order_by(Testimonial.sort_order, Testimonial.id)).all()
    return render(request, db, "admin/testimonials.html", s.ctx(rows=rows))


def _testimonial_values(form: Any) -> dict[str, Any] | None:
    name, body = _text(form, "name", 80), _text(form, "body", 1000)
    if not name or not body:
        return None
    rating = _text(form, "rating", 1)
    sort = _text(form, "sort_order", 4)
    return {
        "name": name,
        "place": _text(form, "place", 80) or None,
        "body": body,
        "rating": int(rating) if rating in {"1", "2", "3", "4", "5"} else None,
        "sort_order": int(sort) if sort.isdecimal() else 0,
        "is_active": form.get("is_active") in {"1", "on"},
    }


@router.post("/testimonials")
async def add_testimonial(
    request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    values = _testimonial_values(await request.form())
    if values is None:
        flash(request, "Enter the customer's name and their words.", "error")
        return _back("/admin/testimonials")
    t = Testimonial(**values)
    db.add(t)
    db.flush()
    audit(db, request, s.user, "testimonial.create", "testimonial", t.id)
    db.commit()
    flash(request, "Testimonial added.")
    return _back("/admin/testimonials")


@router.post("/testimonials/{tid}")
async def edit_testimonial(
    tid: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    t = db.get(Testimonial, tid)
    if t is None:
        raise NotFound()
    values = _testimonial_values(await request.form())
    if values is None:
        flash(request, "Enter the customer's name and their words.", "error")
        return _back("/admin/testimonials")
    for k, v in values.items():
        setattr(t, k, v)
    audit(db, request, s.user, "testimonial.update", "testimonial", t.id)
    db.commit()
    flash(request, "Testimonial saved.")
    return _back("/admin/testimonials")


@router.post("/testimonials/{tid}/delete")
def delete_testimonial(
    tid: int, request: Request, db: Session = Depends(get_db), s: Scope = Depends(scope)
) -> Response:
    t = db.get(Testimonial, tid)
    if t is None:
        raise NotFound()
    audit(db, request, s.user, "testimonial.delete", "testimonial", t.id, {"name": t.name})
    db.delete(t)
    db.commit()
    flash(request, "Testimonial deleted.")
    return _back("/admin/testimonials")
