"""commerce: order statuses, checkout token, delivery slots, site settings, testimonials

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_STATUSES = (
    "pending",
    "confirmed",
    "preparing",
    "ready",
    "out_for_delivery",
    "delivered",
    "cancelled",
    "returned",
)
OLD_ORDER = ("placed", "confirmed", "completed", "cancelled")
OLD_DELIVERY = (
    "placed",
    "confirmed",
    "packed",
    "out_for_delivery",
    "ready_for_pickup",
    "delivered",
    "cancelled",
)


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _enum(name: str, values: Sequence[str]) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, length=24)


def upgrade() -> None:
    # Orders: new status set. placed -> pending, completed -> delivered.
    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint(op.f("ck_orders_status_valid"), type_="check")
    op.execute("UPDATE orders SET status = 'pending' WHERE status = 'placed'")
    op.execute("UPDATE orders SET status = 'delivered' WHERE status = 'completed'")
    with op.batch_alter_table("orders") as batch:
        batch.alter_column(
            "status",
            existing_type=_enum("order_status", OLD_ORDER),
            type_=_enum("order_status", NEW_STATUSES),
            server_default="pending",
            existing_nullable=False,
        )
        batch.create_check_constraint(op.f("ck_orders_status_valid"), _in("status", NEW_STATUSES))
        batch.add_column(sa.Column("checkout_token", sa.String(length=64), nullable=True))
        batch.create_unique_constraint(op.f("uq_orders_checkout_token"), ["checkout_token"])

    # Delivery rows share the order status values.
    with op.batch_alter_table("delivery") as batch:
        batch.drop_constraint(op.f("ck_delivery_status_valid"), type_="check")
        batch.drop_constraint(op.f("ck_delivery_pickup_status_only_pickup"), type_="check")
    op.execute("UPDATE delivery SET status = 'pending' WHERE status = 'placed'")
    op.execute("UPDATE delivery SET status = 'ready' WHERE status IN ('packed', 'ready_for_pickup')")
    with op.batch_alter_table("delivery") as batch:
        batch.alter_column(
            "status",
            existing_type=_enum("delivery_status", OLD_DELIVERY),
            type_=_enum("delivery_status", NEW_STATUSES),
            server_default="pending",
            existing_nullable=False,
        )
        batch.create_check_constraint(op.f("ck_delivery_status_valid"), _in("status", NEW_STATUSES))
        batch.create_check_constraint(
            op.f("ck_delivery_pickup_never_out_for_delivery"),
            "method = 'delivery' OR status <> 'out_for_delivery'",
        )

    with op.batch_alter_table("seller_delivery_zones") as batch:
        batch.add_column(
            sa.Column("estimated_days", sa.Integer(), server_default="1", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_seller_delivery_zones_estimated_days_range"), "estimated_days BETWEEN 0 AND 30")

    op.create_table(
        "delivery_slots",
        sa.Column("store_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("label", sa.String(length=60), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("end_time > start_time", name=op.f("ck_delivery_slots_end_after_start")),
        sa.ForeignKeyConstraint(
            ["store_id"],
            ["stores.id"],
            name=op.f("fk_delivery_slots_store_id_stores"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_delivery_slots")),
    )
    op.create_index(
        "ix_delivery_slots_store_id_sort_order", "delivery_slots", ["store_id", "sort_order"]
    )

    op.create_table(
        "site_settings",
        sa.Column("key", sa.String(length=60), nullable=False),
        sa.Column(
            "value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_site_settings")),
    )

    op.create_table(
        "testimonials",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("place", sa.String(length=80), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint(
            "rating IS NULL OR rating BETWEEN 1 AND 5", name=op.f("ck_testimonials_rating_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_testimonials")),
    )
    op.create_index(
        "ix_testimonials_is_active_sort_order", "testimonials", ["is_active", "sort_order"]
    )

    # Homepage "featured" rail lookup.
    op.create_index("ix_products_is_featured_status", "products", ["is_featured", "status"])


def downgrade() -> None:
    op.drop_index("ix_products_is_featured_status", table_name="products")
    op.drop_index("ix_testimonials_is_active_sort_order", table_name="testimonials")
    op.drop_table("testimonials")
    op.drop_table("site_settings")
    op.drop_index("ix_delivery_slots_store_id_sort_order", table_name="delivery_slots")
    op.drop_table("delivery_slots")

    with op.batch_alter_table("seller_delivery_zones") as batch:
        batch.drop_constraint(op.f("ck_seller_delivery_zones_estimated_days_range"), type_="check")
        batch.drop_column("estimated_days")

    with op.batch_alter_table("delivery") as batch:
        batch.drop_constraint(op.f("ck_delivery_status_valid"), type_="check")
        batch.drop_constraint(op.f("ck_delivery_pickup_never_out_for_delivery"), type_="check")
    op.execute("UPDATE delivery SET status = 'placed' WHERE status = 'pending'")
    op.execute("UPDATE delivery SET status = 'confirmed' WHERE status = 'preparing'")
    op.execute("UPDATE delivery SET status = 'packed' WHERE status = 'ready' AND method = 'delivery'")
    op.execute(
        "UPDATE delivery SET status = 'ready_for_pickup' WHERE status = 'ready' AND method = 'pickup'"
    )
    op.execute("UPDATE delivery SET status = 'cancelled' WHERE status = 'returned'")
    with op.batch_alter_table("delivery") as batch:
        batch.alter_column(
            "status",
            existing_type=_enum("delivery_status", NEW_STATUSES),
            type_=_enum("delivery_status", OLD_DELIVERY),
            server_default="placed",
            existing_nullable=False,
        )
        batch.create_check_constraint(op.f("ck_delivery_status_valid"), _in("status", OLD_DELIVERY))
        batch.create_check_constraint(
            op.f("ck_delivery_pickup_status_only_pickup"), "method = 'pickup' OR status <> 'ready_for_pickup'"
        )

    with op.batch_alter_table("orders") as batch:
        batch.drop_constraint(op.f("uq_orders_checkout_token"), type_="unique")
        batch.drop_column("checkout_token")
        batch.drop_constraint(op.f("ck_orders_status_valid"), type_="check")
    op.execute("UPDATE orders SET status = 'placed' WHERE status = 'pending'")
    op.execute(
        "UPDATE orders SET status = 'confirmed'"
        " WHERE status IN ('preparing', 'ready', 'out_for_delivery')"
    )
    op.execute("UPDATE orders SET status = 'completed' WHERE status = 'delivered'")
    op.execute("UPDATE orders SET status = 'cancelled' WHERE status = 'returned'")
    with op.batch_alter_table("orders") as batch:
        batch.alter_column(
            "status",
            existing_type=_enum("order_status", NEW_STATUSES),
            type_=_enum("order_status", OLD_ORDER),
            server_default="placed",
            existing_nullable=False,
        )
        batch.create_check_constraint(op.f("ck_orders_status_valid"), _in("status", OLD_ORDER))
