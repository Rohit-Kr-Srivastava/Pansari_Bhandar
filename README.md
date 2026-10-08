# Pansari Bhandar

Online shop for Pansari Bhandar, Gomia, Jharkhand: spices and masala, jadi-buti, puja samagri,
shringar items and daily needs, with local delivery, store pickup and cash on delivery.

FastAPI, Jinja2 and HTMX on PostgreSQL. No React, no Next.js, no front-end build step.

## What is in it

- **Customers:** homepage, categories, search (English and Hindi names), product pages with
  sizes and sale prices, cart, checkout with pincode-based delivery and time slots, order
  tracking, order history, WhatsApp enquiry buttons.
- **Sellers:** register, wait for approval, then add products fast (clone an existing product to
  make a new size), manage stock, orders, store profile and delivery pincodes, fees and slots.
- **Admin:** dashboard, sellers, products, categories, customers, orders, inventory, delivery,
  homepage, testimonials, store information, settings, activity log. All behind admin login.
- **SEO:** clean URLs, titles, descriptions, canonical, Open Graph, sitemap, robots.txt,
  breadcrumbs, `GroceryStore` and `Product` structured data.

The shop's name, phone, WhatsApp number, address, hours, story, delivery rules and offers are
all edited in the admin area. Nothing about the real shop is written into the code.

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate                       # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt pytest httpx ruff
copy .env.example .env                       # macOS/Linux: cp ; then set SECRET_KEY
docker compose up -d db
alembic upgrade head
python -m scripts.seed_dev --pincode 829111 --accounts    # demo data and demo logins
uvicorn app.main:app_factory --factory --reload
```

Open <http://localhost:8000>. Run the tests with `pytest` (SQLite) or set `TEST_DATABASE_URL`
to run them on PostgreSQL.

## Documentation

| File | What it covers |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | design, folder structure, routes, tests, limitations, phase 2 |
| [docs/database.md](docs/database.md) | tables, constraints, indexes |
| [docs/deployment.md](docs/deployment.md) | Docker, Railway, migrations, backups, rollback, exact commands |
| [docs/security.md](docs/security.md) | security audit: each finding, fix and regression test |
| [docs/payments.md](docs/payments.md) | how to add Razorpay or another gateway later |

The planning document and the design system style guide are kept in Claude.
