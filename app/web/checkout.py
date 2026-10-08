"""Checkout: details -> review -> place order -> confirmation."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import new_token
from app.db.models import DeliveryMethod, Order, Store, User
from app.services import cart as cart_service
from app.services import checkout as checkout_service
from app.services import orders as order_service
from app.services import payments, seo, whatsapp
from app.web.deps import NotFound, RateLimited, client_ip, get_db, require_user
from app.web.templating import flash, render, site_settings

router = APIRouter()


def _form_data(form: Any) -> dict[str, str]:
    return {k: v for k, v in form.items() if isinstance(v, str)}


def _pickup_possible(db: Session, store_ids: list[int]) -> bool:
    if not store_ids:
        return False
    stores = db.scalars(select(Store).where(Store.id.in_(store_ids))).all()
    return len(stores) == len(set(store_ids)) and all(s.pickup_available for s in stores)


def _page(
    request: Request,
    db: Session,
    user: User,
    r: checkout_service.Review,
    template: str = "pages/checkout.html",
    status: int = 200,
    **extra: Any,
) -> Response:
    site = site_settings(request, db)
    stores = list(r.summary.store_subtotals())
    ctx = {
        "r": r,
        "f": r.form,
        "summary": r.summary,
        "cart_count": r.summary.count,
        "pickup_possible": _pickup_possible(db, stores),
        "providers": payments.available_providers(max(r.total, 1)),
        "token": request.session.get("checkout_token") or "",
        **extra,
    }
    meta = seo.Meta(title=seo.title("Checkout", site), robots="noindex,nofollow")
    return render(request, db, template, ctx, meta=meta, status_code=status)


def _cart(db: Session, user: User):  # type: ignore[no-untyped-def]
    return cart_service.get_cart(db, user.id, None)


@router.get("/checkout")
def checkout(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    site = site_settings(request, db)
    cart = _cart(db, user)
    summary = cart_service.summarise(db, cart, site)
    if summary.is_empty:
        flash(request, "Your cart is empty.", "error")
        return RedirectResponse("/cart", status_code=303)
    if summary.has_problems:
        flash(request, "Some items in your cart need attention before checkout.", "error")
        return RedirectResponse("/cart", status_code=303)
    form = checkout_service.prefill(user, db)
    if not _pickup_possible(db, list(summary.store_subtotals())):
        form.method = DeliveryMethod.DELIVERY.value
    r = checkout_service.review(db, cart, form, site, summary)
    r.errors = {}  # do not shout at the customer before they type anything
    return _page(request, db, user, r)


@router.post("/checkout/quote")
async def checkout_quote(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    """HTMX: pincode / delivery method changed -> delivery options and totals."""
    if not request.app.state.ratelimiter.hit(f"pin:{client_ip(request)}", 30, 60):
        raise RateLimited()
    site = site_settings(request, db)
    form = checkout_service.CheckoutForm.from_mapping(_form_data(await request.form()))
    r = checkout_service.review(db, _cart(db, user), form, site)
    r.errors.pop("slot", None)  # the first slot is pre-selected in the re-rendered block
    return render(
        request,
        db,
        "partials/checkout_delivery.html",
        {
            "r": r,
            "f": form,
            "summary": r.summary,
            "pickup_possible": _pickup_possible(db, list(r.summary.store_subtotals())),
        },
    )


@router.post("/checkout/review")
async def checkout_review(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    site = site_settings(request, db)
    form = checkout_service.CheckoutForm.from_mapping(_form_data(await request.form()))
    cart = _cart(db, user)
    r = checkout_service.review(db, cart, form, site)
    if r.summary.is_empty:
        flash(request, "Your cart is empty.", "error")
        return RedirectResponse("/cart", status_code=303)
    if request.query_params.get("edit"):
        r.errors = {}
        return _page(request, db, user, r)
    if not r.ok:
        return _page(request, db, user, r, status=422)
    request.session["checkout_token"] = new_token(24)
    return _page(
        request, db, user, r, template="pages/checkout_review.html", fingerprint=r.fingerprint()
    )


@router.post("/checkout/place")
async def checkout_place(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    if not request.app.state.ratelimiter.hit(f"order:{user.id}", 10, 300):
        raise RateLimited("Too many order attempts. Please wait a few minutes.")
    site = site_settings(request, db)
    data = _form_data(await request.form())
    form = checkout_service.CheckoutForm.from_mapping(data)
    token = data.get("checkout_token", "")
    if token != request.session.get("checkout_token"):
        # Missing or old token (expired session, second tab, back button): check for an
        # order already placed with it, otherwise send the customer back to review.
        existing = _order_by_token(db, token, user)
        if existing is not None:
            return RedirectResponse(f"/orders/{existing.order_no}/confirmed", status_code=303)
        flash(request, "Your checkout session expired. Please review your order again.", "error")
        return RedirectResponse("/checkout", status_code=303)
    cart = _cart(db, user)
    try:
        order, start, created = checkout_service.place_order(
            db, user, cart, form, site, token, data.get("fingerprint", "")
        )
    except checkout_service.PriceChanged as exc:
        db.rollback()
        r = checkout_service.review(db, _cart(db, user), form, site)
        return _page(
            request,
            db,
            user,
            r,
            template="pages/checkout_review.html",
            fingerprint=r.fingerprint(),
            notice=str(exc),
            status=409,
        )
    except checkout_service.CheckoutError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse("/cart", status_code=303)
    request.session.pop("checkout_token", None)
    if start.redirect_url:
        return RedirectResponse(start.redirect_url, status_code=303)
    return RedirectResponse(f"/orders/{order.order_no}/confirmed", status_code=303)


def _order_by_token(db: Session, token: str, user: User) -> Order | None:
    if not token:
        return None
    return db.scalar(select(Order).where(Order.checkout_token == token, Order.user_id == user.id))


@router.get("/orders/{order_no}/confirmed")
def order_confirmed(
    order_no: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    order = order_service.load_order(db, order_no[:24])
    if order is None or order.user_id != user.id:
        raise NotFound()
    site = site_settings(request, db)
    meta = seo.Meta(title=seo.title("Order placed", site), robots="noindex,nofollow")
    return render(
        request,
        db,
        "pages/order_confirmed.html",
        {
            "order": order,
            "wa_order": whatsapp.order_link(site, order),
            "labels": order_service.CUSTOMER_LABELS,
            "is_pickup": order_service.is_pickup(order),
        },
        meta=meta,
    )
