# Database design (MVP)

PostgreSQL, SQLAlchemy 2.x models in `app/db/models/`, Alembic migrations `0001_initial_schema` and
`0002_commerce`. 24 tables. Money is stored as integer paise. Every table has `created_at` / `updated_at`
(except `inventory`, which has both too, and `admin_actions`, which is append-only with `created_at`).

## 1. Relationships

```text
users 1-1 customer_profiles          users 1-1 seller_profiles 1-1 stores
users 1-* addresses                  stores 1-* products 1-* product_variants 1-1 inventory
users 1-1 wishlists 1-* wishlist_items *-1 products
users 1-* carts 1-* cart_items *-1 product_variants   (guest carts use session_token)
categories 1-* categories (parent)   categories 1-* products     products 1-* product_images
stores 1-* seller_delivery_zones     stores 1-* delivery_slots
users 1-* orders 1-* order_items     orders 1-* payments
orders 1-* delivery  (one row per store in the order)   delivery *-1 stores
products 1-* reviews *-1 users       users 1-* admin_actions (actor)
```

Decisions that differ slightly from the raw table list:

- **One store per seller** (`stores.seller_profile_id` is unique). Relax later if needed.
- **A product has one category**, the most specific one. The parent (category) is reached through
  `category.parent`. Subcategory filtering uses the same column.
- **`delivery`** holds one row per store per order (method, status, fee, slot). That is what lets a
  seller manage only their part of a multi-seller order.
- **`seller_delivery_zones`** are per store: pincode, fee, minimum order, free-delivery threshold,
  same-day option and cut-off. `stores.delivery_radius_km`, latitude and longitude are optional.
- **`admin_actions`** is the audit trail. Its `actor_id` can also record seller approval actions.
- **Packaged-food details** (FSSAI licence number, ingredients, net quantity and so on) live in
  `products.product_info` (JSON). They are filled in by the seller, never by the system.
- **Brand is optional.** `brand` can be empty; `brand_type` is `branded`, `no_brand`, `generic` or `local`.

## 2. Schema summary

| Table | Main columns |
| --- | --- |
| users | email, phone, password_hash, full_name, role (customer, seller, admin), is_active, last_login_at |
| customer_profiles | user_id, preferred_language (en, hi), marketing_opt_in |
| seller_profiles | user_id, business_name, contact_name, contact_phone, status (pending, approved, rejected, suspended), reviewed_by_id, reviewed_at, rejection_reason |
| stores | seller_profile_id, slug, name, name_hi, description, phone, whatsapp, address, pincode, lat/long, delivery_radius_km, pickup_available, pickup_instructions, is_active |
| addresses | user_id, recipient_name, phone, line1, line2, landmark, city, state, pincode, is_default |
| categories | parent_id, slug, name, name_hi, image_path, sort_order, is_active |
| products | store_id, category_id, slug, name, name_hi, short_description, description, brand, brand_type, product_type, product_info, search_keywords, status (draft, pending, approved, rejected, inactive), rejection_reason, reviewed_by_id, is_featured, is_seasonal |
| product_images | product_id, path, alt_text, sort_order, is_primary |
| product_variants | product_id, sku, label, unit_type (weight, piece, pack), weight_grams, piece_count, price_paise, sale_price_paise, is_active |
| inventory | variant_id (primary key), quantity, reserved, low_stock_threshold |
| carts / cart_items | user_id or session_token, status; cart_id, variant_id, quantity |
| wishlists / wishlist_items | user_id; wishlist_id, product_id |
| orders | order_no, user_id, status (pending, confirmed, preparing, ready, out_for_delivery, delivered, cancelled, returned), checkout_token (unique, makes checkout idempotent), delivery_address (JSON snapshot), subtotal, delivery_fee, discount, total, customer_note, placed_at |
| order_items | order_id, store_id, product_id, variant_id, product_name, variant_label, sku, unit_price_paise, quantity, line_total_paise |
| payments | order_id, method (cod), status, amount_paise, provider, provider_ref, paid_at |
| delivery | order_id, store_id, method (delivery, pickup), status (same values as orders), fee_paise, scheduled_date, slot_label, is_same_day |
| seller_delivery_zones | store_id, pincode, area_name, delivery_fee_paise, min_order_paise, free_delivery_above_paise, same_day_available, same_day_cutoff, estimated_days |
| delivery_slots | store_id, label, start_time, end_time, sort_order, is_active |
| site_settings | key, value (JSON: shop name, phone, WhatsApp number, hours, story, homepage rows, offer, SEO text) |
| testimonials | name, place, body, rating, sort_order, is_active |
| reviews | product_id, user_id, order_item_id, rating, title, body, status (pending, approved, hidden) |
| admin_actions | actor_id, action, entity_type, entity_id, reason, before, after, ip_address |

## 3. Constraints worth knowing

- Users need an email or a phone; email is unique ignoring case; phone is unique.
- A branded product must have a brand name; a rejected product must have a reason.
- A weight variant needs `weight_grams` (50, 100, 250, 500, 1000 and so on) and no `piece_count`;
  a piece or pack variant needs `piece_count` and no weight. Sale price must be below price.
- Variant label unique within a product; SKU unique everywhere.
- Stock cannot go negative and `reserved` cannot exceed `quantity`.
- At most one default address per user, one primary image per product, one active cart per user.
- A cart holds a variant once, quantity 1 to 99.
- Order total must equal subtotal + delivery fee - discount; each line total must equal price x quantity.
- One `delivery` row per (order, store); a pickup delivery can never be `out_for_delivery`.
- A delivery slot must end after it starts; a zone's delivery days are 0 to 30.
- One review per user per product, rating 1 to 5. One delivery zone per (store, pincode).
- Foreign keys protect history: stores, categories and users with orders cannot be deleted
  (RESTRICT); deleting a product keeps order lines (name and price copied, product link set to NULL).
- Enumerated values (roles, statuses) are enforced by CHECK constraints, not native PostgreSQL enums.

## 4. Indexes

Unique: users.phone, lower(users.email), categories.slug, stores.slug, products.slug,
product_variants.sku, orders.order_no, carts.session_token.
Lookup: products(status, category_id), products(store_id, status), products(name),
product_variants(product_id, is_active), categories(parent_id, sort_order),
orders(user_id, placed_at), orders(status, placed_at), order_items(order_id), order_items(store_id),
delivery(store_id, status), seller_delivery_zones(pincode, is_active), reviews(product_id, status),
admin_actions(entity_type, entity_id), admin_actions(actor_id, created_at), inventory(quantity),
addresses(user_id), seller_profiles(status).
Partial unique: one default address per user, one primary image per product, one active cart per user.
Later (Phase 4): a trigram index on product names and `search_keywords` for faster search.

## 5. Example data

`python -m scripts.seed_dev --pincode 829111 --accounts` loads clearly marked demo data: ten
categories with Hindi names, a "Demo Store (replace)", 12 weighed products (50 g to 1 kg) and
12 piece or pack products, a delivery zone, two slots, and demo logins with random passwords
(printed once). Prices are sample values. Nothing about the real shop is invented. It refuses to
run when `ENVIRONMENT=production`.

## 6. Commands

```bash
docker compose up -d db                # local PostgreSQL
cp .env.example .env
alembic upgrade head                   # create the tables
alembic check                          # models and migration agree
python -m scripts.seed_dev             # optional demo data
pytest                                 # SQLite
TEST_DATABASE_URL=postgresql+psycopg://pansari:pansari@localhost:5432/pansari_test pytest
ruff check . && mypy app scripts tests
```
