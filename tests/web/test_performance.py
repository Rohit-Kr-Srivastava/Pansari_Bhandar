"""Query-count budgets. A page must not issue more queries when the catalogue grows (no N+1)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from app.db.models import Product, ProductStatus
from tests.web.conftest import World, _variant, login
from tests.web.test_cart_checkout import add


class QueryCounter:
    def __init__(self, engine: Engine) -> None:
        self.engine, self.statements = engine, []  # type: ignore[var-annotated]

    def __enter__(self) -> "QueryCounter":
        event.listen(self.engine, "before_cursor_execute", self._on)
        return self

    def __exit__(self, *exc: object) -> None:
        event.remove(self.engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, *a) -> None:  # type: ignore[no-untyped-def]
        self.statements.append(statement)

    @property
    def count(self) -> int:
        return len(self.statements)


def grow_catalogue(db: Session, world: World, n: int) -> None:
    for i in range(n):
        p = Product(
            store_id=world.store.id,
            category_id=world.spices.id,
            slug=f"bulk-{i}-{len(world.store.name)}-{db.bind.url.database or ''}-{n}",  # type: ignore[union-attr]
            name=f"Bulk {i}",
            status=ProductStatus.APPROVED,
            is_featured=True,
        )
        db.add(p)
        db.flush()
        for label, grams in [("100 g", 100), ("250 g", 250)]:
            _variant(db, p, label, 1000 + i, 5, grams=grams)
    db.commit()


PUBLIC_PAGES = ["/", "/shop", "/c/spices-masala", "/search?q=l", "/p/haldi-powder"]


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_public_pages_do_not_n_plus_one(
    client: TestClient, world: World, db: Session, web_engine: Engine, path: str
) -> None:
    client.get(path)  # warm caches (settings, navigation)
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    with QueryCounter(web_engine) as small:
        assert client.get(path).status_code == 200
    grow_catalogue(db, world, 20)
    client.app.state.cache.clear()  # type: ignore[attr-defined]
    with QueryCounter(web_engine) as large:
        assert client.get(path).status_code == 200
    assert large.count == small.count, f"{path}: {small.count} -> {large.count} queries"
    budget = 20 if path == "/" else 14  # the homepage runs one indexed query per product row
    assert large.count <= budget, f"{path} uses {large.count} queries"


def test_cart_and_checkout_query_budget(
    client: TestClient, world: World, db: Session, web_engine: Engine
) -> None:
    login(client, world.customer.phone or "")
    add(client, world.haldi_100.id, 1)
    with QueryCounter(web_engine) as one:
        client.get("/cart")
    add(client, world.haldi_250.id, 1)
    add(client, world.diya_1.id, 1)
    with QueryCounter(web_engine) as three:
        client.get("/cart")
    assert three.count == one.count
    with QueryCounter(web_engine) as chk:
        assert client.get("/checkout").status_code == 200
    assert chk.count <= 16


@pytest.mark.parametrize(
    "path",
    [
        "/admin/products",
        "/admin/inventory",
        "/admin/orders",
        "/admin",
        "/seller/products",
        "/seller/inventory",
    ],
)
def test_dashboard_pages_do_not_n_plus_one(
    client: TestClient, world: World, db: Session, web_engine: Engine, path: str
) -> None:
    login(client, world.admin.phone if path.startswith("/admin") else world.seller.phone or "")
    with QueryCounter(web_engine) as small:
        assert client.get(path).status_code == 200
    grow_catalogue(db, world, 15)
    with QueryCounter(web_engine) as large:
        assert client.get(path).status_code == 200
    assert large.count == small.count, f"{path}: {small.count} -> {large.count} queries"


def test_static_assets_are_small_and_cacheable(client: TestClient, world: World) -> None:
    html = client.get("/").text
    import re

    css = re.search(r'href="(/static/css/app\.css\?v=\w+)"', html).group(1)  # type: ignore[union-attr]
    js = re.search(r'src="(/static/js/app\.js\?v=\w+)"', html).group(1)  # type: ignore[union-attr]
    r = client.get(css)
    assert "immutable" in r.headers["cache-control"] and len(r.content) < 40_000
    r = client.get(js)
    assert len(r.content) < 8_000
    scripts = re.findall(r"<script[^>]+src=", html)
    assert len(scripts) == 2  # htmx + our file: no other JavaScript
    assert len(html.encode()) < 120_000


def test_indexes_exist_for_hot_queries(web_engine: Engine) -> None:
    from sqlalchemy import inspect

    insp = inspect(web_engine)

    def cols(table: str) -> set[tuple[str, ...]]:
        return {tuple(i["column_names"]) for i in insp.get_indexes(table)}

    assert ("status", "category_id") in cols("products")
    assert ("is_featured", "status") in cols("products")
    assert ("product_id", "is_active") in cols("product_variants")
    assert ("status", "placed_at") in cols("orders") and ("user_id", "placed_at") in cols("orders")
    assert ("order_id",) in cols("order_items") and ("store_id",) in cols("order_items")
    assert ("pincode", "is_active") in cols("seller_delivery_zones")
    assert ("quantity",) in cols("inventory")
