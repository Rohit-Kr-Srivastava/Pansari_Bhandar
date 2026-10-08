"""Sign in, register, sign out, and the customer's orders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import safe_next
from app.db.models import Address, Order, User, UserRole
from app.services import accounts, seo, whatsapp
from app.services import cart as cart_service
from app.services import orders as order_service
from app.web.deps import (
    NotFound,
    RateLimited,
    client_ip,
    current_user,
    get_db,
    guest_token,
    login_session,
    logout_session,
    require_user,
    touch_login,
)
from app.web.templating import flash, render, site_settings

router = APIRouter()


def _home_for(user: User) -> str:
    if user.role == UserRole.ADMIN:
        return "/admin"
    if user.role == UserRole.SELLER:
        return "/seller"
    return "/"


def _meta(request: Request, db: Session, page: str) -> seo.Meta:
    return seo.Meta(title=seo.title(page, site_settings(request, db)), robots="noindex,nofollow")


@router.get("/login")
def login_page(request: Request, next: str = "", db: Session = Depends(get_db)) -> Response:
    user = current_user(request, db)
    if user:
        return RedirectResponse(safe_next(next, _home_for(user)), status_code=303)
    return render(
        request,
        db,
        "pages/login.html",
        {"next": safe_next(next, "")},
        meta=_meta(request, db, "Sign in"),
    )


@router.post("/login")
def login(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    limiter = request.app.state.ratelimiter
    ip = client_ip(request)
    login = login.strip()[:254]
    # Key on the canonical identity, so "98765 43210" and "x9876543210" share one bucket.
    if not limiter.hit(f"login-ip:{ip}", 20, 600) or not limiter.hit(
        f"login:{accounts.login_key(login)}", 6, 600
    ):
        raise RateLimited("Too many sign-in attempts. Please wait 10 minutes and try again.")
    user = accounts.authenticate(db, login, password[:256])
    if user is None:
        return render(
            request,
            db,
            "pages/login.html",
            {
                "next": safe_next(next, ""),
                "login": login,
                "error": "Mobile number / email or password is not correct.",
            },
            status_code=400,
            meta=_meta(request, db, "Sign in"),
        )
    touch_login(user)
    token = guest_token(request)
    login_session(request, user)
    cart_service.merge_guest_cart(db, token, user.id, site_settings(request, db))
    request.session.pop("cart", None)
    db.commit()
    return RedirectResponse(safe_next(next, _home_for(user)), status_code=303)


@router.get("/register")
def register_page(request: Request, next: str = "", db: Session = Depends(get_db)) -> Response:
    if current_user(request, db):
        return RedirectResponse("/account", status_code=303)
    return render(
        request,
        db,
        "pages/register.html",
        {"next": safe_next(next, ""), "v": {}},
        meta=_meta(request, db, "Create an account"),
    )


@router.post("/register")
def register(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form(""),
    db: Session = Depends(get_db),
) -> Response:
    if not request.app.state.ratelimiter.hit(f"register:{client_ip(request)}", 10, 3600):
        raise RateLimited("Too many accounts created from this network. Try again later.")
    reg = accounts.Registration(full_name, phone, email, password[:256])
    try:
        user = accounts.register_customer(db, reg)
    except accounts.AccountError as exc:
        db.rollback()
        return render(
            request,
            db,
            "pages/register.html",
            {
                "next": safe_next(next, ""),
                "errors": exc.errors,
                "v": {"full_name": full_name[:120], "phone": phone[:15], "email": email[:254]},
            },
            status_code=400,
            meta=_meta(request, db, "Create an account"),
        )
    touch_login(user)
    token = guest_token(request)
    login_session(request, user)
    cart_service.merge_guest_cart(db, token, user.id, site_settings(request, db))
    request.session.pop("cart", None)
    db.commit()
    flash(request, "Welcome! Your account is ready.")
    return RedirectResponse(safe_next(next, "/"), status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    logout_session(request)
    return RedirectResponse("/", status_code=303)


@router.get("/account")
def account(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)
) -> Response:
    orders = db.scalars(
        select(Order).where(Order.user_id == user.id).order_by(Order.placed_at.desc()).limit(50)
    ).all()
    addresses = db.scalars(
        select(Address).where(Address.user_id == user.id).order_by(Address.is_default.desc())
    ).all()
    return render(
        request,
        db,
        "pages/account.html",
        {"orders": orders, "addresses": addresses, "labels": order_service.CUSTOMER_LABELS},
        meta=_meta(request, db, "My account"),
    )


def _my_order(db: Session, user: User, order_no: str) -> Order:
    order = order_service.load_order(db, order_no[:24])
    if order is None or order.user_id != user.id:
        raise NotFound()
    return order


@router.get("/account/orders/{order_no}")
def my_order(
    order_no: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    order = _my_order(db, user, order_no)
    site = site_settings(request, db)
    return render(
        request,
        db,
        "pages/account_order.html",
        {
            "order": order,
            "labels": order_service.CUSTOMER_LABELS,
            "can_cancel": order_service.customer_can_cancel(order),
            "wa_order": whatsapp.order_link(site, order),
            "is_pickup": order_service.is_pickup(order),
        },
        meta=_meta(request, db, f"Order {order.order_no}"),
    )


@router.post("/account/orders/{order_no}/cancel")
def cancel_my_order(
    order_no: str,
    request: Request,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    order = _my_order(db, user, order_no)
    order_no = order.order_no
    cannot = "This order can no longer be cancelled online. Please contact the shop."
    if not order_service.customer_can_cancel(order):
        flash(request, cannot, "error")
    else:
        try:
            order_service.change_status(
                db,
                order,
                order_service.S.CANCELLED,
                user,
                reason=("Cancelled by customer. " + reason.strip())[:500],
                ip=client_ip(request),
            )
            db.commit()
            flash(request, "Your order has been cancelled.")
        except order_service.OrderError:
            flash(request, cannot, "error")  # status changed at the same moment
    return RedirectResponse(f"/account/orders/{order_no}", status_code=303)


@router.post("/account/password")
def change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
) -> Response:
    if not request.app.state.ratelimiter.hit(f"pw:{user.id}", 5, 600):
        raise RateLimited()
    try:
        accounts.change_password(user, current_password[:256], new_password[:256])
    except accounts.AccountError as exc:
        flash(request, " ".join(exc.errors.values()), "error")
        return RedirectResponse("/account#password", status_code=303)
    login_session(request, user)  # re-sign this device; other devices are signed out
    db.commit()
    flash(request, "Password changed. Other devices have been signed out.")
    return RedirectResponse("/account", status_code=303)
