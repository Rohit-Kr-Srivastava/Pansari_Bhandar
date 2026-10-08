"""Shop: all products, category pages, search results and product detail."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from app.core.formatting import parse_rupees
from app.services import catalog, seo, whatsapp
from app.services.catalog import SORT_LABELS, ListingFilters, Sort
from app.web.deps import NotFound, get_db, is_htmx
from app.web.templating import media_url, render, site_settings

router = APIRouter()


def _filters(request: Request) -> tuple[ListingFilters, dict[str, Any]]:
    qp = request.query_params

    def money(key: str) -> int | None:
        try:
            return parse_rupees(qp.get(key))
        except ValueError:
            return None

    try:
        sort = Sort(qp.get("sort", ""))
    except ValueError:
        sort = Sort.RELEVANCE
    page_raw = qp.get("page", "1")
    page = int(page_raw) if page_raw.isdecimal() and len(page_raw) < 6 else 1
    f = ListingFilters(
        q=catalog.clean_query(qp.get("q")),
        min_price=money("min"),
        max_price=money("max"),
        in_stock=qp.get("in_stock") == "1",
        sort=sort,
        page=page,
    )
    params = {
        "q": f.q,
        "min": qp.get("min", "")[:10] if f.min_price is not None else "",
        "max": qp.get("max", "")[:10] if f.max_price is not None else "",
        "in_stock": "1" if f.in_stock else "",
        "sort": sort.value if sort is not Sort.RELEVANCE else "",
    }
    return f, params


def _listing(
    request: Request,
    db: Session,
    *,
    heading: str,
    base_url: str,
    meta: seo.Meta,
    category: Any = None,
    search: bool = False,
) -> Response:
    f, params = _filters(request)
    if category is not None:
        f.category_ids = catalog.category_ids_with_children(category)
    page = catalog.list_products(db, f)
    filtered = any(params[k] for k in ("min", "max", "in_stock", "sort")) or (f.q and not search)
    if page.page > 1 or filtered:
        meta.robots = "noindex,follow" if filtered else meta.robots
    if page.page > 1:
        meta.canonical = f"{meta.canonical}?page={page.page}"
        meta.title = f"{meta.title} (page {page.page})"
    ctx = {
        "heading": heading,
        "category": category,
        "page": page,
        "params": params,
        "base_url": base_url,
        "sort_labels": SORT_LABELS,
        "search": search,
    }
    if is_htmx(request) and request.headers.get("hx-target") == "results":
        return render(request, db, "partials/results.html", ctx, meta=meta)
    return render(request, db, "pages/listing.html", ctx, meta=meta)


@router.get("/shop")
def shop(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    meta = seo.Meta(
        title=seo.title("Shop all products", site),
        description=seo.shorten(
            f"Spices, masala, jadi-buti, puja samagri, shringar and daily needs from "
            f"{site.business_name}, {site.locality}. Local delivery and store pickup."
        ),
        canonical=f"{url}/shop",
        breadcrumbs=[("Home", "/"), ("Shop", "/shop")],
    )
    meta.jsonld = [seo.breadcrumb_list(meta.breadcrumbs, url)]
    return _listing(request, db, heading="All products", base_url="/shop", meta=meta)


@router.get("/c/{slug}")
def category_page(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    cat = catalog.category_by_slug(db, slug[:80])
    if cat is None:
        raise NotFound()
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    crumbs = [("Home", "/"), ("Shop", "/shop")]
    if cat.parent:
        crumbs.append((cat.parent.name, f"/c/{cat.parent.slug}"))
    crumbs.append((cat.name, f"/c/{cat.slug}"))
    meta = seo.Meta(
        title=seo.title(f"{cat.name} in {site.locality}" if site.locality else cat.name, site),
        description=seo.shorten(
            cat.description
            or f"Buy {cat.name.lower()} from {site.business_name}, {site.locality}. "
            "Fresh stock, local delivery and store pickup."
        ),
        canonical=f"{url}/c/{cat.slug}",
        breadcrumbs=crumbs,
    )
    meta.jsonld = [seo.breadcrumb_list(crumbs, url)]
    return _listing(
        request, db, heading=cat.name, base_url=f"/c/{cat.slug}", meta=meta, category=cat
    )


@router.get("/search")
def search(request: Request, db: Session = Depends(get_db)) -> Response:
    site = site_settings(request, db)
    q = catalog.clean_query(request.query_params.get("q"))
    url = request.app.state.settings.site_url
    meta = seo.Meta(
        title=seo.title(f"Search: {q}" if q else "Search", site),
        canonical=f"{url}/search",
        robots="noindex,follow",
    )
    heading = f"Results for “{q}”" if q else "Search"
    return _listing(request, db, heading=heading, base_url="/search", meta=meta, search=True)


def _product_context(request: Request, db: Session, slug: str) -> dict[str, Any]:
    product = catalog.product_by_slug(db, slug[:160])
    if product is None:
        raise NotFound()
    vid = request.query_params.get("v", "")
    view = catalog.to_view(product, int(vid) if vid.isdecimal() else None)
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    page_url = f"{url}/p/{product.slug}"
    d = view.default
    return {
        "view": view,
        "product": product,
        "v": d,
        "wa_product": whatsapp.product_link(
            site, product.name, d.label if d else None, d.effective if d else None, page_url
        ),
        "page_url": page_url,
    }


@router.get("/p/{slug}")
def product_page(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    ctx = _product_context(request, db, slug)
    product, view = ctx["product"], ctx["view"]
    site = site_settings(request, db)
    url = request.app.state.settings.site_url
    crumbs = [("Home", "/")]
    cat = product.category
    if cat.parent:
        crumbs.append((cat.parent.name, f"/c/{cat.parent.slug}"))
    crumbs.append((cat.name, f"/c/{cat.slug}"))
    crumbs.append((product.name, f"/p/{product.slug}"))
    image_url = f"{url}{media_url(view.image.path)}" if view.image else None
    desc = (
        product.short_description
        or product.description
        or (
            f"{product.name} from {site.business_name}, {site.locality}. "
            + (
                f"Available in {', '.join(v.label for v in view.variants)}."
                if view.variants
                else ""
            )
        )
    )
    meta = seo.Meta(
        title=seo.title(product.name, site),
        description=seo.shorten(desc),
        canonical=ctx["page_url"],
        og_type="product",
        og_image=image_url,
        breadcrumbs=crumbs,
    )
    meta.jsonld = [
        seo.product(view, ctx["page_url"], image_url, site, url),
        seo.breadcrumb_list(crumbs, url),
    ]
    ctx["related"] = catalog.related_products(db, product)
    return render(request, db, "pages/product.html", ctx, meta=meta)


@router.get("/p/{slug}/buybox")
def product_buybox(slug: str, request: Request, db: Session = Depends(get_db)) -> Response:
    """HTMX: re-render the price / stock / add-to-cart block for another size."""
    if not is_htmx(request):
        return RedirectResponse(f"/p/{slug}?{request.url.query}", status_code=303)
    return render(request, db, "partials/buybox.html", _product_context(request, db, slug))
