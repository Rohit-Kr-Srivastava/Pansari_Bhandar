"""Request dependencies: database session, current user, role checks and CSRF."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings
from app.core.security import new_token, safe_next, session_fingerprint, tokens_match
from app.db.models import SellerProfile, SellerStatus, Store, User, UserRole

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
FORM_TYPES = ("application/x-www-form-urlencoded", "multipart/form-data")


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/"):
        self.next_url = next_url


class Forbidden(Exception):
    def __init__(self, message: str = "You do not have access to this page."):
        self.message = message


class CsrfFailed(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, message: str = "Too many attempts. Please wait a minute and try again."):
        self.message = message


class NotFound(Exception):
    pass


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as db:
        yield db


# ---- session & user ----------------------------------------------------------------------


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = new_token()
        request.session["csrf"] = token
    return str(token)


def guest_token(request: Request, create: bool = False) -> str | None:
    token = request.session.get("cart")
    if not token and create:
        token = new_token(24)
        request.session["cart"] = token
    return str(token) if token else None


def login_session(request: Request, user: User) -> None:
    settings = get_settings(request)
    cart = request.session.get("cart")
    request.session.clear()  # new session contents on login (no fixation of old values)
    request.session["uid"] = user.id
    request.session["fp"] = session_fingerprint(user.password_hash, settings.secret_key)
    request.session["csrf"] = new_token()
    request.session["iat"] = int(time.time())
    if cart:
        request.session["cart"] = cart  # merged into the account cart by the caller
    request.state.user = user


def logout_session(request: Request) -> None:
    request.session.clear()
    request.state.user = None


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    if hasattr(request.state, "user"):
        return request.state.user  # type: ignore[no-any-return]
    user = None
    uid = request.session.get("uid")
    if isinstance(uid, int):
        user = db.scalar(select(User).where(User.id == uid))
        if user is not None and user.role == UserRole.SELLER:
            # one query for sellers; customers and admins never touch these relationships
            db.scalar(
                select(SellerProfile)
                .where(SellerProfile.user_id == user.id)
                .options(selectinload(SellerProfile.store))
            )
        settings = get_settings(request)
        if (
            user is None
            or not user.is_active
            or not tokens_match(
                request.session.get("fp"),
                session_fingerprint(user.password_hash, settings.secret_key),
            )
            or (user.role != UserRole.CUSTOMER and _staff_session_expired(request, settings))
        ):
            for key in ("uid", "fp", "iat"):
                request.session.pop(key, None)
            user = None
    request.state.user = user
    return user


def _staff_session_expired(request: Request, settings: Settings) -> bool:
    iat = request.session.get("iat")
    if not isinstance(iat, int):
        return True
    return time.time() - iat > settings.staff_session_hours * 3600


def _next(request: Request) -> str:
    if request.method != "GET":
        return "/"
    path = request.url.path
    return path + (f"?{request.url.query}" if request.url.query else "")


def require_user(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired(_next(request))
    return user


def require_admin(request: Request, user: User | None = Depends(current_user)) -> User:
    if user is None:
        raise LoginRequired(_next(request))
    if user.role != UserRole.ADMIN:
        raise Forbidden()
    return user


def require_seller_account(request: Request, user: User | None = Depends(current_user)) -> User:
    """A seller, approved or not (used for the 'awaiting approval' page)."""
    if user is None:
        raise LoginRequired(_next(request))
    if user.role != UserRole.SELLER or user.seller_profile is None:
        raise Forbidden("This page is for sellers.")
    return user


def require_seller(user: User = Depends(require_seller_account)) -> User:
    profile = user.seller_profile
    assert profile is not None
    if profile.status != SellerStatus.APPROVED or profile.store is None:
        raise Forbidden("pending-seller")
    return user


def seller_store(user: User = Depends(require_seller)) -> Store:
    assert user.seller_profile is not None and user.seller_profile.store is not None
    return user.seller_profile.store


def touch_login(user: User) -> None:
    user.last_login_at = datetime.now(UTC)


# ---- CSRF --------------------------------------------------------------------------------


async def csrf_protect(request: Request) -> None:
    """Applied to every route. Unsafe methods must echo the session's CSRF token, either in the
    X-CSRF-Token header (HTMX) or in a csrf_token form field (plain forms)."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    sent = request.headers.get("x-csrf-token")
    if not sent and request.headers.get("content-type", "").startswith(FORM_TYPES):
        form = await request.form()
        value = form.get("csrf_token")
        sent = value if isinstance(value, str) else None
    if not tokens_match(expected, sent):
        raise CsrfFailed()


def client_ip(request: Request) -> str:
    """The visitor's address for rate limits and the audit log.

    Behind N trusted proxies (Railway's edge = 1), the real client is the Nth entry from the
    right of X-Forwarded-For. Entries further left are supplied by the client and ignored.
    """
    hops = get_settings(request).trusted_proxy_hops
    forwarded = request.headers.get("x-forwarded-for", "")
    if hops > 0 and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-min(hops, len(parts))][:45]
    return request.client.host if request.client else "unknown"


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def back_url(request: Request, default: str = "/") -> str:
    """Same-site path from the Referer header (never another site)."""
    ref = request.headers.get("referer", "")
    parts = urlsplit(ref)
    if parts.netloc and parts.netloc != request.url.netloc:
        return default
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return safe_next(path, default)
