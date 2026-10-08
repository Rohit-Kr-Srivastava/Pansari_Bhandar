"""Homepage, about, contact, delivery information, robots.txt, sitemap.xml and health check."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Category, Product, Store, Testimonial
from app.services import catalog, seo, whatsapp
from app.services import delivery as delivery_service
from app.web.deps import RateLimited, client_ip, get_db, is_htmx
from app.web.templating import render, site_settings

router = APIRouter()


@router.api_route("/healthz", methods=["GET", "HEAD"], include_in_schema=False)
def healthz(request: Request) -> Response:
    with request.app.state.sessionmaker() as db:
        db.execute(text("SELECT 1"))
    return PlainTextResponse("ok")


@router.get("/")
def home(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    popular_list = catalog.popular_ids(db)
    rail_rows = [
        (rail, catalog.rail_ids(db, rail.category_slugs, rail.seasonal_only))
        for rail in site.rails
        if rail.enabled
    ]
    views = catalog.hydrate(db, [*popular_list, *(i for _, ids in rail_rows for i in ids)])
    rails = []
    for rail, ids in rail_rows:
        products = [views[i] for i in ids if i in views]
        if products:
            first = rail.category_slugs[0] if rail.category_slugs else None
            more = f"/c/{first}" if first else "/shop"
            rails.append({"rail": rail, "products": products, "more_url": more})
    testimonials = db.scalars(
        select(Testimonial)
        .where(Testimonial.is_active.is_(True))
        .order_by(Testimonial.sort_order, Testimonial.id)
        .limit(6)
    ).all()
    meta = seo.Meta(
        title=seo.title(None, site),
        description=site.meta_description,
        canonical=f"{url}/",
        jsonld=[seo.local_business(site, url)],
    )
    return render(
        request,
        db,
        "pages/home.html",
        {
            "popular": [views[i] for i in popular_list if i in views],
            "rails": rails,
            "testimonials": testimonials,
            "zones": delivery_service.serviceable_pincodes(db),
            "wa_contact": whatsapp.contact_link(site),
            "pickup": _pickup_offered(db),
        },
        meta=meta,
    )


def _pickup_offered(db: Session) -> bool:
    return bool(
        db.scalar(
            select(Store.id).where(Store.is_active.is_(True), Store.pickup_available.is_(True))
        )
    )


@router.get("/about")
def about(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    meta = seo.Meta(
        title=seo.title(f"About {site.business_name}", site),
        description=seo.shorten(site.story or site.meta_description),
        canonical=f"{url}/about",
        breadcrumbs=[("Home", "/"), ("About us", "/about")],
    )
    meta.jsonld = [seo.breadcrumb_list(meta.breadcrumbs, url)]
    return render(
        request, db, "pages/about.html", {"wa_contact": whatsapp.contact_link(site)}, meta=meta
    )


@router.get("/contact")
def contact(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    meta = seo.Meta(
        title=seo.title("Contact and shop address", site),
        description=seo.shorten(
            f"Visit or call {site.business_name} in {site.locality}, {site.state}. "
            "Opening hours, address, phone and WhatsApp."
        ),
        canonical=f"{url}/contact",
        breadcrumbs=[("Home", "/"), ("Contact", "/contact")],
    )
    meta.jsonld = [seo.local_business(site, url), seo.breadcrumb_list(meta.breadcrumbs, url)]
    return render(
        request, db, "pages/contact.html", {"wa_contact": whatsapp.contact_link(site)}, meta=meta
    )


@router.get("/delivery")
def delivery_info(request: Request, pincode: str = "", db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    meta = seo.Meta(
        title=seo.title(f"Home delivery in {site.locality}", site),
        description=seo.shorten(
            f"Delivery areas, charges and timings for {site.business_name} orders in "
            f"{site.locality} and nearby. Check your pincode."
        ),
        canonical=f"{url}/delivery",
        breadcrumbs=[("Home", "/"), ("Delivery", "/delivery")],
    )
    meta.jsonld = [seo.breadcrumb_list(meta.breadcrumbs, url)]
    stores = db.scalars(select(Store).where(Store.is_active.is_(True))).all()
    return render(
        request,
        db,
        "pages/delivery.html",
        {
            "zones": delivery_service.serviceable_pincodes(db),
            "stores": stores,
            "check": _pincode_check(db, pincode) if pincode.strip() else None,
        },
        meta=meta,
    )


@router.get("/delivery/check")
def delivery_check(
    request: Request,
    pincode: str = "",
    store: int | None = None,
    db: Session = Depends(get_db),
) -> Response:
    if not request.app.state.ratelimiter.hit(f"pin:{client_ip(request)}", 30, 60):
        raise RateLimited()
    if is_htmx(request):
        return render(
            request, db, "partials/pincode_result.html", _pincode_check(db, pincode, store)
        )
    return RedirectResponse(f"/delivery?pincode={quote(pincode.strip()[:6])}", status_code=303)


def _pincode_check(db: Session, pincode: str, store: int | None = None) -> dict[str, Any]:
    pincode = pincode.strip()[:6]
    valid = delivery_service.valid_pincode(pincode)
    results = (
        delivery_service.public_check(db, pincode, [store] if store else None) if valid else []
    )
    return {
        "pincode": pincode,
        "valid": valid,
        "results": results,
        "available": any(r.available for r in results),
    }


@router.get("/robots.txt", include_in_schema=False)
def robots(request: Request) -> Response:
    url = request.app.state.settings.site_url
    lines = [
        "User-agent: *",
        "Disallow: /cart",
        "Disallow: /checkout",
        "Disallow: /account",
        "Disallow: /orders/",
        "Disallow: /seller",
        "Disallow: /admin",
        "Disallow: /login",
        "Disallow: /register",
        "Disallow: /search",
        "Disallow: /delivery/check",
        "",
        f"Sitemap: {url}/sitemap.xml",
    ]
    if not request.app.state.settings.is_production:
        lines = ["User-agent: *", "Disallow: /"]  # keep staging and local copies out of search
    return PlainTextResponse("\n".join(lines) + "\n")


@router.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request, db: Session = Depends(get_db)) -> Response:
    url = request.app.state.settings.site_url
    today = datetime.now(UTC).date().isoformat()
    entries: list[tuple[str, str]] = [
        (f"{url}/", today),
        (f"{url}/shop", today),
        (f"{url}/about", today),
        (f"{url}/contact", today),
        (f"{url}/delivery", today),
    ]
    for slug, updated in db.execute(
        select(Category.slug, Category.updated_at).where(Category.is_active.is_(True))
    ):
        entries.append((f"{url}/c/{slug}", updated.date().isoformat()))
    product_rows = db.execute(
        catalog.visible_products().with_only_columns(Product.slug, Product.updated_at)
    )
    for slug, updated in product_rows:
        entries.append((f"{url}/p/{slug}", updated.date().isoformat()))
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in entries:
        body.append(f"<url><loc>{xml_escape(loc)}</loc><lastmod>{lastmod}</lastmod></url>")
    body.append("</urlset>")
    return Response("\n".join(body), media_type="application/xml")


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return RedirectResponse("/static/img/favicon.svg", status_code=301)
