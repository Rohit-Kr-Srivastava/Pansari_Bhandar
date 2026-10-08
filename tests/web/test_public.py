"""Homepage, listing, search, product detail and SEO output."""

import json
import re

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.db.models import Product, ProductStatus, Testimonial
from app.services.settings import save_site_settings
from tests.web.conftest import HX, World


def _jsonld(html: str) -> list[dict]:  # type: ignore[type-arg]
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    return [json.loads(b) for b in blocks]


def test_homepage_sections(client: TestClient, world: World) -> None:
    html = client.get("/").text
    for text in [
        "Shop by category",
        "Popular products",
        "Spices &amp; Masala",
        "Why shop with us",
        "Our story",
        "Visit the shop",
        "Local delivery",
        "Check your pincode",
    ]:
        assert text in html, text
    assert "Haldi Powder" in html
    assert "What our customers say" not in html  # hidden until real testimonials exist
    assert "Prefer to order on WhatsApp" not in html  # hidden until a number is configured


def test_homepage_shows_admin_content(client: TestClient, world: World, db: Session) -> None:
    save_site_settings(
        db,
        {
            "announcement": "Diwali offer",
            "announcement_enabled": True,
            "whatsapp_number": "919000000009",
        },
    )
    db.add(Testimonial(name="Sunita", place="Gomia", body="Fresh haldi every time", rating=5))
    db.commit()
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    html = client.get("/").text
    assert "Diwali offer" in html
    assert "Fresh haldi every time" in html
    assert "https://wa.me/919000000009?text=" in html


def test_category_page_and_filters(client: TestClient, world: World) -> None:
    r = client.get("/c/spices-masala")
    assert r.status_code == 200
    assert "Haldi Powder" in r.text and "Clay Diya" not in r.text
    r = client.get("/c/spices-masala?min=40&max=50")
    assert "Other Jeera" in r.text and "Haldi Powder" not in r.text
    assert 'name="robots" content="noindex,follow"' in r.text  # filtered pages are not indexed


def test_sorting(client: TestClient, world: World) -> None:
    html = client.get("/shop?sort=price_asc").text
    assert html.index("Clay Diya") < html.index("Haldi Powder") < html.index("Other Jeera")
    html = client.get("/shop?sort=price_desc").text
    assert html.index("Other Jeera") < html.index("Haldi Powder")
    assert client.get("/shop?sort=bogus").status_code == 200


def test_in_stock_filter(client: TestClient, world: World, db: Session) -> None:
    for v in world.diya.variants:
        v.inventory.quantity = 0  # type: ignore[union-attr]
    db.commit()
    assert "Clay Diya" in client.get("/shop").text
    assert "Clay Diya" not in client.get("/shop?in_stock=1").text


def test_htmx_listing_returns_fragment(client: TestClient, world: World) -> None:
    r = client.get("/shop?sort=name", headers={**HX, "HX-Target": "results"})
    assert r.status_code == 200
    assert r.text.lstrip().startswith(("{%", "<section")) or '<section id="results"' in r.text
    assert "<html" not in r.text


def test_pagination(client: TestClient, world: World, db: Session) -> None:
    for i in range(30):
        p = Product(
            store_id=world.store.id,
            category_id=world.puja.id,
            slug=f"item-{i}",
            name=f"Item {i:02d}",
            status=ProductStatus.APPROVED,
        )
        db.add(p)
        db.flush()
        from tests.web.conftest import _variant

        _variant(db, p, "1 piece", 1000 + i, 5, pieces=1)
    db.commit()
    page1 = client.get("/shop?sort=name").text
    page2 = client.get("/shop?sort=name&page=2").text
    assert "Item 00" in page1 and "Item 29" not in page1
    assert "Item 29" in page2
    assert 'rel="next"' in page1
    assert client.get("/shop?page=999").status_code == 200  # clamps to the last page


def test_search_english_hindi_and_keywords(client: TestClient, world: World) -> None:
    assert "Haldi Powder" in client.get("/search?q=haldi").text
    assert "Haldi Powder" in client.get("/search?q=turmeric").text
    assert "Haldi Powder" in client.get("/search?q=हल्दी").text
    html = client.get("/search?q=zzzz").text
    assert "No products found" in html
    assert 'content="noindex,follow"' in html


def test_search_treats_wildcards_literally(client: TestClient, world: World) -> None:
    html = client.get("/search?q=%25").text  # "%" must not match everything
    assert "Haldi Powder" not in html


