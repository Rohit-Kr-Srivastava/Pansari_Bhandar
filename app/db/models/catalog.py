"""Catalogue: categories, products, images, variants (weight or piece) and inventory."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    BigIntFK,
    IdMixin,
    JSONType,
    TimestampMixin,
    enum_check,
    enum_column,
)

if TYPE_CHECKING:
    from app.db.models.store import Store


class ProductStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INACTIVE = "inactive"


class ProductType(StrEnum):
    LOOSE = "loose"  # sold loose by weight or piece, no consumer packaging
    PACKAGED_FOOD = "packaged_food"
    PACKAGED_OTHER = "packaged_other"


class BrandType(StrEnum):
    BRANDED = "branded"
    NO_BRAND = "no_brand"
    GENERIC = "generic"
    LOCAL = "local"


class UnitType(StrEnum):
    WEIGHT = "weight"  # priced per pack of N grams (50 g, 100 g, 250 g, 500 g, 1 kg)
    PIECE = "piece"  # sold by count
    PACK = "pack"  # a pack holding N pieces, for example a pack of 50 batti


class Category(IdMixin, TimestampMixin, Base):
    """Two levels at most: a top category and its subcategories (enforced in the service layer)."""

    __tablename__ = "categories"

    parent_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("categories.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    name_hi: Mapped[str | None] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    image_path: Mapped[str | None] = mapped_column(String(300))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    parent: Mapped[Category | None] = relationship(
        back_populates="children", remote_side="Category.id"
    )
    children: Mapped[list[Category]] = relationship(back_populates="parent")
    products: Mapped[list[Product]] = relationship(back_populates="category")

    __table_args__ = (
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
        Index("ix_categories_parent_id_sort_order", "parent_id", "sort_order"),
    )


class Product(IdMixin, TimestampMixin, Base):
    __tablename__ = "products"

    store_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("stores.id", ondelete="RESTRICT"))
    # The most specific category. The parent category is reached through category.parent.
    category_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("categories.id", ondelete="RESTRICT")
    )
    slug: Mapped[str] = mapped_column(String(160), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    name_hi: Mapped[str | None] = mapped_column(String(160))
    short_description: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    # Brand is optional. brand_type says what to show when there is no brand name.
    brand: Mapped[str | None] = mapped_column(String(120))
    brand_type: Mapped[BrandType] = mapped_column(
        enum_column(BrandType, "brand_type"),
        default=BrandType.NO_BRAND,
        server_default="no_brand",
    )
    product_type: Mapped[ProductType] = mapped_column(
        enum_column(ProductType, "product_type"),
        default=ProductType.LOOSE,
        server_default="loose",
    )
    # Seller-entered details where applicable (for example packaged-food label information).
    # Nothing is filled in by the system.
    product_info: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # Extra search words, such as Hindi and Hinglish names (haldi, turmeric, हल्दी).
    search_keywords: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ProductStatus] = mapped_column(
        enum_column(ProductStatus, "product_status"),
        default=ProductStatus.DRAFT,
        server_default="draft",
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reviewed_by_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    is_seasonal: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    store: Mapped[Store] = relationship(back_populates="products")
    category: Mapped[Category] = relationship(back_populates="products")
    images: Mapped[list[ProductImage]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductImage.sort_order"
    )
    variants: Mapped[list[ProductVariant]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductVariant.sort_order"
    )

    __table_args__ = (
        enum_check("status", ProductStatus),
        enum_check("product_type", ProductType),
        enum_check("brand_type", BrandType),
        CheckConstraint(
            "brand_type <> 'branded' OR brand IS NOT NULL", name="branded_needs_brand_name"
        ),
        CheckConstraint(
            "status <> 'rejected' OR rejection_reason IS NOT NULL", name="rejected_needs_reason"
        ),
        Index("ix_products_status_category_id", "status", "category_id"),
        Index("ix_products_store_id_status", "store_id", "status"),
        Index("ix_products_name", "name"),
        Index("ix_products_is_featured_status", "is_featured", "status"),
    )


class ProductImage(IdMixin, TimestampMixin, Base):
    __tablename__ = "product_images"

    product_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("products.id", ondelete="CASCADE"))
    path: Mapped[str] = mapped_column(String(300))
    alt_text: Mapped[str | None] = mapped_column(String(200))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

    product: Mapped[Product] = relationship(back_populates="images")

    __table_args__ = (
        Index("ix_product_images_product_id_sort_order", "product_id", "sort_order"),
        Index(
            "uq_product_images_one_primary",
            "product_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary"),
        ),
    )


class ProductVariant(IdMixin, TimestampMixin, Base):
    """One purchasable option with its own price and SKU: 250 g, 1 kg, pack of 50, 1 piece."""

    __tablename__ = "product_variants"

    product_id: Mapped[int] = mapped_column(BigIntFK, ForeignKey("products.id", ondelete="CASCADE"))
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(60))
    unit_type: Mapped[UnitType] = mapped_column(enum_column(UnitType, "unit_type"))
    weight_grams: Mapped[int | None] = mapped_column(Integer)
    piece_count: Mapped[int | None] = mapped_column(Integer)
    # Money is stored as integer paise (100 paise = 1 rupee).
    price_paise: Mapped[int] = mapped_column(Integer)
    sale_price_paise: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    product: Mapped[Product] = relationship(back_populates="variants")
    inventory: Mapped[Inventory | None] = relationship(
        back_populates="variant", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        enum_check("unit_type", UnitType),
        UniqueConstraint("product_id", "label", name="product_label"),
        CheckConstraint("price_paise >= 0", name="price_non_negative"),
        CheckConstraint(
            "sale_price_paise IS NULL"
            " OR (sale_price_paise >= 0 AND sale_price_paise < price_paise)",
            name="sale_below_price",
        ),
        CheckConstraint(
            "(unit_type = 'weight' AND COALESCE(weight_grams, 0) > 0 AND piece_count IS NULL)"
            " OR (unit_type <> 'weight' AND COALESCE(piece_count, 0) > 0"
            " AND weight_grams IS NULL)",
            name="unit_quantity_matches_type",
        ),
        Index("ix_product_variants_product_id_is_active", "product_id", "is_active"),
    )


class Inventory(TimestampMixin, Base):
    """Stock for one variant, kept apart from price and product details."""

    __tablename__ = "inventory"

    variant_id: Mapped[int] = mapped_column(
        BigIntFK, ForeignKey("product_variants.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reserved: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, server_default="5")

    variant: Mapped[ProductVariant] = relationship(back_populates="inventory")

    __table_args__ = (
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("reserved >= 0 AND reserved <= quantity", name="reserved_within_quantity"),
        CheckConstraint("low_stock_threshold >= 0", name="threshold_non_negative"),
        Index("ix_inventory_quantity", "quantity"),
    )
