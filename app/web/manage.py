"""Screens shared by the seller dashboard (one store) and the admin dashboard (all stores):
products, inventory, orders and delivery settings.

Every function takes a Scope. For sellers scope.store_id is their own store and every query is
filtered by it; ownership is checked again on every write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload
from starlette.datastructures import UploadFile

from app.core.formatting import parse_rupees, rupees_plain
from app.db.models import (
    Category,
    DeliverySlot,
    Inventory,
    Order,
    OrderStatus,
    Product,
    ProductStatus,
    ProductVariant,
    SellerDeliveryZone,
    Store,
    User,
)
from app.services import dashboard, whatsapp
from app.services import orders as order_service
from app.services import product_editor as editor
from app.services.delivery import valid_pincode
from app.web.deps import NotFound, client_ip, is_htmx
from app.web.templating import flash, render, site_settings, toast_headers

PER_PAGE = 30


@dataclass
class Scope:
    area: str  # "seller" or "admin"
    user: User
    store_id: int | None  # None = every store (admin)

    @property
    def base(self) -> str:
        return f"/{self.area}"

    @property
    def layout(self) -> str:
        return f"{self.area}/layout.html"

    @property
    def is_admin(self) -> bool:
        return self.area == "admin"

    def ctx(self, **kw: Any) -> dict[str, Any]:
        return {"scope": self, "layout": self.layout, "base": self.base, **kw}


def _page_no(request: Request) -> int:
    raw = request.query_params.get("page", "1")
    return max(int(raw), 1) if raw.isdecimal() and len(raw) < 6 else 1


def _int_or_none(value: Any) -> int | None:
    v = str(value or "").strip()
    return int(v) if v.isdecimal() and len(v) < 10 else None


# ---- products ----------------------------------------------------------------------------


def _categories(db: Session) -> list[Category]:
    return list(
        db.scalars(
            select(Category)
            .where(Category.is_active.is_(True))
            .order_by(Category.sort_order, Category.name)
            .options(selectinload(Category.parent))
        )
    )


def _owned_product(db: Session, scope: Scope, product_id: int) -> Product:
    product = editor.load_for_edit(db, product_id)
    if product is None or (scope.store_id is not None and product.store_id != scope.store_id):
        raise NotFound()
    return product


def products_list(request: Request, db: Session, scope: Scope) -> Response:
    q = " ".join(request.query_params.get("q", "").split())[:80]
    status = request.query_params.get("status", "")
    stmt = select(Product).options(
        selectinload(Product.variants).selectinload(ProductVariant.inventory),
        selectinload(Product.images),
        selectinload(Product.category),
        selectinload(Product.store),
    )
    if scope.store_id is not None:
        stmt = stmt.where(Product.store_id == scope.store_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                Product.name.ilike(like),
                Product.search_keywords.ilike(like),
                Product.variants.any(ProductVariant.sku.ilike(like)),
            )
        )
    if status in {s.value for s in ProductStatus}:
        stmt = stmt.where(Product.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = _page_no(request)
    products = db.scalars(
        stmt.order_by(Product.updated_at.desc(), Product.id.desc())
        .offset((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    ).all()
    ctx = scope.ctx(
        products=products,
        q=q,
        status=status,
        statuses=list(ProductStatus),
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
        total=total,
    )
    return render(request, db, "manage/products.html", ctx)


def _stores(db: Session) -> list[Store]:
    return list(db.scalars(select(Store).order_by(Store.name)))


def product_form(
    request: Request,
    db: Session,
    scope: Scope,
    form: editor.ProductForm,
    product: Product | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    if not form.variants:
        form.variants = [editor.VariantRow()]
    ctx = scope.ctx(
        form=form,
        product=product,
        errors=errors or {},
        categories=_categories(db),
        stores=_stores(db) if scope.is_admin else [],
        info_fields=editor.INFO_FIELDS,
        unit_choices=editor.UNIT_CHOICES,
        require_approval=site_settings(request, db).require_product_approval,
        empty_row=editor.VariantRow(),
    )
    return render(request, db, "manage/product_form.html", ctx, status_code=status_code)


def new_product_page(request: Request, db: Session, scope: Scope) -> Response:
    form = editor.ProductForm()
    if scope.store_id:
        form.store_id = str(scope.store_id)
    return product_form(request, db, scope, form)


def clone_product_page(request: Request, db: Session, scope: Scope, product_id: int) -> Response:
    source = _owned_product(db, scope, product_id)
    form = editor.ProductForm.from_product(source, clone=True)
    return product_form(request, db, scope, form)


def edit_product_page(request: Request, db: Session, scope: Scope, product_id: int) -> Response:
    product = _owned_product(db, scope, product_id)
    return product_form(request, db, scope, editor.ProductForm.from_product(product), product)


async def _uploads(request: Request, form: Any) -> list[bytes]:
    limit = request.app.state.settings.max_upload_mb << 20
    out = []
    for f in form.getlist("images"):
        if isinstance(f, UploadFile) and f.filename:
            data = await f.read(limit + 1)
            if data:
                out.append(data)
    return out


async def save_product(
    request: Request, db: Session, scope: Scope, product_id: int | None = None
) -> Response:
    raw = await request.form()
    form = editor.ProductForm.from_form(raw)
    product = _owned_product(db, scope, product_id) if product_id else None
    if scope.is_admin:
        store_id = _int_or_none(form.store_id) or (product.store_id if product else None)
        store = db.get(Store, store_id) if store_id else None
    else:
        store = db.get(Store, scope.store_id)
    if store is None:
        return product_form(
            request,
            db,
            scope,
            form,
            product,
            {"store_id": "Choose the store this product belongs to."},
            422,
        )
    if form.clone_from:
        # Parse once and verify ownership; the editor only ever sees this checked id.
        clone_id = _int_or_none(form.clone_from)
        if clone_id is None or product is not None:
            form.clone_from = ""
        else:
            form.clone_from = str(_owned_product(db, scope, clone_id).id)
    settings = request.app.state.settings
    try:
        saved = editor.save(
            db,
            form,
            actor=scope.user,
            store=store,
            settings=site_settings(request, db),
            media_root=settings.media_root,
            uploads=await _uploads(request, raw),
            max_upload_bytes=settings.max_upload_mb << 20,
            product=product,
            ip=client_ip(request),
        )
        db.commit()
        editor.finish_uploads(db, settings.media_root)
    except editor.ProductFormError as exc:
        db.rollback()
        product = _owned_product(db, scope, product_id) if product_id else None
        return product_form(request, db, scope, form, product, exc.errors, 422)
    request.app.state.cache.clear()
    msg = {
        ProductStatus.APPROVED: "Saved. The product is live in the shop.",
        ProductStatus.PENDING: "Saved and sent for approval.",
        ProductStatus.DRAFT: "Saved as a draft.",
        ProductStatus.INACTIVE: "Saved. The product is hidden from the shop.",
    }.get(saved.status, "Saved.")
    flash(request, msg)
    if raw.get("then") == "clone":
        return RedirectResponse(f"{scope.base}/products/{saved.id}/clone", status_code=303)
    return RedirectResponse(f"{scope.base}/products/{saved.id}/edit", status_code=303)


def set_product_status(
    request: Request, db: Session, scope: Scope, product_id: int, action: str
) -> Response:
    product = _owned_product(db, scope, product_id)
    settings = site_settings(request, db)
    if action == "hide":
        product.status = ProductStatus.INACTIVE
    elif action == "show":
        trusted = scope.is_admin or not settings.require_product_approval
        product.status = ProductStatus.APPROVED if trusted else ProductStatus.PENDING
    else:
        raise NotFound()
    db.commit()
    request.app.state.cache.clear()
    flash(request, "Product updated.")
    return RedirectResponse(f"{scope.base}/products", status_code=303)


# ---- inventory ---------------------------------------------------------------------------


def inventory_list(request: Request, db: Session, scope: Scope) -> Response:
    q = " ".join(request.query_params.get("q", "").split())[:80]
    only_low = request.query_params.get("low") == "1"
    stmt = (
        select(ProductVariant)
        .join(Product, Product.id == ProductVariant.product_id)
        .outerjoin(Inventory, Inventory.variant_id == ProductVariant.id)
        .where(Product.status != ProductStatus.REJECTED)
        .options(
            selectinload(ProductVariant.inventory),
            selectinload(ProductVariant.product).selectinload(Product.store),
        )
    )
    if scope.store_id is not None:
        stmt = stmt.where(Product.store_id == scope.store_id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Product.name.ilike(like), ProductVariant.sku.ilike(like)))
    if only_low:
        stmt = stmt.where(
            func.coalesce(Inventory.quantity, 0) - func.coalesce(Inventory.reserved, 0)
            <= func.coalesce(Inventory.low_stock_threshold, 0)
        )
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = _page_no(request)
    variants = db.scalars(
        stmt.order_by(Product.name, ProductVariant.sort_order)
        .offset((page - 1) * PER_PAGE * 2)
        .limit(PER_PAGE * 2)
    ).all()
    ctx = scope.ctx(
        variants=variants,
        q=q,
        only_low=only_low,
        page=page,
        pages=max((total + PER_PAGE * 2 - 1) // (PER_PAGE * 2), 1),
    )
    return render(request, db, "manage/inventory.html", ctx)


async def update_stock(request: Request, db: Session, scope: Scope, variant_id: int) -> Response:
    form = await request.form()
    variant = db.scalar(
        select(ProductVariant)
        .where(ProductVariant.id == variant_id)
        .options(selectinload(ProductVariant.inventory), selectinload(ProductVariant.product))
    )
    if variant is None or (
        scope.store_id is not None and variant.product.store_id != scope.store_id
    ):
        raise NotFound()
    qty_raw = str(form.get("quantity", "")).strip()
    adjust = str(form.get("adjust", "")).strip()
    try:
        threshold_raw = str(form.get("threshold", "")).strip()
        threshold = int(threshold_raw) if threshold_raw.isdecimal() else None
        if adjust:
            if not re.fullmatch(r"[+-]?[0-9]{1,5}", adjust):
                raise ValueError("Enter the change as a whole number, e.g. +5 or -2.")
            # Relative change in one UPDATE, so a checkout at the same moment is not lost.
            editor.adjust_stock(db, variant, int(adjust), scope.user, client_ip(request))
        elif re.fullmatch(r"[0-9]{1,6}", qty_raw):
            editor.set_stock(db, variant, int(qty_raw), threshold, scope.user, client_ip(request))
        else:
            raise ValueError("Enter the stock as a whole number.")
        db.commit()
        message, kind = f"Stock for {variant.product.name} {variant.label} saved.", "success"
    except ValueError as exc:
        db.rollback()
        message, kind = str(exc), "error"
    db.refresh(variant)
    if is_htmx(request):
        return render(
            request,
            db,
            "manage/_inventory_row.html",
            scope.ctx(v=variant),
            headers=toast_headers(message, kind),
        )
    flash(request, message, kind)
    return RedirectResponse(f"{scope.base}/inventory", status_code=303)


# ---- orders ------------------------------------------------------------------------------


def orders_list(request: Request, db: Session, scope: Scope) -> Response:
    status = request.query_params.get("status", "open")
    q = request.query_params.get("q", "").strip()[:40]
    stmt = dashboard.orders_query(scope.store_id).options(
        selectinload(Order.items), selectinload(Order.deliveries)
    )
    if status == "open":
        stmt = stmt.where(Order.status.in_(order_service.OPEN_STATUSES))
    elif status in {s.value for s in OrderStatus}:
        stmt = stmt.where(Order.status == status)
    if q:
        stmt = stmt.where(
            or_(
                Order.order_no.ilike(f"%{q}%"),
                Order.user_id.in_(select(User.id).where(User.phone.ilike(f"%{q}%"))),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    page = _page_no(request)
    orders = db.scalars(
        stmt.order_by(Order.placed_at.desc()).offset((page - 1) * PER_PAGE).limit(PER_PAGE)
    ).all()
    ctx = scope.ctx(
        orders=orders,
        status=status,
        q=q,
        labels=order_service.LABELS,
        statuses=list(OrderStatus),
        page=page,
        pages=max((total + PER_PAGE - 1) // PER_PAGE, 1),
    )
    return render(request, db, "manage/orders.html", ctx)


def _scoped_order(db: Session, scope: Scope, order_no: str) -> Order:
    order = order_service.load_order(db, order_no[:24])
    if order is None:
        raise NotFound()
    if scope.store_id is not None and scope.store_id not in order_service.store_ids(order):
        raise NotFound()
    return order


def order_detail(request: Request, db: Session, scope: Scope, order_no: str) -> Response:
    order = _scoped_order(db, scope, order_no)
    customer = db.get(User, order.user_id)
    items = [i for i in order.items if scope.store_id is None or i.store_id == scope.store_id]
    can_manage = order_service.can_manage(scope.user, order)
    addr = order.delivery_address or {}
    message = f"Namaste {addr.get('name', '')}, about your order {order.order_no}: "
    ctx = scope.ctx(
        order=order,
        items=items,
        customer=customer,
        labels=order_service.LABELS,
        next_statuses=order_service.allowed_next(order) if can_manage else [],
        can_manage=can_manage,
        is_pickup=order_service.is_pickup(order),
        wa_customer=whatsapp.customer_link(
            addr.get("phone") or (customer.phone if customer else None), message
        ),
    )
    return render(request, db, "manage/order_detail.html", ctx)


async def change_order_status(
    request: Request, db: Session, scope: Scope, order_no: str
) -> Response:
    form = await request.form()
    order = _scoped_order(db, scope, order_no)
    order_no = order.order_no
    if not order_service.can_manage(scope.user, order):
        flash(
            request,
            "This order includes other shops' items. The admin manages its status.",
            "error",
        )
        return RedirectResponse(f"{scope.base}/orders/{order.order_no}", status_code=303)
    try:
        new = OrderStatus(str(form.get("status", "")))
        order_service.change_status(
            db,
            order,
            new,
            scope.user,
            reason=str(form.get("reason", ""))[:500] or None,
            ip=client_ip(request),
        )
        db.commit()
        flash(request, f"Order marked {order_service.LABELS[new]}.")
    except (ValueError, order_service.OrderError) as exc:
        db.rollback()
        flash(
            request,
            str(exc) if isinstance(exc, order_service.OrderError) else "Choose a valid status.",
            "error",
        )
    return RedirectResponse(f"{scope.base}/orders/{order_no}", status_code=303)


# ---- delivery settings -------------------------------------------------------------------


def _parse_time(value: Any) -> time | None:
    v = str(value or "").strip()
    if not v:
        return None
    try:
        return time.fromisoformat(v[:5])
    except ValueError:
        return None


def delivery_page(
    request: Request, db: Session, scope: Scope, store: Store, errors: dict[str, str] | None = None
) -> Response:
    zones = db.scalars(
        select(SellerDeliveryZone)
        .where(SellerDeliveryZone.store_id == store.id)
        .order_by(SellerDeliveryZone.pincode)
    ).all()
    slots = db.scalars(
        select(DeliverySlot)
        .where(DeliverySlot.store_id == store.id)
        .order_by(DeliverySlot.sort_order, DeliverySlot.start_time)
    ).all()
    ctx = scope.ctx(
        store=store,
        zones=zones,
        slots=slots,
        errors=errors or {},
        stores=_stores(db) if scope.is_admin else [],
        rupees_plain=rupees_plain,
    )
    return render(request, db, "manage/delivery.html", ctx, status_code=422 if errors else 200)


def _delivery_url(scope: Scope, store: Store) -> str:
    return f"{scope.base}/delivery" + (f"?store={store.id}" if scope.is_admin else "")


async def save_delivery_options(
    request: Request, db: Session, scope: Scope, store: Store
) -> Response:
    form = await request.form()
    radius_raw = str(form.get("delivery_radius_km", "")).strip()
    try:
        radius = Decimal(radius_raw) if radius_raw else None
        if radius is not None and not (0 <= radius <= 100):
            raise InvalidOperation
    except InvalidOperation:
        return delivery_page(
            request, db, scope, store, {"radius": "Delivery radius must be between 0 and 100 km."}
        )
    store.delivery_radius_km = radius
    store.pickup_available = form.get("pickup_available") in {"1", "on"}
    store.pickup_instructions = str(form.get("pickup_instructions", "")).strip()[:1000] or None
    db.commit()
    flash(request, "Delivery options saved.")
    return RedirectResponse(_delivery_url(scope, store), status_code=303)


def _zone_values(form: Any) -> tuple[dict[str, Any], dict[str, str]]:
    errors: dict[str, str] = {}
    pincode = str(form.get("pincode", "")).strip()
    if not valid_pincode(pincode):
        errors["pincode"] = "Enter a valid 6-digit pincode."
    values: dict[str, Any] = {
        "pincode": pincode,
        "area_name": str(form.get("area_name", "")).strip()[:120] or None,
    }
    try:
        values["delivery_fee_paise"] = parse_rupees(str(form.get("fee", ""))) or 0
        values["min_order_paise"] = parse_rupees(str(form.get("min_order", ""))) or 0
        values["free_delivery_above_paise"] = parse_rupees(str(form.get("free_above", "")))
    except ValueError as exc:
        errors["fee"] = str(exc)
    days = str(form.get("estimated_days", "1")).strip() or "1"
    if not days.isdecimal() or not 0 <= int(days) <= 30:
        errors["estimated_days"] = "Delivery days must be between 0 and 30."
    else:
        values["estimated_days"] = int(days)
    values["same_day_available"] = form.get("same_day_available") in {"1", "on"}
    values["same_day_cutoff"] = _parse_time(form.get("same_day_cutoff"))
    values["is_active"] = form.get("is_active") in {"1", "on"}
    return values, errors


async def save_zone(
    request: Request, db: Session, scope: Scope, store: Store, zone_id: int | None = None
) -> Response:
    form = await request.form()
    values, errors = _zone_values(form)
    zone = None
    if zone_id is not None:
        zone = db.get(SellerDeliveryZone, zone_id)
        if zone is None or zone.store_id != store.id:
            raise NotFound()
    clash = db.scalar(
        select(SellerDeliveryZone.id).where(
            SellerDeliveryZone.store_id == store.id,
            SellerDeliveryZone.pincode == values["pincode"],
            SellerDeliveryZone.id != (zone_id or 0),
        )
    )
    if clash:
        errors["pincode"] = f"Pincode {values['pincode']} is already in your list."
    if errors:
        return delivery_page(request, db, scope, store, errors)
    if zone is None:
        zone = SellerDeliveryZone(store_id=store.id, **values)
        db.add(zone)
    else:
        for k, v in values.items():
            setattr(zone, k, v)
    db.commit()
    flash(request, f"Delivery to {values['pincode']} saved.")
    return RedirectResponse(_delivery_url(scope, store), status_code=303)


def delete_zone(
    request: Request, db: Session, scope: Scope, store: Store, zone_id: int
) -> Response:
    zone = db.get(SellerDeliveryZone, zone_id)
    if zone is None or zone.store_id != store.id:
        raise NotFound()
    db.delete(zone)
    db.commit()
    flash(request, "Pincode removed.")
    return RedirectResponse(_delivery_url(scope, store), status_code=303)


async def save_slot(
    request: Request, db: Session, scope: Scope, store: Store, slot_id: int | None = None
) -> Response:
    form = await request.form()
    start, end = _parse_time(form.get("start_time")), _parse_time(form.get("end_time"))
    if start is None or end is None or end <= start:
        return delivery_page(
            request,
            db,
            scope,
            store,
            {"slot": "Enter a start and end time; the end must be later."},
        )
    label = str(form.get("label", "")).strip()[:60] or (
        f"{start.strftime('%I:%M %p').lstrip('0')} - {end.strftime('%I:%M %p').lstrip('0')}"
    )
    slot = None
    if slot_id is not None:
        slot = db.get(DeliverySlot, slot_id)
        if slot is None or slot.store_id != store.id:
            raise NotFound()
    if slot is None:
        slot = DeliverySlot(store_id=store.id, label=label, start_time=start, end_time=end)
        db.add(slot)
    slot.label, slot.start_time, slot.end_time = label, start, end
    slot.is_active = form.get("is_active") in {"1", "on"}
    slot.sort_order = start.hour * 60 + start.minute
    db.commit()
    flash(request, "Delivery slot saved.")
    return RedirectResponse(_delivery_url(scope, store), status_code=303)


def delete_slot(
    request: Request, db: Session, scope: Scope, store: Store, slot_id: int
) -> Response:
    slot = db.get(DeliverySlot, slot_id)
    if slot is None or slot.store_id != store.id:
        raise NotFound()
    db.delete(slot)
    db.commit()
    flash(request, "Delivery slot removed.")
    return RedirectResponse(_delivery_url(scope, store), status_code=303)


# ---- dashboard ---------------------------------------------------------------------------


def dashboard_page(request: Request, db: Session, scope: Scope, template: str) -> Response:
    recent = db.scalars(
        dashboard.orders_query(scope.store_id)
        .where(Order.status.in_(order_service.OPEN_STATUSES))
        .order_by(Order.placed_at.desc())
        .limit(8)
    ).all()
    ctx = scope.ctx(
        stats=dashboard.summary(db, scope.store_id),
        low=dashboard.low_stock(db, scope.store_id),
        popular=dashboard.popular(db, scope.store_id),
        recent=recent,
        labels=order_service.LABELS,
    )
    return render(request, db, template, ctx)