def test_hidden_products_are_not_visible(client: TestClient, world: World, db: Session) -> None:
    world.haldi.status = ProductStatus.PENDING
    db.commit()
    assert client.get("/p/haldi-powder").status_code == 404
    assert "Haldi Powder" not in client.get("/shop").text
    assert "Haldi Powder" not in client.get("/search?q=haldi").text


def test_product_page(client: TestClient, world: World) -> None:
    r = client.get("/p/haldi-powder")
    assert r.status_code == 200
    html = r.text
    assert "₹22" in html  # first in-stock size
    assert "100 g" in html and "250 g" in html
    assert "Add to cart" in html and "Buy now" in html
    assert "Check delivery to your area" in html
    assert "You may also need" in html and "Other Jeera" in html  # related products
    r = client.get(f"/p/haldi-powder?v={world.haldi_250.id}")
    assert "₹50" in r.text and "MRP ₹55" in r.text and "Only 3 left" in r.text


def test_buybox_fragment(client: TestClient, world: World) -> None:
    r = client.get(f"/p/haldi-powder/buybox?v={world.haldi_250.id}", headers=HX)
    assert r.status_code == 200 and "<html" not in r.text and 'id="buybox"' in r.text


def test_missing_product_image_uses_placeholder(client: TestClient, world: World) -> None:
    assert 'class="ph"' in client.get("/p/haldi-powder").text


def test_product_json_ld_and_meta(client: TestClient, world: World) -> None:
    html = client.get("/p/haldi-powder").text
    data = _jsonld(html)
    product = next(d for d in data if d["@type"] == "Product")
    assert product["name"] == "Haldi Powder"
    offers = {o["name"]: o for o in product["offers"]}
    assert offers["250 g"]["price"] == "50.00" and offers["250 g"]["priceCurrency"] == "INR"
    assert offers["100 g"]["availability"].endswith("InStock")
    crumbs = next(d for d in data if d["@type"] == "BreadcrumbList")
    assert crumbs["itemListElement"][-1]["name"] == "Haldi Powder"
    assert '<link rel="canonical" href="https://shop.example.test/p/haldi-powder">' in html
    assert '<meta property="og:type" content="product">' in html
    assert "<title>Haldi Powder | Pansari Bhandar, Gomia</title>" in html


def test_local_business_schema_only_publishes_entered_facts(
    client: TestClient, world: World, db: Session
) -> None:
    data = next(d for d in _jsonld(client.get("/").text) if d["@type"] == "GroceryStore")
    assert "telephone" not in data and "openingHours" not in data
    save_site_settings(db, {"phone": "9000000009", "opening_hours": ["Mo-Sa 08:00-21:00"]})
    db.commit()
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    data = next(d for d in _jsonld(client.get("/").text) if d["@type"] == "GroceryStore")
    assert data["telephone"] == "+919000000009"
    assert data["openingHours"] == ["Mo-Sa 08:00-21:00"]


def test_robots_and_sitemap(client: TestClient, world: World, app) -> None:  # type: ignore[no-untyped-def]
    assert "Disallow: /" in client.get("/robots.txt").text  # non-production: keep out of search
    app.state.settings.environment = "production"
    robots = client.get("/robots.txt").text
    assert (
        "Disallow: /admin" in robots and "Sitemap: https://shop.example.test/sitemap.xml" in robots
    )
    sm = client.get("/sitemap.xml")
    assert sm.headers["content-type"].startswith("application/xml")
    assert "https://shop.example.test/p/haldi-powder" in sm.text
    assert "https://shop.example.test/c/spices-masala" in sm.text


def test_pincode_check(client: TestClient, world: World) -> None:
    r = client.get("/delivery/check?pincode=829111", headers=HX)
    assert "Delivery available" in r.text and "₹20" in r.text
    r = client.get("/delivery/check?pincode=110001", headers=HX)
    assert "do not deliver to 110001" in r.text
    r = client.get("/delivery/check?pincode=12ab", headers=HX)
    assert "valid 6-digit pincode" in r.text


def test_info_pages(client: TestClient, world: World) -> None:
    for path in [
        "/about",
        "/contact",
        "/delivery",
        "/cart",
        "/login",
        "/register",
        "/seller/register",
    ]:
        assert client.get(path).status_code == 200, path
    assert "829111" in client.get("/delivery").text


def test_not_found_pages(client: TestClient, world: World) -> None:
    for path in ["/p/does-not-exist", "/c/nope", "/nothing-here"]:
        r = client.get(path)
        assert r.status_code == 404 and "Page not found" in r.text
