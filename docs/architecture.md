# Architecture

Server-rendered web app: FastAPI + Jinja2 + HTMX + SQLAlchemy 2 + PostgreSQL. No React, no
Next.js, no front-end build step. One stylesheet (about 35 KB), two small scripts (HTMX, vendored,
and `app.js`, about 5 KB).

```text
browser ──HTML / HTMX fragments──▶ web/ routes ──▶ services/ (business rules) ──▶ db/ models ──▶ PostgreSQL
                                        │
                                        └─▶ templates/ (pages, partials, components)
```

Rules that keep it simple:

- **Routes are thin.** They parse the request, call a service, render a template. Business rules
  (pricing, stock, delivery, status changes, approvals) live in `app/services/` and are unit
  tested without HTTP.
- **Nothing about the shop is hard-coded.** Name, phone, WhatsApp number, hours, story, homepage
  rows, offers: `site_settings` (admin). Delivery pincodes, fees, slots, pickup: database tables
  (seller/admin). Payment methods: the provider registry.
- **Money is integer paise.** Display uses `rupees()` (Indian digit grouping).
- **The server prices everything.** The browser sends variant ids and quantities only.
- **HTMX only where it helps**: size switching, add-to-cart, cart quantity, pincode check,
  checkout delivery options, listing filters/sort/pagination, inventory rows. Every page also
  works as a normal full-page request without JavaScript.

## Folder structure

```text
app/
  main.py              app factory, middleware order, error pages
  core/                config (env), security (argon2, CSRF helpers), rate limiter, money formatting
  db/                  declarative base, session, models/ (24 tables)
  services/            catalog, cart, checkout, orders, payments, delivery, whatsapp,
                       product_editor, images, accounts, dashboard, settings, seo
  web/                 deps (session, auth, CSRF), middleware, templating, and routers:
                       public, shop, cart, checkout, account, seller, admin, manage (shared)
  templates/           base.html, components/ui.html (macros), partials/ (HTMX fragments),
                       pages/ (storefront), manage/ (shared dashboard screens), seller/, admin/
  static/              css/app.css, js/app.js, vendor/htmx.min.js, img/
alembic/versions/      0001 initial schema, 0002 commerce (statuses, slots, settings, testimonials)
scripts/               seed_dev (demo data), create_admin, start.sh (container entry point)
tests/                 db/ (schema, constraints, migration), web/ (HTTP, units, security, perf)
docs/                  architecture, database, deployment, payments, security
Dockerfile  railway.json  docker-compose.yml  requirements.txt  pyproject.toml
```

## Database (24 tables, PostgreSQL)

Users and profiles: `users`, `customer_profiles`, `seller_profiles`, `addresses`.
Catalogue: `stores`, `categories` (2 levels), `products`, `product_images`, `product_variants`
(per-size price, sale price, SKU), `inventory` (stock per variant).
Selling: `carts`, `cart_items`, `wishlists`, `wishlist_items`, `orders`, `order_items`
(name and price copied, so history survives edits), `payments`, `delivery` (one row per store
per order), `reviews`.
Delivery: `seller_delivery_zones` (pincode, fee, minimum, free-above, same-day, cut-off, days),
`delivery_slots`.
Content and audit: `site_settings` (one JSON document), `testimonials`, `admin_actions`
(append-only audit trail of approvals, status changes, stock and settings edits).

Integrity is enforced in the database as well as the code: CHECK constraints for statuses, price
rules (`sale < price`), stock (`quantity >= 0`, `reserved <= quantity`), order totals
(`total = subtotal + fee - discount`, `line = price x quantity`); unique indexes for SKU, slug,
order number, checkout token, one active cart per user, one default address, one primary image.
Details: `docs/database.md`.

## Request flow for an order

