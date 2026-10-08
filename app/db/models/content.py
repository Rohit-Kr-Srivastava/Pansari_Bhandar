"""Site content managed from the admin dashboard: settings and testimonials."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IdMixin, JSONType, TimestampMixin


class SiteSetting(TimestampMixin, Base):
    """One JSON document per key. The "site" key holds app.services.settings.SiteSettings."""

    __tablename__ = "site_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType)


class Testimonial(IdMixin, TimestampMixin, Base):
    """A real customer's words, entered by the admin. Nothing is shown until one is added."""

    __tablename__ = "testimonials"

    name: Mapped[str] = mapped_column(String(80))
    place: Mapped[str | None] = mapped_column(String(80))
    body: Mapped[str] = mapped_column(Text)
    rating: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("rating IS NULL OR rating BETWEEN 1 AND 5", name="rating_range"),
        Index("ix_testimonials_is_active_sort_order", "is_active", "sort_order"),
    )
