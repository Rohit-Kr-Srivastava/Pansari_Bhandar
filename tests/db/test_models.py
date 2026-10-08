import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    Address,
    AdminAction,
    BrandType,
    Cart,
    CartItem,
    CartStatus,
    Delivery,
    DeliveryMethod,
    DeliveryStatus,
    Inventory,
    Order,
    OrderItem,
    Payment,
    Product,
    ProductImage,
    ProductStatus,
    ProductVariant,
    Review,
    SellerDeliveryZone,
    SellerProfile,
    SellerStatus,
    Store,
    UnitType,
    User,
    UserRole,
    Wishlist,
    WishlistItem,
)
from tests.db.factories import make_category, make_product, make_store, make_user, make_variant

EXPECTED_TABLES = {
    "users",
    "customer_profiles",
    "seller_profiles",
    "stores",
    "addresses",
    "categories",
    "products",
    "product_images",
    "product_variants",
    "inventory",
    "carts",
    "cart_items",
    "wishlists",
    "wishlist_items",
    "orders",
    "order_items",
    "payments",
    "delivery",
    "seller_delivery_zones",
    "reviews",
    "admin_actions",
    "delivery_slots",
    "site_settings",
    "testimonials",
}


def rejected(session: Session, *objs: object) -> None:
    """Assert the database refuses these rows, then clear the failed transaction."""
    session.add_all(objs)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_all_expected_tables_exist(session: Session) -> None:
    tables = set(inspect(session.get_bind()).get_table_names()) - {"alembic_version"}
    assert tables == EXPECTED_TABLES
    assert set(Base.metadata.tables) == EXPECTED_TABLES


# ---- users and profiles ------------------------------------------------------------------


def test_user_needs_email_or_phone(session: Session) -> None:
    rejected(session, User(password_hash="!", full_name="No Contact"))


def test_user_with_phone_only_is_allowed(session: Session) -> None:
    session.add(User(phone="9876543210", password_hash="!", full_name="Phone Only"))
    session.flush()


def test_email_unique_ignoring_case(session: Session) -> None:
    make_user(session, email="Asha@Example.test")
    session.commit()
    rejected(session, User(email="asha@example.TEST", password_hash="!", full_name="Dup"))


def test_phone_unique_and_length(session: Session) -> None:
    make_user(session, phone="9876543210")
    session.commit()
    rejected(session, User(phone="9876543210", password_hash="!", full_name="Dup"))
    rejected(session, User(phone="123", password_hash="!", full_name="Short"))


def test_invalid_role_rejected_by_database(session: Session) -> None:
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                "INSERT INTO users (email, password_hash, full_name, role) "
                "VALUES ('x@example.test', '!', 'X', 'boss')"
            )
        )
    session.rollback()


def test_seller_defaults_to_pending_and_has_one_store(session: Session) -> None:
    seller = make_user(session, UserRole.SELLER)
    profile = SellerProfile(user_id=seller.id, business_name="Demo Seller")
    session.add(profile)
    session.flush()
    session.refresh(profile)
    assert profile.status is SellerStatus.PENDING
    session.add(Store(seller_profile_id=profile.id, slug="one", name="One"))
    session.commit()
    rejected(session, Store(seller_profile_id=profile.id, slug="two", name="Two"))


def test_seller_profile_one_per_user(session: Session) -> None:
    seller = make_user(session, UserRole.SELLER)
    session.add(SellerProfile(user_id=seller.id, business_name="A"))
    session.commit()
    rejected(session, SellerProfile(user_id=seller.id, business_name="B"))


def test_address_pincode_and_single_default(session: Session) -> None:
    user = make_user(session)
    base = dict(user_id=user.id, recipient_name="A", phone="9876543210", line1="Line")
    session.add(Address(pincode="829111", is_default=True, **base))
    session.commit()
    rejected(session, Address(pincode="82911", **base))
    rejected(session, Address(pincode="829112", is_default=True, **base))
    session.add(Address(pincode="829112", is_default=False, **base))
    session.flush()


# ---- catalogue ---------------------------------------------------------------------------


def test_category_cannot_be_its_own_parent(session: Session) -> None:
    cat = make_category(session)
    session.commit()
    cat.parent_id = cat.id
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_category_hierarchy(session: Session) -> None:
    parent = make_category(session)
    child = make_category(session, parent)
    session.commit()
    assert child.parent is parent and parent.children == [child]


def test_brand_is_optional_and_defaults_to_no_brand(session: Session) -> None:
    product = make_product(session, name="Turmeric Powder")
    session.refresh(product)
    assert product.brand is None
    assert product.brand_type is BrandType.NO_BRAND
    assert product.status is ProductStatus.DRAFT


