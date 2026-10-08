"""Import every model so Base.metadata (and Alembic) sees all tables."""

from app.db.models.audit import AdminAction
from app.db.models.cart import Cart, CartItem, CartStatus, Wishlist, WishlistItem
from app.db.models.catalog import (
    BrandType,
    Category,
    Inventory,
    Product,
    ProductImage,
    ProductStatus,
    ProductType,
    ProductVariant,
    UnitType,
)
from app.db.models.content import SiteSetting, Testimonial
from app.db.models.order import (
    Delivery,
    DeliveryMethod,
    DeliveryStatus,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentMethod,
    PaymentStatus,
)
from app.db.models.review import Review, ReviewStatus
from app.db.models.store import DeliverySlot, SellerDeliveryZone, Store
from app.db.models.user import (
    Address,
    CustomerProfile,
    Language,
    SellerProfile,
    SellerStatus,
    User,
    UserRole,
)

__all__ = [
    "AdminAction",
    "Address",
    "BrandType",
    "Cart",
    "CartItem",
    "CartStatus",
    "Category",
    "CustomerProfile",
    "Delivery",
    "DeliverySlot",
    "DeliveryMethod",
    "DeliveryStatus",
    "Inventory",
    "Language",
    "Order",
    "OrderItem",
    "OrderStatus",
    "Payment",
    "PaymentMethod",
    "PaymentStatus",
    "Product",
    "ProductImage",
    "ProductStatus",
    "ProductType",
    "ProductVariant",
    "Review",
    "ReviewStatus",
    "SellerDeliveryZone",
    "SellerProfile",
    "SellerStatus",
    "SiteSetting",
    "Store",
    "Testimonial",
    "UnitType",
    "User",
    "UserRole",
    "Wishlist",
    "WishlistItem",
]
