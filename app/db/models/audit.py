"""Append-only record of admin (and approval) actions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BigIntFK, IdMixin, JSONType


class AdminAction(IdMixin, Base):
    __tablename__ = "admin_actions"

    # Nullable and SET NULL so the record survives if the account is later removed.
    actor_id: Mapped[int | None] = mapped_column(
        BigIntFK, ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(80))  # for example "seller.approve"
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[int | None] = mapped_column(BigIntFK)
    reason: Mapped[str | None] = mapped_column(Text)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_admin_actions_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_admin_actions_actor_id_created_at", "actor_id", "created_at"),
    )
