"""Page metadata and structured data (schema.org JSON-LD).

Only facts the shop has entered are published; empty settings are left out of the markup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.catalog import ProductView
from app.services.settings import SiteSettings

HOURS_RE = re.compile(
    r"^(Mo|Tu|We|Th|Fr|Sa|Su)(-(Mo|Tu|We|Th|Fr|Sa|Su))?(,\S+)* \d{2}:\d{2}-\d{2}:\d{2}$"
)


@dataclass
class Meta:
    title: str
    description: str = ""
    canonical: str | None = None
    robots: str = "index,follow"
    og_type: str = "website"
    og_image: str | None = None
    jsonld: list[dict[str, Any]] = field(default_factory=list)
    breadcrumbs: list[tuple[str, str]] = field(default_factory=list)


def title(page: str | None, s: SiteSettings) -> str:
    brand = f"{s.business_name}, {s.locality}" if s.locality else s.business_name
    return f"{page} | {brand}" if page else f"{brand}: {s.tagline}"


def shorten(text: str | None, limit: int = 158) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0] + "…"


def local_business(s: SiteSettings, site_url: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "GroceryStore",
        "@id": f"{site_url}/#store",
        "name": s.business_name,
        "url": f"{site_url}/",
        "image": f"{site_url}/static/img/og-default.png",
        "priceRange": "₹",
        "currenciesAccepted": "INR",
        "paymentAccepted": "Cash, UPI",
    }
    address = {
        "@type": "PostalAddress",
        "streetAddress": s.address_line,
        "addressLocality": s.locality,
        "addressRegion": s.state,
        "postalCode": s.pincode,
        "addressCountry": "IN",
    }
    data["address"] = {k: v for k, v in address.items() if v}
    if s.phone:
        data["telephone"] = f"+91{s.phone[-10:]}" if len(s.phone) >= 10 else s.phone
    if s.latitude and s.longitude:
        data["geo"] = {"@type": "GeoCoordinates", "latitude": s.latitude, "longitude": s.longitude}
    hours = [h for h in s.opening_hours if HOURS_RE.match(h.strip())]
    if hours:
        data["openingHours"] = hours
    if s.map_url:
        data["hasMap"] = s.map_url
    if s.established_year.isdecimal():
        data["foundingDate"] = s.established_year
    return data


def breadcrumb_list(items: list[tuple[str, str]], site_url: str) -> dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": f"{site_url}{url}"}
            for i, (name, url) in enumerate(items)
        ],
    }


def product(
    view: ProductView, url: str, image_url: str | None, s: SiteSettings, site_url: str
) -> dict[str, Any]:
    p = view.product
    data: dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": p.name,
        "url": url,
        "category": p.category.name,
    }
    if image_url:
        data["image"] = [image_url]
    desc = p.short_description or p.description
    if desc:
        data["description"] = shorten(desc, 500)
    if p.brand:
        data["brand"] = {"@type": "Brand", "name": p.brand}
    if view.default:
        data["sku"] = next((v.sku for v in p.variants if v.id == view.default.id), None)
    seller = {"@type": "Organization", "name": s.business_name, "@id": f"{site_url}/#store"}

    def offer(v: Any) -> dict[str, Any]:
        return {
            "@type": "Offer",
            "name": v.label,
            "price": f"{v.effective / 100:.2f}",
            "priceCurrency": "INR",
            "availability": "https://schema.org/InStock"
            if v.in_stock
            else "https://schema.org/OutOfStock",
            "url": f"{url}?v={v.id}",
            "seller": seller,
            "itemCondition": "https://schema.org/NewCondition",
        }

    if view.variants:
        data["offers"] = [offer(v) for v in view.variants]
    return data