@pytest.mark.parametrize("brand_type", [BrandType.GENERIC, BrandType.LOCAL])
def test_generic_and_local_products_need_no_brand(session: Session, brand_type: BrandType) -> None:
    make_product(session, brand_type=brand_type)


def test_branded_product_needs_brand_name(session: Session) -> None:
    store, cat = make_store(session), make_category(session)
    session.commit()
    rejected(
        session,
        Product(
            store_id=store.id, category_id=cat.id, slug="b", name="B", brand_type=BrandType.BRANDED
        ),
    )


def test_rejected_product_needs_reason(session: Session) -> None:
    product = make_product(session)
    session.commit()
    product.status = ProductStatus.REJECTED
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_product_slug_unique(session: Session) -> None:
    store, cat = make_store(session), make_category(session)
    make_product(session, store, cat, slug="haldi")
    session.commit()
    rejected(session, Product(store_id=store.id, category_id=cat.id, slug="haldi", name="Dup"))


def test_product_info_json_round_trip(session: Session) -> None:
    info = {"net_quantity": "100 g", "ingredients": "as printed by the seller"}
    product = make_product(session, product_info=info)
    session.commit()
    session.refresh(product)
    assert product.product_info == info


def test_one_primary_image_per_product(session: Session) -> None:
    product = make_product(session)
    session.add(ProductImage(product_id=product.id, path="a.jpg", is_primary=True))
    session.commit()
    rejected(session, ProductImage(product_id=product.id, path="b.jpg", is_primary=True))


@pytest.mark.parametrize(
    "grams,label", [(50, "50 g"), (100, "100 g"), (250, "250 g"), (500, "500 g"), (1000, "1 kg")]
)
def test_weight_variants(session: Session, grams: int, label: str) -> None:
    variant = make_variant(session, make_product(session), label=label, weight_grams=grams)
    assert variant.weight_grams == grams


def test_piece_and_pack_variants(session: Session) -> None:
    product = make_product(session)
    make_variant(
        session, product, "1 piece", unit_type=UnitType.PIECE, weight_grams=None, piece_count=1
    )
    make_variant(
        session, product, "Pack of 50", unit_type=UnitType.PACK, weight_grams=None, piece_count=50
    )


def test_variant_quantity_must_match_unit_type(session: Session) -> None:
    product = make_product(session)
    session.commit()
    base = dict(product_id=product.id, price_paise=1000)
    rejected(session, ProductVariant(sku="w0", label="w", unit_type=UnitType.WEIGHT, **base))
    rejected(
        session,
        ProductVariant(sku="p0", label="p", unit_type=UnitType.PIECE, weight_grams=100, **base),
    )
    rejected(
        session,
        ProductVariant(
            sku="w1", label="w1", unit_type=UnitType.WEIGHT, weight_grams=100, piece_count=2, **base
        ),
    )
    rejected(
        session,
        ProductVariant(sku="w2", label="w2", unit_type=UnitType.WEIGHT, weight_grams=0, **base),
    )


def test_sale_price_must_be_below_price(session: Session) -> None:
    product = make_product(session)
    make_variant(session, product, "ok", price=5000, sale_price_paise=4500)
    session.commit()
    rejected(
        session,
        ProductVariant(
            product_id=product.id,
            sku="s1",
            label="eq",
            unit_type=UnitType.WEIGHT,
            weight_grams=100,
            price_paise=5000,
            sale_price_paise=5000,
        ),
    )
    rejected(
        session,
        ProductVariant(
            product_id=product.id,
            sku="s2",
            label="neg",
            unit_type=UnitType.WEIGHT,
            weight_grams=100,
            price_paise=5000,
            sale_price_paise=-1,
        ),
    )


def test_negative_price_rejected(session: Session) -> None:
    product = make_product(session)
    session.commit()
    rejected(
        session,
        ProductVariant(
            product_id=product.id,
            sku="n",
            label="n",
            unit_type=UnitType.WEIGHT,
            weight_grams=100,
            price_paise=-1,
        ),
    )


def test_variant_label_and_sku_unique(session: Session) -> None:
    product = make_product(session)
    make_variant(session, product, "250 g", sku="SKU-A")
    session.commit()
    rejected(
        session,
        ProductVariant(
            product_id=product.id,
            sku="SKU-B",
            label="250 g",
            unit_type=UnitType.WEIGHT,
            weight_grams=250,
            price_paise=1,
        ),
    )
    rejected(
        session,
        ProductVariant(
            product_id=product.id,
            sku="SKU-A",
            label="500 g",
            unit_type=UnitType.WEIGHT,
            weight_grams=500,
            price_paise=1,
        ),
    )


