# Payments: Cash on Delivery now, an online gateway later

Checkout talks only to the `PaymentProvider` interface in `app/services/payments.py`. Today
`PROVIDERS` contains `CashOnDelivery`. Adding Razorpay, Cashfree, PayU or any other gateway means
adding a provider, not rewriting checkout.

## What checkout does with a provider

1. The customer picks from `available_providers(total)` (the list is rendered from the registry).
2. `place_order()` creates the order, takes stock, creates a `Payment` row (`method`, `status`
   `pending`, `amount_paise`, `provider`, `provider_ref`) and calls `provider.start(...)`.
3. `start()` returns `PaymentStart(redirect_url=...)`. COD returns nothing (go to the
   confirmation page). A gateway returns its hosted-checkout URL.
4. When the order is delivered (`orders.change_status`) every still-pending payment is passed to
   `provider.on_delivered()`. COD marks it paid at that point. Cancelling fails pending payments
   and restocks; returning marks paid payments as refunded.

## Adding a gateway (about a day of work)

1. Add the value to `PaymentMethod` (`app/db/models/order.py`), for example `RAZORPAY = "razorpay"`,
   and write an Alembic migration that updates the `payments.method` CHECK constraint.
2. Write `class RazorpayProvider` implementing `method`, `label`, `description`, `is_available`,
   `start` (create the gateway order, store its id in `payment.provider` / `provider_ref`,
   return its checkout URL) and `on_delivered` (nothing: it is already paid).
3. Register it: `PROVIDERS["razorpay"] = RazorpayProvider(...)`. Read keys from environment
   variables (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`), never from templates or the database.
4. Add `POST /payments/razorpay/webhook`. It must be exempt from the CSRF dependency, verify the
   gateway's signature, find the `Payment` by `provider_ref`, and call `mark_paid()` or
   `mark_failed()`. Make it idempotent: gateways retry.
5. Decide what an unpaid online order means. Recommended: keep it `pending`, and cancel it with
   `orders.change_status(..., CANCELLED)` after 30 minutes without payment (a small scheduled
   job), which restocks automatically.
6. Test with the gateway's sandbox: success, failure, abandoned payment, duplicate webhook,
   amount mismatch (reject if the webhook amount differs from `payment.amount_paise`).

Orders already hold everything needed: `total_paise` is validated by a database constraint to
equal subtotal + delivery fee - discount, and all amounts are integer paise (gateways also use
the smallest currency unit).
