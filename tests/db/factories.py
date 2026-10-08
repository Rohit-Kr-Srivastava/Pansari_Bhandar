"""Small helpers that build valid rows so each test only states what it cares about."""

from itertools import count

from sqlalchemy.orm import Session

from app.db.models import (
    Category,
    Inventory,
    Product,
    ProductVariant,
    SellerProfile,
    SellerStatus,
    Store,
    UnitType,
    User,
    UserRole,
)

_n = count(1)


def make_user(session: Session, role: UserRole = UserRole.CUSTOMER, **kw: object) -> User:
    i = next(_n)
    data: dict[str, object] = {
        "email": f"user{i}@example.test",
        "password_hash": "!",  # unusable hash; real hashing arrives in Phase 2
        "full_name": f"Sample User {i}",
        "role": role,
    }
    data.update(kw)
    user = User(**data)
    session.add(user)
    session.flush()
    return user


def make_store(session: Session, approved: bool = True) -> Store:
    i = next(_n)
    seller = make_user(session, UserRole.SELLER)
    profile = SellerProfile(
        user_id=seller.id,
        business_name=f"Sample Seller {i}",
        status=SellerStatus.APPROVED if approved else SellerStatus.PENDING,
    )
    session.add(profile)
    session.flush()
    store = Store(seller_profile_id=profile.id, slug=f"sample-store-{i}", name=f"Sample Store {i}")
    session.add(store)
    session.flush()
    return store


def make_category(session: Session, parent: Category | None = None, **kw: object) -> Category:
    i = next(_n)
    cat = Category(
        slug=f"cat-{i}", name=f"Category {i}", parent_id=parent.id if parent else None, **kw
    )
    session.add(cat)
    session.flush()
    return cat


def make_product(
    session: Session, store: Store | None = None, category: Category | None = None, **kw: object
) -> Product:
    i = next(_n)
    store = store or make_store(session)
    category = category or make_category(session)
    data: dict[str, object] = {
        "store_id": store.id,
        "category_id": category.id,
        "slug": f"product-{i}",
        "name": f"Sample Product {i}",
    }
    data.update(kw)
    product = Product(**data)
    session.add(product)
    session.flush()
    return product


def make_variant(
    session: Session,
    product: Product,
    label: str = "250 g",
    price: int = 5500,
    stock: int | None = 20,
    **kw: object,
) -> ProductVariant:
    i = next(_n)
    data: dict[str, object] = {
        "product_id": product.id,
        "sku": f"SKU-{i}",
        "label": label,
        "unit_type": UnitType.WEIGHT,
        "weight_grams": 250,
        "price_paise": price,
    }
    data.update(kw)
    variant = ProductVariant(**data)
    session.add(variant)
    session.flush()
    if stock is not None:
        session.add(Inventory(variant_id=variant.id, quantity=stock))
        session.flush()
    return variant