def test_stock_is_stored_separately_per_variant(session: Session) -> None:
    product = make_product(session)
    small = make_variant(session, product, "100 g", weight_grams=100, stock=5)
    large = make_variant(session, product, "1 kg", weight_grams=1000, stock=0)
    session.commit()
    assert small.inventory is not None and small.inventory.quantity == 5
    assert large.inventory is not None and large.inventory.quantity == 0


def test_inventory_rules(session: Session) -> None:
    variant = make_variant(session, make_product(session), stock=None)
    other = make_variant(session, make_product(session), stock=None)
    session.commit()
    rejected(session, Inventory(variant_id=variant.id, quantity=-1))
    rejected(session, Inventory(variant_id=variant.id, quantity=2, reserved=3))
    session.add(Inventory(variant_id=other.id, quantity=2, reserved=2))
    session.flush()


def test_deleting_product_removes_variants_and_stock(session: Session) -> None:
    product = make_product(session)
    make_variant(session, product)
    session.commit()
    session.delete(product)
    session.commit()
    assert session.query(ProductVariant).count() == 0
    assert session.query(Inventory).count() == 0


def test_store_with_products_cannot_be_deleted(session: Session) -> None:
    product = make_product(session)
    session.commit()
    with pytest.raises(IntegrityError):  # database-level delete, bypassing ORM cascades
        session.execute(text("DELETE FROM stores"))
    session.rollback()
    assert session.get(Store, product.store_id) is not None


# ---- delivery zones ----------------------------------------------------------------------


def test_delivery_zone_rules(session: Session) -> None:
    store = make_store(session)
    session.add(
        SellerDeliveryZone(
            store_id=store.id,
            pincode="829111",
            delivery_fee_paise=2000,
            min_order_paise=10000,
            free_delivery_above_paise=50000,
        )
    )
    session.commit()
    rejected(session, SellerDeliveryZone(store_id=store.id, pincode="829111"))
    rejected(session, SellerDeliveryZone(store_id=store.id, pincode="82911"))
    rejected(
        session, SellerDeliveryZone(store_id=store.id, pincode="829112", delivery_fee_paise=-1)
    )


# ---- cart and wishlist -------------------------------------------------------------------


def test_cart_needs_owner_and_one_active_cart_per_user(session: Session) -> None:
    user = make_user(session)
    rejected(session, Cart())
    user = make_user(session)
    session.add(Cart(user_id=user.id))
    session.commit()
    rejected(session, Cart(user_id=user.id))
    session.add(Cart(user_id=user.id, status=CartStatus.CONVERTED))
    session.add(Cart(session_token="guest-token"))
    session.flush()


def test_cart_item_rules(session: Session) -> None:
    variant = make_variant(session, make_product(session))
    cart = Cart(session_token="t1")
    session.add(cart)
    session.flush()
    session.add(CartItem(cart_id=cart.id, variant_id=variant.id, quantity=2))
    session.commit()
    rejected(session, CartItem(cart_id=cart.id, variant_id=variant.id, quantity=1))
    other = make_variant(session, make_product(session))
    session.commit()
    rejected(session, CartItem(cart_id=cart.id, variant_id=other.id, quantity=0))
    rejected(session, CartItem(cart_id=cart.id, variant_id=other.id, quantity=100))


def test_wishlist_one_per_user_and_no_duplicate_products(session: Session) -> None:
    user, product = make_user(session), make_product(session)
    wishlist = Wishlist(user_id=user.id)
    session.add(wishlist)
    session.flush()
    session.add(WishlistItem(wishlist_id=wishlist.id, product_id=product.id))
    session.commit()
    rejected(session, WishlistItem(wishlist_id=wishlist.id, product_id=product.id))
    rejected(session, Wishlist(user_id=user.id))


# ---- orders ------------------------------------------------------------------------------


def _order(session: Session, **kw: object) -> Order:
    user = make_user(session)
    data: dict[str, object] = dict(
        order_no=f"PB-{make_user.__name__}-{id(kw)}",
        user_id=user.id,
        subtotal_paise=10000,
        delivery_fee_paise=2000,
        discount_paise=500,
        total_paise=11500,
    )
    data.update(kw)
    return Order(**data)


def test_order_total_must_match_parts(session: Session) -> None:
    session.add(_order(session, order_no="PB-1"))
    session.commit()
    rejected(session, _order(session, order_no="PB-2", total_paise=1))
    rejected(session, _order(session, order_no="PB-3", discount_paise=-5, total_paise=12005))


