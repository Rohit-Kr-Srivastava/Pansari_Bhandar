"""PostgreSQL only (set TEST_DATABASE_URL): the Alembic migration must match the models."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

import app.db.models  # noqa: F401
from alembic import command
from app.db.base import Base
from tests.conftest import PG_URL

pytestmark = pytest.mark.skipif(not PG_URL, reason="needs TEST_DATABASE_URL (PostgreSQL)")


def _config() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", PG_URL or "")
    return cfg


def test_upgrade_matches_models_and_downgrade_is_clean() -> None:
    assert PG_URL
    engine = create_engine(PG_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
    command.upgrade(_config(), "head")
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
        assert diff == []
        assert set(inspect(conn).get_table_names()) == set(Base.metadata.tables) | {
            "alembic_version"
        }
    command.downgrade(_config(), "base")
    with engine.connect() as conn:
        assert inspect(conn).get_table_names() == ["alembic_version"]
    engine.dispose()
