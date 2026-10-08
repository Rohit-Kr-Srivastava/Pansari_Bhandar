import pytest
from sqlalchemy.orm import Session

from app.db.models import Category, DeliverySlot, Product, ProductVariant, SellerDeliveryZone, User
from app.services.settings import load_site_settings
from scripts.seed_dev import PIECE_PRODUCTS, WEIGHT_PRODUCTS, seed


def test_seed_creates_demo_catalogue_and_refuses_to_run_twice(session: Session) -> None:
    logins = seed(session, "829111", accounts=True)
    assert session.query(Category).count() == 10
    assert session.query(Product).count() == len(WEIGHT_PRODUCTS) + len(PIECE_PRODUCTS)
    weights = {v.weight_grams for v in session.query(ProductVariant) if v.weight_grams is not None}
    assert weights == {50, 100, 250, 500, 1000}
    assert session.query(SellerDeliveryZone).count() == 1
    assert session.query(DeliverySlot).count() == 2
    assert "Demo" in load_site_settings(session).story
    # demo logins get random passwords, never a fixed one
    assert set(logins) == {
        "admin@example.test",
        "customer@example.test",
        "demo-seller@example.test",
    }
    assert all(len(p) >= 12 for p in logins.values())
    assert session.query(User).count() == 3
    with pytest.raises(SystemExit):
        seed(session)


def test_seed_refuses_production(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with pytest.raises(SystemExit):
        seed(session)
