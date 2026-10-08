"""Fixtures for HTTP-level tests: a real app on a throwaway database.

SQLite file per test by default; set TEST_DATABASE_URL to run against PostgreSQL.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

import app.db.models  # noqa: F401
from app.core.config import Settings
from app.core.security import hash_password
from app.db.base import Base
from app.db.models import (
    Category,
    CustomerProfile,
    DeliverySlot,
    Inventory,
    Product,
    ProductStatus,
    ProductVariant,
    SellerDeliveryZone,
    SellerProfile,
    SellerStatus,
    Store,
    UnitType,
    User,
    UserRole,
)
from app.main import create_app
from tests.conftest import PG_URL

PASSWORD = "test-pass-123"
PINCODE = "829111"


@pytest.fixture
def web_engine(tmp_path: Path) -> Iterator[Engine]:
    if PG_URL:
        eng = create_engine(PG_URL)
        Base.metadata.drop_all(eng)
    else:
        eng = create_engine(
            f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
        )

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(eng)
    yield eng
    if PG_URL:
        Base.metadata.drop_all(eng)
    eng.dispose()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        secret_key="test-secret-key-that-is-long-enough-1234567890",
        database_url="sqlite://",
        media_root=tmp_path / "media",
        site_url="https://shop.example.test",
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest.fixture
def app(settings: Settings, web_engine: Engine) -> FastAPI:
    return create_app(settings, web_engine)


@pytest.fixture
def db(web_engine: Engine) -> Iterator[Session]:
    with sessionmaker(bind=web_engine, expire_on_commit=False)() as s:
        yield s


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True, base_url="http://testserver")


@dataclass
class World:
    customer: User
    seller: User
    other_seller: User
    admin: User
    store: Store
    other_store: Store
    spices: Category
    puja: Category
    haldi: Product
    haldi_100: ProductVariant
    haldi_250: ProductVariant
    diya: Product
    diya_1: ProductVariant
    other_product: Product


def _user(db: Session, phone: str, role: UserRole, name: str) -> User:
    u = User(phone=phone, full_name=name, role=role, password_hash=hash_password(PASSWORD))
    db.add(u)
    db.flush()
    if role == UserRole.CUSTOMER:
        db.add(CustomerProfile(user_id=u.id))
    return u


def _store(db: Session, user: User, slug: str) -> Store:
    p = SellerProfile(user_id=user.id, business_name=slug, status=SellerStatus.APPROVED)
    db.add(p)
    db.flush()
    s = Store(seller_profile_id=p.id, slug=slug, name=slug.title(), pickup_available=True)
    db.add(s)
    db.flush()
    return s


def _variant(
    db: Session,
    product: Product,
    label: str,
    price: int,
    stock: int,
    grams: int | None = None,
    pieces: int | None = None,
    sale: int | None = None,
) -> ProductVariant:
    v = ProductVariant(
        product_id=product.id,
        sku=f"{product.slug}-{label}".upper().replace(" ", ""),
        label=label,
        unit_type=UnitType.WEIGHT if grams else UnitType.PIECE,
        weight_grams=grams,
        piece_count=pieces,
        price_paise=price,
        sale_price_paise=sale,
    )
    db.add(v)
    db.flush()
    db.add(Inventory(variant_id=v.id, quantity=stock))
    db.flush()
    return v


@pytest.fixture
def world(db: Session) -> World:
    customer = _user(db, "9000000001", UserRole.CUSTOMER, "Asha Devi")
    seller = _user(db, "9000000002", UserRole.SELLER, "Ramesh Gupta")
    other_seller = _user(db, "9000000003", UserRole.SELLER, "Other Seller")
    admin = _user(db, "9000000004", UserRole.ADMIN, "Shop Admin")
    store = _store(db, seller, "main-shop")
    other_store = _store(db, other_seller, "other-shop")
    spices = Category(slug="spices-masala", name="Spices & Masala", name_hi="मसाले")
    puja = Category(slug="puja-samagri", name="Puja Samagri", sort_order=1)
    db.add_all([spices, puja])
    db.flush()
    haldi = Product(
        store_id=store.id,
        category_id=spices.id,
        slug="haldi-powder",
        name="Haldi Powder",
        name_hi="हल्दी पाउडर",
        search_keywords="turmeric",
        status=ProductStatus.APPROVED,
        is_featured=True,
    )
    diya = Product(
        store_id=store.id,
        category_id=puja.id,
        slug="clay-diya",
        name="Clay Diya",
        status=ProductStatus.APPROVED,
        is_seasonal=True,
    )
    other = Product(
        store_id=other_store.id,
        category_id=spices.id,
        slug="other-jeera",
        name="Other Jeera",
        status=ProductStatus.APPROVED,
    )
    db.add_all([haldi, diya, other])
    db.flush()
    h100 = _variant(db, haldi, "100 g", 2200, 10, grams=100)
    h250 = _variant(db, haldi, "250 g", 5500, 3, grams=250, sale=5000)
    d1 = _variant(db, diya, "1 piece", 500, 50, pieces=1)
    _variant(db, other, "100 g", 4500, 10, grams=100)
    db.add(
        SellerDeliveryZone(
            store_id=store.id,
            pincode=PINCODE,
            area_name="Gomia Bazar",
            delivery_fee_paise=2000,
            min_order_paise=5000,
            free_delivery_above_paise=50000,
            estimated_days=1,
        )
    )
    db.add(
        DeliverySlot(
            store_id=store.id, label="10 am - 1 pm", start_time=time(10), end_time=time(13)
        )
    )
    db.commit()
    return World(
        customer,
        seller,
        other_seller,
        admin,
        store,
        other_store,
        spices,
        puja,
        haldi,
        h100,
        h250,
        diya,
        d1,
        other,
    )


# ---- helpers -----------------------------------------------------------------------------

CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def csrf(client: TestClient, path: str = "/") -> str:
    match = CSRF_RE.search(client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def login(client: TestClient, phone: str, password: str = PASSWORD) -> None:
    token = csrf(client, "/login")
    r = client.post(
        "/login",
        data={"login": phone, "password": password, "csrf_token": token},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text[:500]


def post(client: TestClient, path: str, data: dict | None = None, **kw):  # type: ignore[no-untyped-def]
    data = dict(data or {})
    data.setdefault("csrf_token", csrf(client))
    kw.setdefault("follow_redirects", False)
    return client.post(path, data=data, **kw)


HX = {"HX-Request": "true"}
