"""Cart pages and HTMX interactions (add, change quantity, remove)."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.services import cart as cart_service
from app.services import seo, whatsapp
from app.web.deps import (
    RateLimited,
    back_url,
    client_ip,
    current_user,
    get_db,
    guest_token,
    is_htmx,
)
from app.web.templating import flash, render, site_settings, toast_headers

router = APIRouter()


def _owner(request: Request, db: Session) -> tuple[int | None, str | None]:
    user = current_user(request, db)
    return (user.id if user else None), (None if user else guest_token(request))


def _cart_context(request: Request, db: Session) -> dict[str, Any]:
    site = site_settings(request, db)
    uid, token = _owner(request, db)
    cart = cart_service.get_cart(db, uid, token)
    summary = cart_service.summarise(db, cart, site)
    return {
        "summary": summary,
        "cart_count": summary.count,
        "wa_cart": whatsapp.cart_link(site, summary),
    }


@router.get("/cart")
def view_cart(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    meta = seo.Meta(title=seo.title("Your cart", site), robots="noindex,nofollow")
    return render(request, db, "pages/cart.html", _cart_context(request, db), meta=meta)


def _int(value: str | None, default: int = 0) -> int:
    v = (value or "").strip()
    if v.lstrip("-").isdecimal() and len(v) < 7:
        return int(v)
    return default


@router.post("/cart/add")
def add_to_cart(
    request: Request,
    variant_id: str = Form(""),
    qty: str = Form("1"),
    buy_now: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    if not request.app.state.ratelimiter.hit(f"cart:{client_ip(request)}", 60, 60):
        raise RateLimited()
    site = site_settings(request, db)
    uid, _ = _owner(request, db)
    token = None if uid else guest_token(request, create=True)
    cart = cart_service.get_or_create_cart(db, uid, token)
    try:
        quantity = 1 if not qty.strip() else _int(qty, 0)  # junk -> 0 -> rejected
        cart_service.add_item(db, cart, _int(variant_id), quantity, site)
        db.commit()
    except cart_service.CartError as exc:
        db.rollback()
        if is_htmx(request):
            return Response(status_code=200, headers=toast_headers(str(exc), "error"))
        flash(request, str(exc), "error")
        return RedirectResponse(back_url(request, "/cart"), status_code=303)

    if buy_now:
        if is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": "/checkout"})
        return RedirectResponse("/checkout", status_code=303)
    count = cart_service.cart_count(db, uid, token)
    if is_htmx(request):
        return render(
            request,
            db,
            "partials/cart_count.html",
            {"cart_count": count},
            headers=toast_headers(
                "Added to cart",
                "success",
                link=("/cart", "View cart"),
                cartUpdated={"count": count},
            ),
        )
    flash(request, "Added to cart.")
    return RedirectResponse("/cart", status_code=303)


def _cart_response(
    request: Request, db: Session, message: str = "", kind: str = "success"
) -> Response:
    if is_htmx(request):
        headers = toast_headers(message, kind) if message else None
        return render(
            request, db, "partials/cart_body.html", _cart_context(request, db), headers=headers
        )
    if message:
        flash(request, message, kind)
    return RedirectResponse("/cart", status_code=303)


@router.post("/cart/items/{item_id}")
def update_item(
    item_id: int, request: Request, qty: str = Form(""), db: Session = Depends(get_db)
) -> Response:
    site = site_settings(request, db)
    uid, token = _owner(request, db)
    cart = cart_service.get_cart(db, uid, token)
    if cart is None:
        return _cart_response(
            request, db, "Your cart has expired. Please add items again.", "error"
        )
    try:
        if not re.fullmatch(r"[0-9]{1,3}", qty.strip()):
            raise cart_service.CartError("Enter a quantity, for example 2.")
        cart_service.set_quantity(db, cart, item_id, int(qty), site)
        db.commit()
    except cart_service.CartError as exc:
        db.rollback()
        return _cart_response(request, db, str(exc), "error")
    return _cart_response(request, db)


@router.post("/cart/items/{item_id}/remove")
def remove_item(item_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    uid, token = _owner(request, db)
    cart = cart_service.get_cart(db, uid, token)
    if cart is not None:
        try:
            cart_service.remove_item(db, cart, item_id)
            db.commit()
        except cart_service.CartError:
            db.rollback()
    return _cart_response(request, db, "Removed from cart")
