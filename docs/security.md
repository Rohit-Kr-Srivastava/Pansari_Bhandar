# Security audit

Audit of authentication, authorization, passwords, sessions, CSRF, XSS, SQL injection, input
validation, uploads, rate limiting, secrets, logs, and price / inventory / order manipulation.
Every finding below has a fix and a regression test (`tests/web/test_security.py`, the
`test_f*` tests are named after the finding number).

## Findings fixed

| # | Severity | Issue | Fix | Regression test |
| --- | --- | --- | --- | --- |
| 1 | **High** | The per-account login limit keyed on the raw text typed, but the lookup strips non-digits, so `9876543210`, `x9876543210` and `98765 43210` were three buckets for one account. Online password guessing was effectively unlimited. | Limit keyed on the canonical identity (`phone:<10 digits>` / `email:<lower>`), plus the per-IP limit. | `test_f1_login_limit_cannot_be_bypassed_with_phone_variants` |
| 2 | Medium | `--forwarded-allow-ips "*"` made uvicorn trust the client-controlled left-most `X-Forwarded-For` entry, so every IP-based limit (login, register, cart, pincode) could be dodged by rotating a header, and audit-log IPs could be forged. | uvicorn proxy handling off; `client_ip()` reads the Nth entry from the **right** (`TRUSTED_PROXY_HOPS`, 1 on Railway, 0 locally). | `test_f2_*` (two tests) |
| 3 | Medium | Two simultaneous cancel requests both passed the status check and both restocked, so stock grew by 2N for an order that took N (repeatable). | Status change is a compare-and-set `UPDATE ... WHERE status = :old`; the loser gets "just updated by someone else". | `test_f3_status_change_on_stale_order_is_refused` |
| 4 | Medium | `clone_from=0000000005` skipped the ownership check (length rule) but was still honoured when copying images, so a seller could copy another seller's draft photos. | `clone_from` parsed and ownership-checked once in the route; the editor only sees the verified id. | `test_f4_clone_from_cannot_copy_another_sellers_images` |
| 5 | Low | `str.isdigit()` accepts `²`/`٣` but `int()` rejects them: unauthenticated `/shop?page=²` returned a 500 and a traceback log line (log flooding). | `isdecimal()` for every `int()` conversion; ASCII-only digit extraction for phones; stock adjust validated by regex. | `test_f5_*` (5 cases) |
| 6 | Low | Stock `+1`/`-1` read the value, then wrote `current ± n`, overwriting a checkout that happened in between. | `UPDATE inventory SET quantity = quantity + :n` with floor / ceiling guards. | `test_f6_stock_adjust_is_relative_to_current_value` |
| 7 | Low | Pillow only errors above 2x its pixel limit; an 80-megapixel PNG (a few KB) was decoded and copied three times. | Pixel count checked from the header before decoding; hard 40 MP limit. | `test_f7_huge_image_dimensions_rejected` |
| 8 | Low | A seller could change size labels or search keywords on a live product without re-approval. | Variant labels/units and `search_keywords` count as content changes. Price and stock edits stay live. | `test_f8_label_change_needs_reapproval` |
| 9 | Low | On multi-shop orders a seller saw the whole order's subtotal, discount, total and payment. | Sellers see their own items and delivery charge only. | `test_f9_seller_sees_only_own_totals_on_multi_shop_order` |
| 10 | Low | Anonymous visitors could make the server parse a 46 MB form body (the large limit was global). | 256 KB limit everywhere except the product and homepage upload routes. | `test_f10_small_body_limit_outside_upload_routes` |
| 11 | Low | The `.env.example` placeholder secret (54 chars) passed the production length check. | Placeholder values refused in production. | `test_f11_placeholder_secret_refused_in_production` |
| 12 | Low | Sessions are stateless cookies: a stolen admin cookie stayed valid for 30 days. | Seller and admin sessions expire after 12 hours (`STAFF_SESSION_HOURS`); password change still signs out every device. | `test_f12_staff_sessions_expire` |
| 14 | Low | Pages containing the CSRF token were `no-cache`, which allows a shared cache to store them. | `private, no-cache` for public pages, `no-store` for account, cart, checkout and dashboards. | `test_f14_pages_are_not_shared_cacheable` |

## Accepted risks (documented, not fixed)

- **#13 Account enumeration at registration.** "An account with this number already exists" is
  shown so a real customer is told to sign in. Registration is limited to 10 per hour per IP.
  Login errors do not reveal whether an account exists (`test_login_errors_do_not_reveal_accounts`).
- **Stateless sessions cannot be revoked individually** (only by password change, deactivating
  the account, or rotating `SECRET_KEY`). Mitigated by #12.
- **In-process rate limiter.** Correct for the single-process deployment; move it to Redis or
  PostgreSQL before running more than one process.

## Controls verified (with tests)

- **Authorization:** every `/admin` route needs an active admin; every `/seller` route needs an
  approved seller with a store; all seller queries and writes are scoped to their store
  (products, inventory, zones, slots, orders, clone, images); customers only see their own
  orders. Tests: `test_admin_pages_require_admin`, `test_seller_cannot_touch_other_sellers_data`,
  `test_other_seller_cannot_see_order`, `test_order_history_is_private`.
- **CSRF:** one app-wide dependency for every non-GET request, including multipart uploads and
  logout; HTMX sends the token in a header. Tests: `test_post_without_csrf_token_is_rejected`,
  `test_wrong_csrf_token_and_htmx_header`, `test_multipart_form_needs_csrf`.
- **Passwords:** argon2id, 8-128 characters, not digits-only; constant-time dummy verification
  for unknown accounts; sessions bound to a fingerprint of the password hash.
- **XSS:** Jinja autoescape on, no inline scripts or styles (strict CSP), JSON-LD via `tojson`,
  admin-entered links must be `https://`. Tests: `test_user_content_is_escaped`,
  `test_json_ld_cannot_break_out_of_script`.
- **SQL injection:** ORM and bound parameters only; `LIKE` wildcards escaped
  (`test_search_is_injection_safe`).
- **Uploads:** decoded and re-encoded to WebP under random names, metadata (EXIF/GPS) dropped,
  size and pixel limits, path-traversal guard. Tests: `test_bad_uploads_rejected`,
  `test_oversized_upload_rejected`.
- **Price, stock and order manipulation:** the client never sends a price, fee or total; the
  server recomputes and compares a fingerprint at "Place order"; stock is taken with a
  conditional UPDATE; one-time checkout tokens make double submits and replays harmless; status
  changes follow an allowed-transition table. Tests: `test_client_cannot_set_prices_or_fees`,
  `test_price_change_between_review_and_place`, `test_duplicate_submission_creates_one_order`,
  `test_checkout_token_cannot_reveal_another_customers_order`, `test_invalid_status_change_rejected`.
- **Config and secrets:** production refuses `DEBUG=true`, a weak or placeholder `SECRET_KEY`,
  and `ALLOWED_HOSTS=*`; `.env` and `media/` are git-ignored; `seed_dev` refuses to run in
  production; `create_admin` never prints the password; request bodies and query strings are
  never logged. Docs and OpenAPI are disabled.
- **Headers:** CSP (self only), HSTS (production), X-Frame-Options, nosniff, Referrer-Policy,
  Permissions-Policy, COOP; cookies HttpOnly, SameSite=Lax, Secure in production.
