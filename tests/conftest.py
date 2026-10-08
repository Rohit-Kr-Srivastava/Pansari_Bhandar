"""Test database fixtures.

By default tests run on in-memory SQLite. Set TEST_DATABASE_URL to a PostgreSQL URL to run the
same tests (and the migration test) against PostgreSQL.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

import app.db.models  # noqa: F401
from app.db.base import Base

PG_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True, scope="session")
def _fast_password_hashing() -> Iterator[None]:
    """Production uses argon2's defaults; tests use the cheapest valid parameters."""
    from argon2 import PasswordHasher

    from app.core import security

    original = security._hasher
    security._hasher = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1)
    yield
    security._hasher = original


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    if PG_URL:
        eng = create_engine(PG_URL)
    else:
        eng = create_engine("sqlite://")

        @event.listens_for(eng, "connect")
        def _fk_on(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

    yield eng
    eng.dispose()


def _drop_all(engine: Engine) -> None:
    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":  # DROP TABLE would trip foreign keys
            conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        Base.metadata.drop_all(conn)
        conn.commit()
        if engine.dialect.name == "sqlite":
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    _drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
        s.rollback()
    _drop_all(engine)
