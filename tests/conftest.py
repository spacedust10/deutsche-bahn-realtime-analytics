import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def rt_bytes() -> bytes:
    """Real GTFS-RT payload captured from the live long-distance feed."""
    return (FIXTURES / "gtfs_rt_sample.pb").read_bytes()


@pytest.fixture(scope="session")
def static_zip() -> Path:
    return FIXTURES / "gtfs_static_sample.zip"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture(scope="session")
def pg_settings():
    """Point the warehouse at a throwaway test database."""
    from dbrt.config import Settings

    return Settings.from_env({"PGDATABASE": os.environ.get("PGDATABASE_TEST", "dbrt_test")})


@pytest.fixture()
def warehouse(pg_settings):
    """A schema-applied, empty warehouse per test."""
    psycopg2 = pytest.importorskip("psycopg2")
    from dbrt.storage import Warehouse

    try:
        wh = Warehouse(pg_settings.dsn())
    except psycopg2.OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")

    wh.apply_schema()
    wh.truncate_all()
    yield wh
    wh.close()
