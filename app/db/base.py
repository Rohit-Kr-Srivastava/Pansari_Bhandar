"""Declarative base, shared column types and mixins for all models."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Enum, Integer, MetaData, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Stable constraint names make Alembic migrations predictable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# BigInteger in PostgreSQL; plain Integer in SQLite so autoincrement works in tests.
BigIntPK = BigInteger().with_variant(Integer, "sqlite")
BigIntFK = BigInteger().with_variant(Integer, "sqlite")
# JSONB in PostgreSQL, JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {dict[str, Any]: JSONType}


class IdMixin:
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


def enum_column(enum_cls: type[StrEnum], name: str) -> Enum:
    """A VARCHAR column for a StrEnum (no native PG enum, easy to migrate).

    Pair it with enum_check() in the table's __table_args__ to enforce the allowed values in
    the database. The check is written explicitly so Alembic renders it exactly once.
    """
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        length=24,
        create_constraint=False,
        values_callable=lambda members: [m.value for m in members],
    )


def enum_check(column: str, enum_cls: type[StrEnum]) -> CheckConstraint:
    values = ", ".join(f"'{m.value}'" for m in enum_cls)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")
