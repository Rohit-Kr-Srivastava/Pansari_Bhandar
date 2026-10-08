"""Jinja2 setup and the render() helper that every page uses."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.formatting import percent_off, rupees, rupees_plain, weight_label
from app.db.models import Category
from app.services import seo
from app.services.cart import cart_count
from app.services.delivery import IST
from app.services.settings import SiteSettings, load_site_settings
from app.web.deps import csrf_token, current_user, guest_token

APP_DIR = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


# ---- small TTL cache for data shown on every page ----------------------------------------


@dataclass
class NavCategory:
    id: int
    slug: str
    name: str
    name_hi: str
    image_path: str | None
    children: list[tuple[str, str]] = field(default_factory=list)


class PageCache:
    """Caches site settings and navigation for a short time. Admin saves call clear()."""

    def __init__(self, ttl: float = 60.0):
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str, loader: Any) -> Any:
        hit = self._data.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        value = loader()
        self._data[key] = (now, value)
        return value

    def clear(self) -> None:
        self._data.clear()


def _load_nav(db: Session) -> list[NavCategory]:
    cats = db.scalars(
        select(Category)
        .where(Category.parent_id.is_(None), Category.is_active.is_(True))
        .order_by(Category.sort_order, Category.name)
        .options(selectinload(Category.children))
    ).all()
    return [
        NavCategory(
            c.id,
            c.slug,
            c.name,
            c.name_hi or "",
            c.image_path,
            [
                (ch.slug, ch.name)
                for ch in sorted(c.children, key=lambda x: x.sort_order)
                if ch.is_active
            ],
        )
        for c in cats
    ]


def site_settings(request: Request, db: Session) -> SiteSettings:
    if not hasattr(request.state, "site"):
        cache: PageCache = request.app.state.cache
        request.state.site = cache.get("site", lambda: load_site_settings(db))
    return request.state.site  # type: ignore[no-any-return]


def nav_categories(request: Request, db: Session) -> list[NavCategory]:
    cache: PageCache = request.app.state.cache
    return cache.get("nav", lambda: _load_nav(db))  # type: ignore[no-any-return]


# ---- filters -----------------------------------------------------------------------------


def _static_url_factory() -> Any:
    """Cache-busting URLs: /static/css/app.css?v=<content hash>. Re-hashed when the file changes."""
    hashes: dict[str, tuple[float, str]] = {}

    def static_url(path: str) -> str:
        f = STATIC_DIR / path
        mtime = f.stat().st_mtime if f.is_file() else 0.0
        cached = hashes.get(path)
        if cached is None or cached[0] != mtime:
            digest = hashlib.sha1(f.read_bytes()).hexdigest()[:10] if mtime else "0"
            hashes[path] = cached = (mtime, digest)
        return f"/static/{path}?v={cached[1]}"

    return static_url


def media_url(path: str | None, size: str = "lg") -> str:
    if not path:
        return ""
    if size == "sm" and path.endswith(".webp"):
        path = path.replace(".webp", "_sm.webp")
    return f"/media/{path}"


def ist(value: datetime | None, fmt: str = "%d %b %Y, %I:%M %p") -> str:
    """Show a stored timestamp in Indian Standard Time (the database keeps UTC)."""
    if value is None:
        return ""
    if value.tzinfo is None:  # SQLite returns naive values; they are UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST).strftime(fmt).replace(" 0", " ").lstrip("0")


def nl2p(text: str | None) -> Markup:
    if not text:
        return Markup("")
    paras = [p.strip() for p in str(text).replace("\r", "").split("\n\n") if p.strip()]
    return Markup("".join(f"<p>{escape(p).replace(chr(10), Markup('<br>'))}</p>" for p in paras))


def qs(params: dict[str, Any], **changes: Any) -> str:
    merged = {**params, **changes}
    clean = {k: v for k, v in merged.items() if v is not None and v != "" and v is not False}
    if str(clean.get("page", "")) == "1":
        clean.pop("page")
    return ("?" + urlencode(clean, doseq=True)) if clean else ""


templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
env = templates.env
env.filters.update(
    rupees=rupees,
    rupees_plain=rupees_plain,
    weight=weight_label,
    media=media_url,
    nl2p=nl2p,
    ist=ist,
    percent_off=percent_off,
)
env.globals.update(static_url=_static_url_factory(), qs=qs, media_url=media_url)
env.trim_blocks = True
env.lstrip_blocks = True


def flash(request: Request, message: str, kind: str = "success") -> None:
    request.session.setdefault("flash", [])
    request.session["flash"] = [*request.session["flash"], [kind, message]][-5:]


def render(
    request: Request,
    db: Session,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    meta: seo.Meta | None = None,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    user = current_user(request, db)
    site = site_settings(request, db)
    settings = request.app.state.settings
    ctx: dict[str, Any] = {
        "request": request,
        "user": user,
        "site": site,
        "csrf_token": csrf_token(request),
        "nav": nav_categories(request, db),
        "flashes": request.session.pop("flash", []),
        "site_url": settings.site_url,
        "meta": meta or seo.Meta(title=seo.title(None, site)),
        "current_path": request.url.path,
        "hx": request.headers.get("hx-request") == "true",
    }
    if "cart_count" not in (context or {}):
        ctx["cart_count"] = cart_count(db, user.id if user else None, guest_token(request))
    ctx.update(context or {})
    return templates.TemplateResponse(
        request, template, ctx, status_code=status_code, headers=headers
    )


def toast_headers(
    message: str, kind: str = "success", link: tuple[str, str] | None = None, **extra: Any
) -> dict[str, str]:
    toast: dict[str, Any] = {"message": message, "kind": kind}
    if link:
        toast["link"] = {"href": link[0], "text": link[1]}
    events: dict[str, Any] = {"toast": toast}
    events.update(extra)
    return {"HX-Trigger": json.dumps(events)}
