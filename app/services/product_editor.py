"""Create, edit and clone products. Shared by the seller and admin dashboards.

The form is built for a shop owner: one row per size ("250 g", "1 kg", "Pack of 12"),
rupee prices, SKU generated when left blank, and Clone to make a new size quickly.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.formatting import parse_rupees, rupees_plain, weight_label
from app.db.models import (
    AdminAction,
    BrandType,
    Category,
    Inventory,
    OrderItem,
    Product,
    ProductImage,
    ProductStatus,
    ProductType,
    ProductVariant,
    Store,
    UnitType,
    User,
    UserRole,
)
from app.services import images as image_service
from app.services.settings import SiteSettings

MAX_IMAGES = 8
MAX_VARIANTS = 12

# Packaged-product details. Filled in by the seller from the pack; never by the system.
INFO_FIELDS: list[tuple[str, str, str]] = [
    ("net_quantity", "Net quantity", "As printed on the pack, e.g. 100 g"),
    ("mrp_note", "MRP (as printed)", "e.g. MRP Rs 55 incl. of all taxes"),
    ("ingredients", "Ingredients", ""),
    ("veg_mark", "Veg / non-veg mark", "veg or non-veg, as on the pack"),
    ("allergens", "Allergen information", ""),
    ("fssai_license", "FSSAI licence number", "14 digits, from the pack"),
    ("manufacturer", "Manufacturer / packer name and address", ""),
    ("country_of_origin", "Country of origin", ""),
    ("best_before", "Best before / use by", "e.g. 12 months from packing"),
    ("storage", "Storage instructions", ""),
    ("other", "Other information", ""),
]
INFO_KEYS = {k for k, _, _ in INFO_FIELDS}
UNIT_CHOICES = [("g", "grams"), ("kg", "kg"), ("piece", "piece(s)"), ("pack", "pack of")]
CONTENT_FIELDS = (
    "name",
    "name_hi",
    "category_id",
    "brand",
    "brand_type",
    "product_type",
    "short_description",
    "description",
    "product_info",
    "search_keywords",
)


@dataclass
class VariantRow:
    id: int | None = None
    label: str = ""
    unit: str = "g"
    amount: str = ""
    price: str = ""
    sale: str = ""
    sku: str = ""
    stock: str = ""
    stock_was: str = ""
    threshold: str = ""
    active: bool = True
    remove: bool = False


@dataclass
class ProductForm:
    name: str = ""
    name_hi: str = ""
    category_id: str = ""
    store_id: str = ""
    brand: str = ""
    brand_type: str = BrandType.NO_BRAND.value
    product_type: str = ProductType.LOOSE.value
    short_description: str = ""
    description: str = ""
    search_keywords: str = ""
    is_featured: bool = False
    is_seasonal: bool = False
    info: dict[str, str] = field(default_factory=dict)
    variants: list[VariantRow] = field(default_factory=list)
    remove_images: list[int] = field(default_factory=list)
    primary_image: str = ""
    clone_from: str = ""
    action: str = "publish"

    @classmethod
    def from_form(cls, form: Any) -> ProductForm:
        def g(key: str, limit: int = 300) -> str:
            return str(form.get(key) or "").strip()[:limit]

        def gl(key: str) -> list[str]:
            return [str(x) for x in form.getlist(key)]

        ids, labels, units = gl("v_id"), gl("v_label"), gl("v_unit")
        amounts, prices, sales = gl("v_amount"), gl("v_price"), gl("v_sale")
        skus, stocks, thresholds = gl("v_sku"), gl("v_stock"), gl("v_threshold")
        stocks_was = gl("v_stock_was")
        removes = set(gl("v_remove"))
        inactive = set(gl("v_inactive"))
        rows: list[VariantRow] = []
        for i in range(min(len(prices), MAX_VARIANTS + 5)):

            def at(lst: list[str], i: int = i) -> str:
                return lst[i].strip()[:64] if i < len(lst) else ""

            row = VariantRow(
                id=int(at(ids)) if at(ids).isdecimal() else None,
                label=at(labels)[:60],
                unit=at(units) or "g",
                amount=at(amounts),
                price=at(prices),
                sale=at(sales),
                sku=at(skus),
                stock=at(stocks),
                stock_was=at(stocks_was),
                threshold=at(thresholds),
            )
            key = str(row.id) if row.id else f"new{i}"
            row.remove = key in removes
            row.active = key not in inactive
            if row.id or any([row.label, row.amount, row.price, row.sku, row.stock]):
                rows.append(row)
        return cls(
            name=g("name", 160),
            name_hi=g("name_hi", 160),
            category_id=g("category_id", 12),
            store_id=g("store_id", 12),
            brand=g("brand", 120),
            brand_type=g("brand_type", 20) or BrandType.NO_BRAND.value,
            product_type=g("product_type", 20) or ProductType.LOOSE.value,
            short_description=g("short_description", 300),
            description=g("description", 5000),
            search_keywords=g("search_keywords", 500),
            is_featured=form.get("is_featured") in {"1", "on"},
            is_seasonal=form.get("is_seasonal") in {"1", "on"},
            info={k: g(f"info_{k}", 1000) for k in INFO_KEYS if g(f"info_{k}", 1000)},
            variants=rows,
            remove_images=[int(x) for x in gl("remove_image") if x.isdecimal()],
            primary_image=g("primary_image", 12),
            clone_from=g("clone_from", 12),
            action=g("action", 20) or "publish",
        )

    @classmethod
    def from_product(cls, p: Product, clone: bool = False) -> ProductForm:
        rows = []
        for v in p.variants:
            if v.unit_type == UnitType.WEIGHT:
                grams = v.weight_grams or 0
                unit, amount = ("kg", f"{grams / 1000:g}") if grams >= 1000 else ("g", str(grams))
            else:
                unit, amount = v.unit_type.value, str(v.piece_count or "")
            rows.append(
                VariantRow(
                    id=None if clone else v.id,
                    label=v.label,
                    unit=unit,
                    amount=amount,
                    price=rupees_plain(v.price_paise),
                    sale=rupees_plain(v.sale_price_paise),
                    sku="" if clone else v.sku,
                    stock="" if clone else str(v.inventory.quantity if v.inventory else 0),
                    stock_was="" if clone else str(v.inventory.quantity if v.inventory else 0),
                    threshold=str(v.inventory.low_stock_threshold) if v.inventory else "",
                    active=v.is_active,
                )
            )
        return cls(
            name=p.name,
            name_hi=p.name_hi or "",
            category_id=str(p.category_id),
            store_id=str(p.store_id),
            brand=p.brand or "",
            brand_type=p.brand_type.value,
            product_type=p.product_type.value,
            short_description=p.short_description or "",
            description=p.description or "",
            search_keywords=p.search_keywords or "",
            is_featured=p.is_featured,
            is_seasonal=p.is_seasonal,
            info={k: str(v) for k, v in (p.product_info or {}).items() if k in INFO_KEYS},
            variants=rows,
            clone_from=str(p.id) if clone else "",
        )


class ProductFormError(ValueError):
    def __init__(self, errors: dict[str, str]):
        super().__init__("; ".join(errors.values()))
        self.errors = errors


# ---- helpers -----------------------------------------------------------------------------


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:120]


def unique_slug(db: Session, model: Any, base: str, exclude_id: int | None = None) -> str:
    base = base or "item"
    slug, n = base, 2
    while True:
        stmt = select(model.id).where(model.slug == slug)
        if exclude_id:
            stmt = stmt.where(model.id != exclude_id)
        if db.scalar(stmt) is None:
            return slug
        slug, n = f"{base}-{n}", n + 1


def _unique_sku(db: Session, base: str, taken: set[str]) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", base.upper()).strip("-")[:56] or "SKU"
    sku, n = base, 2
    while sku in taken or db.scalar(select(ProductVariant.id).where(ProductVariant.sku == sku)):
        sku, n = f"{base}-{n}", n + 1
    return sku


def _parse_int(
    value: str, label: str, errors: dict[str, str], key: str, lo: int = 0, hi: int = 1_000_000
) -> int | None:
    if value == "":
        return None
    try:
        n = int(value)
    except ValueError:
        errors[key] = f"{label}: enter a whole number."
        return None
    if n < lo or n > hi:
        errors[key] = f"{label}: enter a number from {lo} to {hi}."
        return None
    return n


def _resolve_variant(row: VariantRow, i: int, errors: dict[str, str]) -> dict[str, Any] | None:
    key = f"v{i}"
    data: dict[str, Any] = {}
    try:
        amount = float(row.amount) if row.amount else 0.0
    except ValueError:
        errors[key] = f"Size {i + 1}: enter the weight or number of pieces."
        return None
    if not math.isfinite(amount) or amount <= 0 or amount > 100_000:
        errors[key] = f"Size {i + 1}: enter the weight or number of pieces."
        return None
    if row.unit in ("g", "kg"):
        grams = round(amount * (1000 if row.unit == "kg" else 1))
        if grams <= 0:
            errors[key] = f"Size {i + 1}: weight must be at least 1 g."
            return None
        data.update(unit_type=UnitType.WEIGHT, weight_grams=grams, piece_count=None)
        default_label = weight_label(grams)
    elif row.unit in ("piece", "pack"):
        if amount != int(amount):
            errors[key] = f"Size {i + 1}: pieces must be a whole number."
            return None
        n = int(amount)
        data.update(unit_type=UnitType(row.unit), piece_count=n, weight_grams=None)
        default_label = (
            f"Pack of {n}" if row.unit == "pack" else ("1 piece" if n == 1 else f"{n} pieces")
        )
    else:
        errors[key] = f"Size {i + 1}: choose a unit."
        return None
    data["label"] = row.label or default_label
    try:
        price = parse_rupees(row.price)
        sale = parse_rupees(row.sale)
    except ValueError as exc:
        errors[key] = f"Size {i + 1}: {exc}"
        return None
    if price is None or price <= 0:
        errors[key] = f"Size {i + 1}: enter the price."
        return None
    if sale is not None and sale >= price:
        errors[key] = f"Size {i + 1}: sale price must be lower than the price."
        return None
    data.update(price_paise=price, sale_price_paise=sale)
    stock = _parse_int(row.stock, f"Size {i + 1} stock", errors, key, 0, 100_000)
    threshold = _parse_int(row.threshold, f"Size {i + 1} low-stock alert", errors, key, 0, 10_000)
    was = int(row.stock_was) if row.id and row.stock_was.isdecimal() else None
    data.update(stock=stock, stock_was=was, threshold=threshold, is_active=row.active)
    if row.sku and not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", row.sku):
        errors[key] = f"Size {i + 1}: SKU may use letters, numbers, dot, dash and underscore."
        return None
    data["sku"] = row.sku.upper()
    return data


def validate(db: Session, form: ProductForm) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    errors: dict[str, str] = {}
    if len(form.name) < 2:
        errors["name"] = "Enter the product name."
    category = db.get(Category, int(form.category_id)) if form.category_id.isdecimal() else None
    if category is None:
        errors["category_id"] = "Choose a category."
    try:
        brand_type = BrandType(form.brand_type)
    except ValueError:
        brand_type = BrandType.NO_BRAND
    if form.brand:
        brand_type = BrandType.BRANDED if brand_type == BrandType.NO_BRAND else brand_type
    elif brand_type == BrandType.BRANDED:
        errors["brand"] = "Enter the brand name, or choose No brand."
    try:
        product_type = ProductType(form.product_type)
    except ValueError:
        product_type = ProductType.LOOSE
    rows = [r for r in form.variants if not r.remove]
    if not rows:
        errors["variants"] = "Add at least one size with a price."
    if len(rows) > MAX_VARIANTS:
        errors["variants"] = f"At most {MAX_VARIANTS} sizes per product."
    variants = []
    labels: set[str] = set()
    for i, row in enumerate(form.variants):  # i matches the row number on the form
        if row.remove:
            continue
        data = _resolve_variant(row, i, errors)
        if data is None:
            continue
        if data["label"].lower() in labels:
            errors[f"v{i}"] = f"Size {i + 1}: two sizes have the same label '{data['label']}'."
        labels.add(data["label"].lower())
        data["id"] = row.id
        variants.append(data)
    if errors:
        raise ProductFormError(errors)
    fields = dict(
        name=form.name,
        name_hi=form.name_hi or None,
        category_id=category.id if category else None,
        brand=form.brand or None,
        brand_type=brand_type,
        product_type=product_type,
        short_description=form.short_description or None,
        description=form.description or None,
        search_keywords=form.search_keywords or None,
        product_info=form.info or None,
        is_featured=form.is_featured,
        is_seasonal=form.is_seasonal,
    )
    return fields, variants


def _status_for(
    action: str,
    actor: User,
    settings: SiteSettings,
    current: ProductStatus | None,
    content_changed: bool,
) -> ProductStatus:
    trusted = actor.role == UserRole.ADMIN or not settings.require_product_approval
    if action == "draft":
        return ProductStatus.DRAFT
    if action == "deactivate":
        return ProductStatus.INACTIVE
    if trusted:
        return ProductStatus.APPROVED
    if current == ProductStatus.APPROVED and not content_changed:
        return ProductStatus.APPROVED  # price and stock edits stay live
    return ProductStatus.PENDING


def load_for_edit(db: Session, product_id: int) -> Product | None:
    return db.scalar(
        select(Product)
        .where(Product.id == product_id)
        .options(
            selectinload(Product.variants).selectinload(ProductVariant.inventory),
            selectinload(Product.images),
        )
    )


def save(
    db: Session,
    form: ProductForm,
    *,
    actor: User,
    store: Store,
    settings: SiteSettings,
    media_root: Path,
    uploads: list[bytes],
    max_upload_bytes: int,
    product: Product | None = None,
    ip: str | None = None,
) -> Product:
    """Validate and save. Raises ProductFormError with field messages."""
    fields, variants = validate(db, form)
    errors: dict[str, str] = {}
    if actor.role != UserRole.ADMIN:
        fields.pop("is_featured")  # featuring on the homepage is the admin's decision
    existing_images = len(product.images) if product else 0
    removing = len(set(form.remove_images))
    if existing_images - removing + len(uploads) > MAX_IMAGES:
        errors["images"] = f"At most {MAX_IMAGES} images per product."
    stored: list[str] = []
    for data in uploads:
        try:
            stored.append(image_service.save_upload(data, media_root, max_upload_bytes))
        except image_service.ImageError as exc:
            errors["images"] = str(exc)
    if errors:
        for rel in stored:
            image_service.delete_files(rel, media_root)
        raise ProductFormError(errors)

    try:
        return _save_product(
            db, form, actor, store, settings, media_root, product, fields, variants, stored, ip
        )
    except BaseException:
        # nothing was committed: do not leave the uploaded (or copied) photos behind
        for rel in stored:
            image_service.delete_files(rel, media_root)
        raise


def _save_product(
    db: Session,
    form: ProductForm,
    actor: User,
    store: Store,
    settings: SiteSettings,
    media_root: Path,
    product: Product | None,
    fields: dict[str, Any],
    variants: list[dict[str, Any]],
    stored: list[str],
    ip: str | None,
) -> Product:
    creating = product is None
    before: dict[str, Any] = {}
    if product is None:
        product = Product(store_id=store.id, slug="", **fields)
        product.slug = unique_slug(db, Product, slugify(form.name))
        db.add(product)
        db.flush()
        content_changed = True
    else:
        before = {k: _plain(getattr(product, k)) for k in CONTENT_FIELDS}
        for k, v in fields.items():
            setattr(product, k, v)
        content_changed = any(before[k] != _plain(getattr(product, k)) for k in CONTENT_FIELDS)
        # Sizes customers see (label and unit) are content too; price and stock are not.
        old_shape = sorted(
            (v.label, v.unit_type.value, v.weight_grams, v.piece_count)
            for v in product.variants
            if v.is_active
        )
        new_shape = sorted(
            (d["label"], d["unit_type"].value, d["weight_grams"], d["piece_count"])
            for d in variants
            if d["is_active"]
        )
        content_changed = content_changed or old_shape != new_shape
        if form.name and slugify(form.name) and product.status == ProductStatus.DRAFT:
            product.slug = unique_slug(db, Product, slugify(form.name), exclude_id=product.id)

    old_status = product.status if not creating else None
    content_changed = content_changed or bool(stored) or bool(form.remove_images)
    product.status = _status_for(form.action, actor, settings, old_status, content_changed)
    if product.status == ProductStatus.APPROVED and actor.role == UserRole.ADMIN:
        product.reviewed_by_id = actor.id
    if product.status != ProductStatus.REJECTED:
        product.rejection_reason = None

    _save_variants(db, product, variants, settings)
    _save_images(db, product, form, stored, media_root)
    db.add(
        AdminAction(
            actor_id=actor.id,
            action="product.create" if creating else "product.update",
            entity_type="product",
            entity_id=product.id,
            before=before or None,
            after={"status": product.status.value, "name": product.name},
            ip_address=ip,
        )
    )
    db.flush()
    return product


def _plain(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def _save_variants(
    db: Session, product: Product, variants: list[dict[str, Any]], settings: SiteSettings
) -> None:
    existing = {v.id: v for v in product.variants}
    keep_ids = {d["id"] for d in variants if d["id"] in existing}
    taken: set[str] = set()
    # Remove sizes the seller deleted. Sizes already ordered are deactivated instead, so order
    # history keeps its link; their label is retired so the seller can add that size again.
    for vid, old in existing.items():
        if vid not in keep_ids:
            ordered = db.scalar(select(func.count()).where(OrderItem.variant_id == vid))
            if ordered:
                old.is_active = False
                if "(old " not in old.label:
                    old.label = f"{old.label[:40]} (old {old.id})"
            else:
                product.variants.remove(old)
                db.delete(old)
    db.flush()
    for order, d in enumerate(variants):
        found = existing.get(d["id"]) if d["id"] else None
        if found is not None:
            variant = found
        else:
            variant = ProductVariant(
                product_id=product.id,
                sku="",
                label=d["label"],
                unit_type=d["unit_type"],
                price_paise=d["price_paise"],
            )
            db.add(variant)
            product.variants.append(variant)
        sku = d["sku"] or variant.sku
        clash = bool(sku) and (
            sku in taken
            or db.scalar(
                select(ProductVariant.id).where(
                    ProductVariant.sku == sku, ProductVariant.id != (variant.id or 0)
                )
            )
            is not None
        )
        if d["sku"] and clash:
            raise ProductFormError({f"v{order}": f"SKU {d['sku']} is already used."})
        if not sku or clash:
            sku = _unique_sku(db, f"{product.slug}-{d['label']}", taken)
        taken.add(sku)
        variant.sku = sku
        variant.label = d["label"]
        variant.unit_type = d["unit_type"]
        variant.weight_grams = d["weight_grams"]
        variant.piece_count = d["piece_count"]
        variant.price_paise = d["price_paise"]
        variant.sale_price_paise = d["sale_price_paise"]
        variant.is_active = d["is_active"]
        variant.sort_order = order
        db.flush()
        inv = variant.inventory or db.get(Inventory, variant.id)
        if inv is None:
            inv = Inventory(
                variant_id=variant.id,
                quantity=0,
                reserved=0,
                low_stock_threshold=settings.default_low_stock_threshold,
            )
            db.add(inv)
            variant.inventory = inv
            db.flush()
        _apply_stock(db, inv, d["stock"], d["stock_was"])
        if d["threshold"] is not None:
            inv.low_stock_threshold = d["threshold"]
    db.flush()


def _apply_stock(db: Session, inv: Inventory, entered: int | None, was: int | None) -> None:
    """Store the stock typed on the form without undoing sales made while it was open.

    The form remembers the stock it showed (`was`). Only the owner's change (entered - was) is
    applied, relative to the current value, so a customer's order placed in between survives.
    """
    if entered is None:
        return
    if was is None:  # a new size: the typed number is the opening stock
        inv.quantity = max(entered, inv.reserved)
        return
    delta = entered - was
    if delta == 0:
        return
    target = Inventory.quantity + delta
    db.execute(
        update(Inventory)
        .where(Inventory.variant_id == inv.variant_id)
        .values(quantity=case((target < Inventory.reserved, Inventory.reserved), else_=target))
        .execution_options(synchronize_session=False)
    )
    db.refresh(inv)


def _delete_after_commit(db: Session, rel: str) -> None:
    db.info.setdefault("delete_after_commit", []).append(rel)


def finish_uploads(db: Session, media_root: Path) -> None:
    """Call after db.commit(): remove files of deleted images (never before the commit)."""
    for rel in db.info.pop("delete_after_commit", []):
        image_service.delete_files(rel, media_root)


def _save_images(
    db: Session, product: Product, form: ProductForm, stored: list[str], media_root: Path
) -> None:
    for img in list(product.images):
        if img.id in form.remove_images:
            product.images.remove(img)
            db.delete(img)
            _delete_after_commit(db, img.path)
    if form.clone_from.isdecimal() and not product.images:
        source = load_for_edit(db, int(form.clone_from))
        if source is not None:
            for src in source.images:
                copy = image_service.copy_image(src.path, media_root)
                if copy:
                    stored.insert(0, copy)
    start = max((i.sort_order for i in product.images), default=-1) + 1
    for n, rel in enumerate(stored):
        img = ProductImage(
            product_id=product.id, path=rel, alt_text=product.name[:200], sort_order=start + n
        )
        product.images.append(img)
    db.flush()
    images = list(product.images)
    primary_id = int(form.primary_image) if form.primary_image.isdecimal() else None
    chosen = next((i for i in images if i.id == primary_id), None)
    chosen = chosen or next((i for i in images if i.is_primary), None)
    chosen = chosen or (images[0] if images else None)
    for i in images:
        if i is not chosen and i.is_primary:
            i.is_primary = False
    db.flush()
    if chosen is not None and not chosen.is_primary:
        chosen.is_primary = True
        db.flush()


def set_stock(
    db: Session,
    variant: ProductVariant,
    quantity: int,
    threshold: int | None,
    actor: User,
    ip: str | None = None,
) -> Inventory:
    if quantity < 0 or quantity > 100_000:
        raise ValueError("Stock must be between 0 and 1,00,000.")
    inv = variant.inventory
    if inv is None:
        inv = Inventory(variant_id=variant.id, quantity=0, reserved=0)
        db.add(inv)
        variant.inventory = inv
    before = {"quantity": inv.quantity, "threshold": inv.low_stock_threshold}
    inv.quantity = max(quantity, inv.reserved)
    if threshold is not None:
        if threshold < 0 or threshold > 10_000:
            raise ValueError("Low-stock alert must be between 0 and 10,000.")
        inv.low_stock_threshold = threshold
    db.add(
        AdminAction(
            actor_id=actor.id,
            action="inventory.set",
            entity_type="variant",
            entity_id=variant.id,
            before=before,
            after={"quantity": inv.quantity, "threshold": inv.low_stock_threshold},
            ip_address=ip,
        )
    )
    db.flush()
    return inv


def adjust_stock(
    db: Session, variant: ProductVariant, delta: int, actor: User, ip: str | None = None
) -> None:
    """Add or remove stock relative to the current value, atomically (no lost updates)."""
    if variant.inventory is None:
        if delta < 0:
            raise ValueError("There is no stock to remove.")
        db.add(Inventory(variant_id=variant.id, quantity=0, reserved=0))
        db.flush()
    result = db.execute(
        update(Inventory)
        .where(
            Inventory.variant_id == variant.id,
            Inventory.quantity + delta >= Inventory.reserved,
            Inventory.quantity + delta >= 0,
            Inventory.quantity + delta <= 100_000,
        )
        .values(quantity=Inventory.quantity + delta)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:  # type: ignore[attr-defined]
        raise ValueError("Stock cannot go below zero.")
    db.add(
        AdminAction(
            actor_id=actor.id,
            action="inventory.set",
            entity_type="variant",
            entity_id=variant.id,
            after={"adjust": delta},
            ip_address=ip,
        )
    )
    db.flush()