def test_order_status_defaults_to_pending_and_checkout_token_unique(session: Session) -> None:
    order = _order(session, order_no="PB-150", checkout_token="tok-1")
    session.add(order)
    session.commit()
    session.refresh(order)
    assert order.status.value == "pending"
    rejected(session, _order(session, order_no="PB-151", checkout_token="tok-1"))


def test_delivery_slot_must_end_after_start(session: Session) -> None:
    from datetime import time

    from app.db.models import DeliverySlot

    store = make_store(session)
    session.commit()
    rejected(
        session,
        DeliverySlot(store_id=store.id, label="bad", start_time=time(13), end_time=time(10)),
    )


def test_order_number_unique(session: Session) -> None:
    session.add(_order(session, order_no="PB-100"))
    session.commit()
    rejected(session, _order(session, order_no="PB-100"))


def test_order_items_keep_snapshots_when_product_is_deleted(session: Session) -> None:
    product = make_product(session, name="Whole Jeera")
    variant = make_variant(session, product, "100 g", price=4500)
    order = _order(
        session,
        order_no="PB-200",
        subtotal_paise=9000,
        delivery_fee_paise=0,
        discount_paise=0,
        total_paise=9000,
    )
    session.add(order)
    session.flush()
    item = OrderItem(
        order_id=order.id,
        store_id=product.store_id,
        product_id=product.id,
        variant_id=variant.id,
        product_name="Whole Jeera",
        variant_label="100 g",
        unit_price_paise=4500,
        quantity=2,
        line_total_paise=9000,
    )
    session.add(item)
    session.commit()
    session.delete(product)
    session.commit()
    session.refresh(item)
    assert item.product_id is None and item.variant_id is None
    assert item.product_name == "Whole Jeera" and item.line_total_paise == 9000


def test_order_item_line_total_and_quantity_rules(session: Session) -> None:
    store = make_store(session)
    order = _order(session, order_no="PB-300")
    session.add(order)
    session.commit()
    base = dict(order_id=order.id, store_id=store.id, product_name="P", variant_label="1")
    rejected(session, OrderItem(unit_price_paise=100, quantity=2, line_total_paise=999, **base))
    rejected(session, OrderItem(unit_price_paise=100, quantity=0, line_total_paise=0, **base))


def test_payment_defaults_to_cod_pending(session: Session) -> None:
    order = _order(session, order_no="PB-400")
    session.add(order)
    session.flush()
    payment = Payment(order_id=order.id, amount_paise=11500)
    session.add(payment)
    session.commit()
    session.refresh(payment)
    assert payment.method.value == "cod" and payment.status.value == "pending"
    rejected(session, Payment(order_id=order.id, amount_paise=-1))


def test_delivery_one_row_per_store_per_order_and_pickup_status(session: Session) -> None:
    store = make_store(session)
    order = _order(session, order_no="PB-500")
    session.add(order)
    session.flush()
    session.add(
        Delivery(
            order_id=order.id,
            store_id=store.id,
            method=DeliveryMethod.PICKUP,
            status=DeliveryStatus.READY,
        )
    )
    session.commit()
    rejected(session, Delivery(order_id=order.id, store_id=store.id))
    other = make_store(session)
    session.commit()
    rejected(
        session,
        Delivery(
            order_id=order.id,
            store_id=other.id,
            method=DeliveryMethod.PICKUP,
            status=DeliveryStatus.OUT_FOR_DELIVERY,
        ),
    )


# ---- reviews and audit trail -------------------------------------------------------------


def test_review_rating_and_one_per_user(session: Session) -> None:
    user, product = make_user(session), make_product(session)
    session.add(Review(product_id=product.id, user_id=user.id, rating=5))
    session.commit()
    rejected(session, Review(product_id=product.id, user_id=user.id, rating=4))
    other = make_user(session)
    session.commit()
    rejected(session, Review(product_id=product.id, user_id=other.id, rating=6))
    rejected(session, Review(product_id=product.id, user_id=other.id, rating=0))


def test_admin_action_survives_actor_deletion(session: Session) -> None:
    admin = make_user(session, UserRole.ADMIN)
    action = AdminAction(
        actor_id=admin.id,
        action="seller.approve",
        entity_type="seller_profile",
        entity_id=1,
        before={"status": "pending"},
        after={"status": "approved"},
    )
    session.add(action)
    session.commit()
    session.execute(text("DELETE FROM users WHERE id = :i"), {"i": admin.id})
    session.commit()
    session.refresh(action)
    assert action.actor_id is None and action.after == {"status": "approved"}


def test_timestamps_are_set(session: Session) -> None:
    user = make_user(session)
    session.commit()
    session.refresh(user)
    assert user.created_at is not None and user.updated_at >= user.created_at
