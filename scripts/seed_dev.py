"""Load clearly-marked DEMO data for local development. Never run this against production.

Usage:  python -m scripts.seed_dev [--pincode 8XXXXX] [--accounts]

Creates the ten launch categories, a demo seller with a demo store, and demo products with
weight and piece variants across every category. Prices are sample values. No phone numbers,
addresses, licence numbers or product claims are invented. A delivery zone and slots are only
created if you pass --pincode. --accounts also creates a demo admin and a demo customer with
random passwords that are printed once.
"""

import argparse
import os
import secrets
from datetime import time

from sqlalchemy.orm import Session

from app.core.security import hash_password
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
from app.db.session import get_engine
from app.services.settings import save_site_settings

CATEGORIES = [
    ("spices-masala", "Spices & Masala", "मसाले"),
    ("grocery-ration", "Grocery & Ration", "राशन"),
    ("whole-spices", "Whole Spices", "साबुत मसाले"),
    ("pansari-jadi-buti", "Pansari / Jadi-Buti", "पंसारी / जड़ी-बूटी"),
    ("puja-samagri", "Puja Samagri", "पूजा सामग्री"),
    ("shringar-ornaments", "Shringar & Ornaments", "श्रृंगार"),
    ("gifts", "Gifts", "उपहार"),
    ("seasonal-festival", "Seasonal & Festival", "त्योहार"),
    ("household-daily-needs", "Household & Daily Needs", "घरेलू सामान"),
    ("other-products", "Other Products", "अन्य सामान"),
]

WEIGHTS = [(50, "50 g"), (100, "100 g"), (250, "250 g"), (500, "500 g"), (1000, "1 kg")]

# slug, name, Hindi name, category, search words, price per 100 g in paise (weight) or None
WEIGHT_PRODUCTS = [
    ("haldi-powder", "Haldi Powder", "हल्दी पाउडर", "spices-masala", "haldi turmeric", 2200),
    ("whole-jeera", "Whole Jeera", "साबुत जीरा", "whole-spices", "jeera cumin zeera", 4500),
    (
        "lal-mirch-powder",
        "Lal Mirch Powder",
        "लाल मिर्च पाउडर",
        "spices-masala",
        "red chilli mirchi",
        3000,
    ),
    ("dhaniya-powder", "Dhaniya Powder", "धनिया पाउडर", "spices-masala", "coriander", 2000),
    ("garam-masala", "Garam Masala", "गरम मसाला", "spices-masala", "masala", 8000),
    ("kali-mirch", "Kali Mirch", "काली मिर्च", "whole-spices", "black pepper", 12000),
    ("methi-dana", "Methi Dana", "मेथी दाना", "whole-spices", "fenugreek", 1800),
    ("ajwain", "Ajwain", "अजवाइन", "pansari-jadi-buti", "carom", 3500),
    ("mulethi", "Mulethi", "मुलेठी", "pansari-jadi-buti", "licorice", 6000),
    ("arhar-dal", "Arhar Dal", "अरहर दाल", "grocery-ration", "toor dal", 1600),
    ("chana-dal", "Chana Dal", "चना दाल", "grocery-ration", "dal", 1100),
    ("sendha-namak", "Sendha Namak", "सेंधा नमक", "grocery-ration", "rock salt", 600),
]

# slug, name, Hindi name, category, words, [(label, unit, count, price paise)], seasonal
PIECE_PRODUCTS = [
    (
        "clay-diya",
        "Clay Diya",
        "मिट्टी का दीया",
        "puja-samagri",
        "diya deepak",
        [("1 piece", UnitType.PIECE, 1, 500), ("Pack of 12", UnitType.PACK, 12, 6000)],
        True,
    ),
    (
        "agarbatti-pack",
        "Agarbatti",
        "अगरबत्ती",
        "puja-samagri",
        "incense",
        [("Pack of 50", UnitType.PACK, 50, 4000)],
        False,
    ),
    (
        "kapoor-tablets",
        "Kapoor Tablets",
        "कपूर",
        "puja-samagri",
        "camphor",
        [("Pack of 20", UnitType.PACK, 20, 3000)],
        False,
    ),
    (
        "roli-moli-set",
        "Roli Moli Set",
        "रोली मौली",
        "puja-samagri",
        "kalava tilak",
        [("1 set", UnitType.PIECE, 1, 2500)],
        False,
    ),
    (
        "glass-bangles",
        "Glass Bangles",
        "कांच की चूड़ियाँ",
        "shringar-ornaments",
        "chudi",
        [("Set of 12", UnitType.PACK, 12, 12000)],
        False,
    ),
    (
        "sindoor",
        "Sindoor",
        "सिंदूर",
        "shringar-ornaments",
        "kumkum",
        [("1 box", UnitType.PIECE, 1, 3000)],
        False,
    ),
    (
        "bindi-pack",
        "Bindi Pack",
        "बिंदी",
        "shringar-ornaments",
        "bindi",
        [("1 card", UnitType.PIECE, 1, 2000)],
        False,
    ),
    (
        "puja-thali",
        "Puja Thali",
        "पूजा थाली",
        "gifts",
        "thali gift",
        [("1 piece", UnitType.PIECE, 1, 35000)],
        True,
    ),
    (
        "dry-fruit-gift-box",
        "Dry Fruit Gift Box",
        "ड्राई फ्रूट गिफ्ट बॉक्स",
        "gifts",
        "gift",
        [("1 box", UnitType.PIECE, 1, 75000)],
        True,
    ),
    (
        "rangoli-colours",
        "Rangoli Colours",
        "रंगोली",
        "seasonal-festival",
        "diwali rangoli",
        [("Pack of 5", UnitType.PACK, 5, 9000)],
        True,
    ),
    (
        "matchbox",
        "Matchbox",
        "माचिस",
        "household-daily-needs",
        "machis",
        [("Pack of 10", UnitType.PACK, 10, 2000)],
        False,
    ),
    (
        "cotton-wicks",
        "Cotton Wicks",
        "रुई की बत्ती",
        "puja-samagri",
        "batti",
        [("Pack of 100", UnitType.PACK, 100, 2000)],
        False,
    ),
]