`GET /checkout` → `POST /checkout/quote` (HTMX, per pincode change) → `POST /checkout/review`
(validate, price, show) → `POST /checkout/place` (token, fingerprint, stock UPDATE, order,
delivery, payment, commit) → `/orders/{no}/confirmed`. Statuses:
Pending → Confirmed → Preparing → Ready → Out for delivery → Delivered, or Cancelled / Returned
(restocks, audit-logged, compare-and-set).

## API summary (101 routes, all HTML; there is no public JSON API)

| Area | Routes | Examples |
| --- | --- | --- |
| Public (16) | pages, SEO, health | `GET /`, `/shop`, `/c/{slug}`, `/search`, `/p/{slug}`, `/p/{slug}/buybox`, `/about`, `/contact`, `/delivery`, `/delivery/check`, `/robots.txt`, `/sitemap.xml`, `/healthz` |
| Customer (18) | account, cart, checkout | `/login`, `/register`, `/cart`, `POST /cart/add`, `POST /cart/items/{id}`, `/checkout`, `POST /checkout/{quote,review,place}`, `/orders/{no}/confirmed`, `/account`, `/account/orders/{no}`, `POST .../cancel` |
| Seller (24) | `/seller/...` | dashboard, products (list, new, edit, clone, hide/show), inventory, orders (+status), store profile, delivery (pincodes, slots, options) |
| Admin (43) | `/admin/...` | everything above for all stores, plus sellers (approve/reject/suspend), product review/feature, categories, customers, homepage, testimonials, store information, settings, activity log |

## Test summary

230+ automated tests; all pass on SQLite and on PostgreSQL 16 (including the Alembic upgrade /
downgrade round trip and "models match migrations").

| Suite | What it proves |
| --- | --- |
| `tests/db` | every table constraint, cascade and restriction; demo seed; migration drift |
| `test_public.py` | homepage sections, listing, filters, sort, pagination, search (English, Hindi, keywords), product page, JSON-LD, sitemap/robots, pincode check |
| `test_cart_checkout.py` | cart limits, stock, hidden products, merge on login, full checkout, free delivery, discount cap, pickup, validation, duplicate submit, price change, stock race, expired session, token replay, cancel and restock, private order history |
| `test_dashboards.py` | role access, seller approval, product form validation, uploads, clone, ownership isolation, inventory, order statuses, delivery settings, admin content |
| `test_security.py` | CSRF, rate limits, sessions, redirects, XSS, injection, headers, production config, and one regression test per audit finding |
| `test_performance.py` | queries per page do not grow with the catalogue; budgets; indexes; asset sizes |
| `test_units.py` | delivery rules with a fixed clock, WhatsApp messages, money formatting |

## Known limitations

- Cash on delivery only (online payment is designed in, see `docs/payments.md`).
- One web process (rate limiter and 60 s settings cache are in memory).
- No email or SMS: customers are contacted by phone or WhatsApp (click-to-chat links, not the
  WhatsApp Business API, so no automatic messages).
- No password reset by email or SMS: the shop resets passwords on request (WhatsApp/phone).
- Reviews tables exist but there is no review UI yet; wishlist tables exist but no UI.
- Product photos live on one volume (no CDN); lost if the volume is lost, so back it up.
- Delivery eligibility is by pincode list; the radius is informational (no maps API).
- Hindi names are shown beside English ones; the interface itself is English only.
- Multi-shop orders are supported by the schema and checkout, but order status for orders that
  span shops is managed by the admin.

## Recommended phase 2

1. Online payments (UPI / cards via Razorpay or Cashfree) using the provider interface.
2. Automatic order notifications (WhatsApp Business API or SMS) and low-stock alerts to the owner.
3. Password reset by SMS OTP; phone-number login with OTP.
4. Customer reviews and wishlist screens; reorder from past orders.
5. Image storage on S3-compatible storage with a CDN; automatic product-photo resizing queue.
6. Coupons and festival campaigns; delivery-boy assignment and a simple delivery run sheet.
7. Hindi interface (the schema already stores Hindi names and `preferred_language`).
8. Redis for rate limiting and caching if the traffic needs more than one process.