def _require_dev() -> None:
    if os.environ.get("ENVIRONMENT", "").lower() == "production":
        raise SystemExit("Refusing to load demo data with ENVIRONMENT=production.")


def seed(session: Session, pincode: str | None = None, accounts: bool = False) -> dict[str, str]:
    """Returns {email: password} for demo accounts created with accounts=True."""
    _require_dev()
    if session.query(Category).count():
        raise SystemExit("Categories already exist; refusing to seed twice.")

    cats = {}
    for order, (slug, name, name_hi) in enumerate(CATEGORIES):
        cats[slug] = Category(slug=slug, name=name, name_hi=name_hi, sort_order=order)
        session.add(cats[slug])

    user = User(
        email="demo-seller@example.test",
        password_hash="!",  # unusable on purpose
        full_name="Demo Seller",
        role=UserRole.SELLER,
    )
    session.add(user)
    session.flush()
    profile = SellerProfile(
        user_id=user.id, business_name="Demo Store (replace)", status=SellerStatus.APPROVED
    )
    session.add(profile)
    session.flush()
    store = Store(
        seller_profile_id=profile.id,
        slug="demo-store",
        name="Demo Store (replace)",
        pickup_available=True,
        pickup_instructions="Demo text: collect from the counter during opening hours (replace).",
    )
    session.add(store)
    session.flush()

    if pincode:
        session.add(
            SellerDeliveryZone(
                store_id=store.id,
                pincode=pincode,
                area_name="Demo area (replace)",
                delivery_fee_paise=2000,
                min_order_paise=10000,
                free_delivery_above_paise=50000,
                same_day_available=True,
                same_day_cutoff=time(16, 0),
            )
        )
        for label, start, end in [("10 am - 1 pm", 10, 13), ("4 pm - 7 pm", 16, 19)]:
            session.add(
                DeliverySlot(
                    store_id=store.id,
                    label=label,
                    start_time=time(start),
                    end_time=time(end),
                    sort_order=start * 60,
                )
            )

    def add_product(
        slug: str,
        name: str,
        name_hi: str,
        cat: str,
        keywords: str,
        seasonal: bool = False,
        featured: bool = False,
    ) -> Product:
        product = Product(
            store_id=store.id,
            category_id=cats[cat].id,
            slug=slug,
            name=name,
            name_hi=name_hi,
            search_keywords=keywords,
            status=ProductStatus.APPROVED,
            is_seasonal=seasonal,
            is_featured=featured,
        )
        session.add(product)
        session.flush()
        return product

    for n, (slug, name, name_hi, cat, kw, base_price) in enumerate(WEIGHT_PRODUCTS):
        product = add_product(slug, name, name_hi, cat, kw, featured=n < 4)
        for grams, label in WEIGHTS:
            variant = ProductVariant(
                product_id=product.id,
                sku=f"{slug}-{grams}g".upper(),
                label=label,
                unit_type=UnitType.WEIGHT,
                weight_grams=grams,
                price_paise=base_price * grams // 100,
                sort_order=grams,
            )
            session.add(variant)
            session.flush()
            session.add(Inventory(variant_id=variant.id, quantity=20))

    for slug, name, name_hi, cat, kw, options, seasonal in PIECE_PRODUCTS:
        product = add_product(slug, name, name_hi, cat, kw, seasonal=seasonal)
        for order, (label, unit, count, price) in enumerate(options):
            variant = ProductVariant(
                product_id=product.id,
                sku=f"{slug}-{count}".upper(),
                label=label,
                unit_type=unit,
                piece_count=count,
                price_paise=price,
                sort_order=order,
            )
            session.add(variant)
            session.flush()
            session.add(Inventory(variant_id=variant.id, quantity=50))

    save_site_settings(
        session,
        {
            "announcement": "Demo announcement (replace in Admin > Homepage)",
            "announcement_enabled": True,
            "story": (
                "Demo text (replace in Admin > Store information). Tell customers who runs "
                "the shop, since when, and what you are known for.\n\n"
                "Second paragraph: how you choose and pack your spices and samagri."
            ),
            "opening_hours": ["Demo hours (replace)"],
        },
    )

    created: dict[str, str] = {}
    if accounts:
        for email, name, role in [
            ("admin@example.test", "Demo Admin", UserRole.ADMIN),
            ("customer@example.test", "Demo Customer", UserRole.CUSTOMER),
        ]:
            password = secrets.token_urlsafe(10)
            u = User(email=email, full_name=name, role=role, password_hash=hash_password(password))
            session.add(u)
            session.flush()
            if role == UserRole.CUSTOMER:
                session.add(CustomerProfile(user_id=u.id))
            created[email] = password
        seller_password = secrets.token_urlsafe(10)
        user.password_hash = hash_password(seller_password)
        created[user.email or ""] = seller_password
    session.commit()
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pincode", help="create a sample delivery zone for this 6-digit pincode")
    parser.add_argument("--accounts", action="store_true", help="create demo admin/customer logins")
    args = parser.parse_args()
    with Session(get_engine()) as db:
        logins = seed(db, args.pincode, args.accounts)
    print("Demo data loaded.")
    for email, password in logins.items():
        print(f"  {email}  password: {password}")
